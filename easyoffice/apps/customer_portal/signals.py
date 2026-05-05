"""
apps/customer_portal/signals.py
================================

The brain of the portal: when a Contract is saved, this is where we…

  1. Make sure a customer_service.Customer row exists for the counterparty.
  2. Make sure each ContractContact on the contract exists and is up to date.
  3. Issue a fresh PortalAccessToken IF the contract is new OR has been
     renewed (start_date or end_date moved into the future).
  4. Create / update a MaintenanceSchedule if it's a maintenance contract.
  5. Email the contact(s) their portal link — but only AFTER the DB
     transaction commits, never inside the save itself.

Wired up in apps.py with `ready()` so the signals attach when Django boots.

Side-effect rules (why this file is so paranoid):

  * NEVER raise back into Contract.save(). A failed email or a missing
    Department FK MUST NOT roll back the user's contract. Wrap every
    fan-out in try/except and log.
  * Use transaction.on_commit so we don't email until the contract row
    is actually durable. If the outer transaction rolls back, the email
    never fires.
  * Idempotent: re-saving an unchanged contract should NOT spam the
    contact with new tokens. We check for an active token first.
"""
from __future__ import annotations

import logging

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from .models import (
    ContractContact, PortalAccessToken,
    MaintenanceSchedule,
    CONTRACT_MODEL, CUSTOMER_MODEL,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Contract resolution
# ─────────────────────────────────────────────────────────────────────────────

def _get_contract_model():
    """Look up the Contract model lazily (avoids hard-import at app-load)."""
    try:
        app_label, model_name = CONTRACT_MODEL.split('.')
        return apps.get_model(app_label, model_name)
    except Exception:
        logger.exception('customer_portal: cannot resolve contract model %r', CONTRACT_MODEL)
        return None


def _get_customer_model():
    try:
        app_label, model_name = CUSTOMER_MODEL.split('.')
        return apps.get_model(app_label, model_name)
    except Exception:
        logger.exception('customer_portal: cannot resolve customer model %r', CUSTOMER_MODEL)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Contract → Customer mirroring
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_customer_for(contract):
    """
    Find or create a customer_service.Customer that matches the contract's
    counterparty. Match priority:
        1. existing customer where any phone matches contract.vendor_phone
        2. existing customer where email matches contract.vendor_email
        3. create a new customer
    """
    Customer = _get_customer_model()
    if Customer is None:
        return None

    vendor_email = (getattr(contract, 'vendor_email', '') or '').strip().lower()
    vendor_phone = (getattr(contract, 'vendor_phone', '') or '').strip()
    vendor_company = (getattr(contract, 'vendor_company', '') or '').strip()
    vendor_name = (getattr(contract, 'vendor_name', '') or '').strip()

    # If we have nothing usable, don't create a ghost customer.
    if not (vendor_email or vendor_phone or vendor_company or vendor_name):
        logger.info(
            'customer_portal: contract %s has no counterparty info; '
            'skipping customer creation',
            contract.pk,
        )
        return None

    customer = None

    # 1) phone match — needs the CustomerPhone model from customer_service
    if vendor_phone:
        try:
            CustomerPhone = apps.get_model('customer_service', 'CustomerPhone')
            from .models import CONTRACT_MODEL  # noqa: F401  (keep import shape)
            from apps.customer_service.models import normalize_phone
            normalized = normalize_phone(vendor_phone)
            if normalized:
                phone_row = CustomerPhone.objects.filter(
                    normalized_number=normalized
                ).select_related('customer').first()
                if phone_row:
                    customer = phone_row.customer
        except Exception:
            logger.exception('customer_portal: phone-match lookup failed')

    # 2) email match
    if customer is None and vendor_email:
        customer = Customer.objects.filter(email__iexact=vendor_email).first()

    # 3) create
    if customer is None:
        customer_type = 'business' if vendor_company else 'individual'
        try:
            customer = Customer.objects.create(
                customer_type=customer_type,
                full_name=vendor_name or vendor_company or 'Unknown',
                company_name=vendor_company,
                email=vendor_email,
                address=getattr(contract, 'vendor_address', '') or '',
                preferred_contact_method='email' if vendor_email else 'phone',
                needs_feedback=True,
                notes=f'Auto-created from contract {contract.pk} on {timezone.now():%Y-%m-%d}.',
            )
        except Exception:
            logger.exception(
                'customer_portal: failed to create customer for contract %s',
                contract.pk,
            )
            return None

        # Attach a phone if we have one
        if vendor_phone:
            try:
                CustomerPhone = apps.get_model('customer_service', 'CustomerPhone')
                CustomerPhone.objects.create(
                    customer=customer,
                    phone_type='primary',
                    phone_number=vendor_phone,
                    is_primary=True,
                )
            except Exception:
                # Unique constraint on normalized_number might fire — fine.
                logger.warning(
                    'customer_portal: could not attach phone %s to customer %s',
                    vendor_phone, customer.pk,
                )

    return customer


# ─────────────────────────────────────────────────────────────────────────────
# Contact upsert
# ─────────────────────────────────────────────────────────────────────────────

def _upsert_primary_contact(contract, customer) -> ContractContact | None:
    """
    The contract form gives us ONE counterparty (vendor_name / vendor_email
    / vendor_phone). Treat that as the 'primary' contact for the contract.

    Additional contacts are managed via the portal-contact admin UI we'll
    expose separately — we do NOT silently create them here.
    """
    email = (getattr(contract, 'vendor_email', '') or '').strip().lower()
    name = (getattr(contract, 'vendor_name', '') or '').strip()

    if not email:
        # No email = no portal access, period. Token-link emails need an inbox.
        logger.info(
            'customer_portal: contract %s has no vendor_email; '
            'no portal contact will be created',
            contract.pk,
        )
        return None

    if not name:
        # Use whatever we have so the contact row is usable.
        name = (getattr(contract, 'vendor_company', '') or email.split('@')[0]).strip()

    contact, created = ContractContact.objects.update_or_create(
        contract=contract,
        email__iexact=email,
        defaults={
            'customer': customer,
            'full_name': name,
            'phone': (getattr(contract, 'vendor_phone', '') or '').strip(),
            'role': 'primary',
            'email': email,  # keep canonical lowercase
            'is_active': True,
        },
    )
    return contact


# ─────────────────────────────────────────────────────────────────────────────
# Token issuance
# ─────────────────────────────────────────────────────────────────────────────

def _expiry_for(contract):
    """
    A token expires at end-of-day on the contract's end_date, plus a small
    grace period (1 day) so the customer isn't locked out at the exact
    second the contract ends.
    """
    from datetime import datetime, date, time, timedelta

    end = getattr(contract, 'end_date', None)
    if not end:
        # Contracts without an end date get a 1-year token. This shouldn't
        # happen in practice — the form requires end_date — but defensive.
        return timezone.now() + timedelta(days=365)

    # end_date *should* be a DateField, but in practice we sometimes see
    # it arrive as a string (CSV imports, raw API payloads, legacy data
    # that wasn't coerced). Normalise to a date before doing arithmetic.
    if isinstance(end, datetime):
        end = end.date()
    elif not isinstance(end, date):
        # String, or something exotic — try to parse it.
        from django.utils.dateparse import parse_date, parse_datetime
        s = str(end).strip()
        parsed = parse_date(s)
        if parsed is None:
            dt = parse_datetime(s)
            parsed = dt.date() if dt else None
        if parsed is None:
            # Give up gracefully rather than raising — issue a 1-year token
            # and log so we can chase the bad data.
            logger.warning(
                'customer_portal: contract %s has unparseable end_date %r; '
                'falling back to 1-year token expiry',
                getattr(contract, 'pk', '?'), end,
            )
            return timezone.now() + timedelta(days=365)
        end = parsed

    expiry_dt = datetime.combine(end + timedelta(days=1), time(23, 59, 59))
    if timezone.is_naive(expiry_dt):
        expiry_dt = timezone.make_aware(expiry_dt, timezone.get_current_timezone())
    return expiry_dt


def _issue_token_if_needed(contact, contract, *, force_new=False) -> tuple:
    """
    Returns (token_obj, was_new). If the contact already has an active
    token for this contract and we're NOT forcing a new one, reuse it.
    """
    if not force_new:
        existing = (
            PortalAccessToken.objects
            .filter(
                contact=contact,
                contract=contract,
                revoked_at__isnull=True,
                valid_until__gt=timezone.now(),
            )
            .order_by('-issued_at')
            .first()
        )
        if existing:
            return existing, False

    # Revoke any prior tokens for this (contact, contract) — only one
    # active token at a time.
    PortalAccessToken.objects.filter(
        contact=contact,
        contract=contract,
        revoked_at__isnull=True,
    ).update(
        revoked_at=timezone.now(),
        revoked_reason='Superseded by new token (contract renewal or manual reissue).',
    )

    token = PortalAccessToken.objects.create(
        contact=contact,
        contract=contract,
        valid_from=timezone.now(),
        valid_until=_expiry_for(contract),
    )
    return token, True


# ─────────────────────────────────────────────────────────────────────────────
# Maintenance schedule
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_maintenance_schedule(contract) -> MaintenanceSchedule | None:
    """
    Only for maintenance contracts. We try to read optional fields:

        contract.maintenance_features  — JSON list/string e.g. ['preventive', 'on_call']
        contract.pm_cadence            — e.g. 'quarterly'

    If neither exists, we default to: has_preventive=True, has_on_call=False,
    cadence='quarterly'. The user can edit it later.
    """
    contract_type = (getattr(contract, 'contract_type', '') or '').lower()
    if 'maintenance' not in contract_type:
        return None

    features = getattr(contract, 'maintenance_features', None) or []
    if isinstance(features, str):
        features = [f.strip().lower() for f in features.split(',') if f.strip()]
    else:
        features = [str(f).lower() for f in features]

    has_preventive = (not features) or ('preventive' in features) or ('pm' in features)
    has_on_call = ('on_call' in features) or ('on-call' in features) or ('reactive' in features)

    cadence = (getattr(contract, 'pm_cadence', None) or 'quarterly').lower()

    schedule, _created = MaintenanceSchedule.objects.update_or_create(
        contract=contract,
        defaults={
            'cadence': cadence,
            'has_preventive': has_preventive,
            'has_on_call': has_on_call,
        },
    )

    # Set next_due_date if we don't have one yet — based on contract.start_date.
    if not schedule.next_due_date and getattr(contract, 'start_date', None):
        from dateutil.relativedelta import relativedelta
        from datetime import timedelta
        offsets = {
            'weekly':       timedelta(weeks=1),
            'biweekly':     timedelta(weeks=2),
            'monthly':      relativedelta(months=1),
            'quarterly':    relativedelta(months=3),
            'semi_annual':  relativedelta(months=6),
            'annual':       relativedelta(years=1),
        }
        first_due = contract.start_date + offsets.get(cadence, relativedelta(months=3))
        schedule.next_due_date = first_due
        schedule.save(update_fields=['next_due_date', 'updated_at'])

    return schedule


# ─────────────────────────────────────────────────────────────────────────────
# Detecting a renewal vs a routine save
# ─────────────────────────────────────────────────────────────────────────────

def _looks_like_renewal(instance) -> bool:
    """
    Heuristic: a renewal is when end_date moved forward from a prior value.

    We can't see the old row directly without a pre_save hook + threadlocal,
    so we rely on:

      * `_was_renewed` flag set by your renewal view (preferred)
      * end_date in the future AND there's already an active token whose
        valid_until is before end_date.

    Yes, the second case is fuzzy — but the consequence of a false positive
    is "the customer gets a new token", which is harmless. The consequence
    of a false negative would be "renewed contract with stale token", which
    is what we're trying to avoid.
    """
    if getattr(instance, '_was_renewed', False):
        return True

    end = getattr(instance, 'end_date', None)
    if not end:
        return False

    # Compare any current active token's expiry against the contract's
    # current end_date. If the contract's end_date is now LATER than the
    # latest token's valid_until, treat as renewal.
    latest = (
        PortalAccessToken.objects
        .filter(contract=instance, revoked_at__isnull=True)
        .order_by('-valid_until')
        .first()
    )
    if not latest:
        return False  # nothing to compare against

    # Build a tz-aware datetime from end_date for comparison.
    from datetime import datetime, time, timedelta
    target = datetime.combine(end + timedelta(days=1), time(23, 59, 59))
    if timezone.is_naive(target):
        target = timezone.make_aware(target, timezone.get_current_timezone())
    return target > latest.valid_until + timezone.timedelta(days=1)


# ─────────────────────────────────────────────────────────────────────────────
# The signal itself
# ─────────────────────────────────────────────────────────────────────────────

def _connect():
    """
    Connect the post_save signal once we know the Contract model exists.
    Called from apps.py.ready().
    """
    Contract = _get_contract_model()
    if Contract is None:
        logger.warning('customer_portal: signal NOT connected — Contract model missing')
        return

    @receiver(post_save, sender=Contract, dispatch_uid='customer_portal_contract_save')
    def on_contract_saved(sender, instance, created, **kwargs):
        # Wrap EVERYTHING in try/except. A signal failure must never break
        # the user's contract save.
        try:
            customer = _ensure_customer_for(instance)
        except Exception:
            logger.exception('customer_portal: customer mirror failed')
            customer = None

        contact = None
        if customer is not None:
            try:
                contact = _upsert_primary_contact(instance, customer)
            except Exception:
                logger.exception('customer_portal: contact upsert failed')

        # Maintenance schedule (idempotent — safe on every save)
        try:
            _ensure_maintenance_schedule(instance)
        except Exception:
            logger.exception('customer_portal: PM schedule upsert failed')

        # Token: only if we have a contact AND (created OR renewed).
        if contact is None:
            return

        force_new = bool(created) or _looks_like_renewal(instance)
        try:
            token, was_new = _issue_token_if_needed(
                contact, instance, force_new=force_new,
            )
        except Exception:
            logger.exception('customer_portal: token issuance failed')
            return

        # Email — but only after commit, only if it's actually a new token.
        if was_new:
            transaction.on_commit(
                lambda: _send_token_email_safely(token, instance, contact, is_renewal=not created)
            )


def _send_token_email_safely(token, contract, contact, *, is_renewal: bool) -> None:
    """
    Wrapper around the email send. Lives at module level (not as a closure
    inside _connect) so it's testable and stack-traces are readable.
    """
    try:
        from . import notifications
        notifications.send_portal_invite(
            token=token,
            contract=contract,
            contact=contact,
            is_renewal=is_renewal,
        )
    except Exception:
        logger.exception(
            'customer_portal: failed to send portal invite for contract %s contact %s',
            getattr(contract, 'pk', '?'),
            getattr(contact, 'pk', '?'),
        )