"""
apps/it_support/customer_link.py
=================================

Bridge between equipment maintenance and the customer-service contact book
(apps.customer_service.Customer / CustomerPhone).

Why this exists
───────────────
Maintenance intake used to capture the owner as loose text — owner_name,
owner_email, owner_phone — with no link to anything. The same walk-in
customer coming back three times produced three unrelated records, none of
which showed up on their contact page. Meanwhile customer_service already
had a proper Customer table with phone normalisation and dedupe.

This module makes maintenance a first-class producer of that table:

  • Existing contact  → link the job to it (resolve_customer)
  • New walk-in       → create the contact, so intake POPULATES the book
                        rather than bypassing it (get_or_create_customer)
  • Phone matching    → via customer_service.normalize_phone, so
                        "+220 123 4567", "1234567" and "002201234567"
                        all resolve to the SAME contact instead of a third

Design rules
────────────
1. SNAPSHOT, DON'T DEREFERENCE. The job keeps owner_name/email/phone as a
   point-in-time record of who handed the equipment over. If a customer
   later renames their company, historical service records — which may be
   signed — must not silently rewrite themselves. The FK is for linking and
   history; the text fields are the record.

2. STAFF ARE NOT CUSTOMERS. An internal UNDP laptop repair has owner_user
   set and customer left NULL. Forcing a Customer row for every staff
   member would pollute the contact book with people sales never talks to.
   The two coexist; see resolve_customer's staff guard.

3. FAIL SOFT. customer_service may not be installed. Every entry point
   degrades to "no customer link" rather than breaking intake — a repair
   desk must keep working even if the CRM is down.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
#  Lazy imports — customer_service is an optional dependency
# ─────────────────────────────────────────────────────────────────────────────

def _cs_models():
    """Return (Customer, CustomerPhone, normalize_phone) or (None, None, None)."""
    try:
        from apps.customer_service.models import (
            Customer, CustomerPhone, normalize_phone,
        )
        return Customer, CustomerPhone, normalize_phone
    except Exception:
        return None, None, None


def customer_service_available() -> bool:
    return _cs_models()[0] is not None


# ─────────────────────────────────────────────────────────────────────────────
#  Lookup
# ─────────────────────────────────────────────────────────────────────────────

def find_customer_by_phone(phone: str):
    """
    Resolve a raw phone string to a Customer via the normalised index.

    normalize_phone() handles the Gambian dialling shapes: a bare 7-digit
    local number gets the 220 country code, a 00 international prefix is
    stripped. Matching on normalized_number (which is unique+indexed) is
    what stops the same person becoming three contacts.
    """
    Customer, CustomerPhone, normalize_phone = _cs_models()
    if Customer is None or not phone:
        return None

    normalized = normalize_phone(phone)
    if not normalized:
        return None

    match = (
        CustomerPhone.objects
        .select_related('customer')
        .filter(normalized_number=normalized, is_active=True)
        .first()
    )
    return match.customer if match else None


def find_customer_by_email(email: str):
    """Resolve an email to a Customer, checking both email fields."""
    Customer, _, _ = _cs_models()
    if Customer is None or not email:
        return None

    from django.db.models import Q
    email = email.strip()
    if not email:
        return None
    return (
        Customer.objects
        .filter(Q(email__iexact=email) | Q(alternate_email__iexact=email))
        .first()
    )


def search_customers(query: str, limit: int = 20):
    """
    Search the contact book for the intake autocomplete.

    Matches name, company, email, customer code and phone. Phone matching
    tries the normalised form first so a user can type the number however
    they have it written down.
    """
    Customer, CustomerPhone, normalize_phone = _cs_models()
    if Customer is None:
        return []

    query = (query or '').strip()
    if len(query) < 2:
        return []

    from django.db.models import Q
    filters = (
        Q(full_name__icontains=query) |
        Q(company_name__icontains=query) |
        Q(email__icontains=query) |
        Q(customer_code__icontains=query) |
        Q(phones__phone_number__icontains=query)
    )

    # If the query looks like a phone number, also match the normalised index —
    # covers someone typing "1234567" for a contact stored as "+2201234567".
    normalized = normalize_phone(query)
    if normalized and len(normalized) > 4:
        filters |= Q(phones__normalized_number__icontains=normalized.lstrip('+'))

    return list(
        Customer.objects
        .filter(filters)
        .prefetch_related('phones')
        .distinct()
        .order_by('full_name')[:limit]
    )


def serialize_customer(customer) -> dict:
    """Compact JSON payload for the intake autocomplete."""
    phones = [p for p in customer.phones.all() if p.is_active]
    primary = next((p for p in phones if p.is_primary), phones[0] if phones else None)
    return {
        'id':            customer.pk,
        'code':          customer.customer_code,
        'display_name':  customer.display_name,
        'full_name':     customer.full_name,
        'company_name':  customer.company_name,
        'email':         customer.email,
        'phone':         primary.phone_number if primary else '',
        'address':       customer.address,
        'city':          customer.city,
        'customer_type': customer.customer_type,
        'phone_count':   len(phones),
    }


# ─────────────────────────────────────────────────────────────────────────────
#  Creation / resolution
# ─────────────────────────────────────────────────────────────────────────────

def get_or_create_customer(*, name, email='', phone='', company='',
                           address='', actor=None, customer_type=None):
    """
    Find an existing contact by phone (then email), or create a new one.

    Returns (customer, created) — or (None, False) if customer_service isn't
    installed. Never raises: intake must not fail because the CRM is
    unavailable.

    Phone wins over email as the match key: at a repair desk the phone number
    is what people actually give, and it's the field with a unique normalised
    index behind it.
    """
    Customer, CustomerPhone, normalize_phone = _cs_models()
    if Customer is None:
        return None, False

    name = (name or '').strip()
    if not name:
        return None, False

    email = (email or '').strip()
    phone = (phone or '').strip()
    company = (company or '').strip()

    try:
        existing = find_customer_by_phone(phone) or find_customer_by_email(email)
        if existing:
            _enrich_customer(existing, email=email, phone=phone,
                             company=company, address=address)
            return existing, False

        customer = Customer.objects.create(
            customer_type=customer_type or ('business' if company else 'individual'),
            full_name=name,
            company_name=company,
            email=email,
            address=(address or '').strip(),
            preferred_contact_method='phone' if phone else ('email' if email else 'phone'),
            notes='Created from IT equipment maintenance intake.',
            created_by=actor,
        )
        if phone:
            CustomerPhone.objects.create(
                customer=customer,
                phone_type='primary',
                phone_number=phone,
                is_primary=True,
                is_active=True,
            )
        logger.info(
            'Created customer %s (%s) from maintenance intake',
            customer.customer_code, customer.display_name,
        )
        return customer, True

    except Exception:
        # Includes the unique-constraint race on normalized_number: two
        # intakes for the same new number at once. Re-resolve rather than
        # blowing up the repair record.
        logger.exception('Could not get/create customer for maintenance intake')
        try:
            fallback = find_customer_by_phone(phone) or find_customer_by_email(email)
            if fallback:
                return fallback, False
        except Exception:
            pass
        return None, False


def _enrich_customer(customer, *, email='', phone='', company='', address=''):
    """
    Backfill blank fields on an existing contact from fresh intake data.

    Only ever fills EMPTY fields — never overwrites. If the contact says the
    company is "Acme Ltd" and intake typed "acme", the existing value stands.
    Sales owns the contact book; maintenance only contributes what's missing.
    """
    Customer, CustomerPhone, normalize_phone = _cs_models()
    if Customer is None:
        return

    updates = []
    if email and not customer.email:
        customer.email = email
        updates.append('email')
    if company and not customer.company_name:
        customer.company_name = company
        updates.append('company_name')
    if address and not customer.address:
        customer.address = address.strip()
        updates.append('address')

    if updates:
        updates.append('updated_at')
        customer.save(update_fields=updates)

    # Attach a genuinely new number as a secondary contact.
    if phone:
        normalized = normalize_phone(phone)
        if normalized and not customer.phones.filter(normalized_number=normalized).exists():
            try:
                CustomerPhone.objects.create(
                    customer=customer,
                    phone_type='secondary',
                    phone_number=phone,
                    is_primary=False,
                    is_active=True,
                )
            except Exception:
                logger.exception('Could not attach phone to customer %s', customer.pk)


def resolve_customer_for_job(payload: dict, *, owner_user=None, actor=None):
    """
    Work out which Customer (if any) an intake payload belongs to.

    Resolution order:
      1. Explicit customer_id from the intake autocomplete → use it.
      2. owner_user set (internal staff asset) → NO customer. Staff are not
         contacts; see rule 2 in the module docstring.
      3. Otherwise → get_or_create by phone/email, so the walk-in lands in
         the contact book.

    Returns (customer_or_None, created_bool).
    """
    Customer, _, _ = _cs_models()
    if Customer is None:
        return None, False

    customer_id = payload.get('customer_id')
    if customer_id:
        customer = Customer.objects.filter(pk=customer_id).first()
        if customer:
            _enrich_customer(
                customer,
                email=(payload.get('owner_email') or '').strip(),
                phone=(payload.get('owner_phone') or '').strip(),
                company=(payload.get('owner_company') or '').strip(),
                address=(payload.get('owner_address') or '').strip(),
            )
            return customer, False

    # Internal staff equipment — deliberately no Customer record.
    if owner_user is not None:
        return None, False

    # Opt-out for a one-off the desk doesn't want in the book.
    if str(payload.get('skip_customer_record') or '').lower() in ('1', 'true', 'on'):
        return None, False

    return get_or_create_customer(
        name=(payload.get('owner_name') or '').strip(),
        email=(payload.get('owner_email') or '').strip(),
        phone=(payload.get('owner_phone') or '').strip(),
        company=(payload.get('owner_company') or '').strip(),
        address=(payload.get('owner_address') or '').strip(),
        actor=actor,
    )
