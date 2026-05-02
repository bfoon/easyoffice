"""
apps/finance/contract_invoice_service.py
========================================

Generates invoices for contracts, using the invoice app's template engine.

Flow:
    1. Pick a template (override > contract.default_invoice_template > error).
    2. Build a draft InvoiceDocument from the template (invoice app does this).
    3. Pre-fill client info from the contract counterparty.
    4. Replace the seed line item with a contract-derived one.
    5. Compute the billing period covered by this invoice.
    6. Set invoice_date = today, due_date based on payment terms.
    7. Finalize the invoice — invoice app allocates the number + saves the PDF.
    8. Create a ContractInvoiceLink for the audit trail.
    9. Bump contract.last_invoice_generated_at + advance contract.next_invoice_date.

Email sending honors `contract.auto_send_invoice` for the scheduled path; the
manual path leaves sending to the user (they can re-use the invoice app's
detail page).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from dateutil.relativedelta import relativedelta  # add to requirements if missing
from django.db import transaction
from django.utils import timezone

from apps.invoices.models import InvoiceDocument, InvoiceLineItem, InvoiceTemplate
from apps.invoices.services import (
    create_invoice_from_template,
    finalize_invoice,
)


# ── Errors ──────────────────────────────────────────────────────────────────

class ContractInvoiceError(Exception):
    """Raised when an invoice cannot be generated for a contract."""


# ── Billing-cycle math ──────────────────────────────────────────────────────

def _advance_date(start: date, billing_cycle: str) -> Optional[date]:
    """
    Given a start date and a billing cycle, return the NEXT period's start.
    Returns None for one-off contracts (no further invoices).
    """
    if not start:
        return None
    cycle = (billing_cycle or '').lower()
    if cycle in ('monthly', 'month'):
        return start + relativedelta(months=1)
    if cycle in ('quarterly', 'quarter'):
        return start + relativedelta(months=3)
    if cycle in ('semi_annual', 'semi-annual', 'biannual'):
        return start + relativedelta(months=6)
    if cycle in ('annual', 'annually', 'yearly', 'year'):
        return start + relativedelta(years=1)
    if cycle in ('weekly', 'week'):
        return start + timedelta(weeks=1)
    if cycle in ('biweekly', 'fortnightly'):
        return start + timedelta(weeks=2)
    if cycle in ('one_off', 'oneoff', 'one-off', ''):
        return None
    # Unknown cycle → treat like one-off so we don't loop forever
    return None


def _period_for(contract, period_start: date) -> tuple[date, date]:
    """Return (start, end_inclusive) for the billing period beginning at start."""
    next_start = _advance_date(period_start, contract.billing_cycle)
    if next_start is None:
        # one-off — period is just a single day, or the full contract span
        end = contract.end_date or period_start
        return period_start, end
    return period_start, next_start - timedelta(days=1)


def _parse_payment_terms_days(terms: str) -> int:
    """
    Best-effort parse of 'Net 30', 'Net 14', 'Net60' → integer days.
    Falls back to 30 if it can't tell.
    """
    if not terms:
        return 30
    digits = ''.join(ch for ch in terms if ch.isdigit())
    try:
        return int(digits) if digits else 30
    except ValueError:
        return 30


# ── Public API ──────────────────────────────────────────────────────────────

@dataclass
class GenerationResult:
    invoice: InvoiceDocument
    link: 'ContractInvoiceLink'  # noqa
    was_finalized: bool


@transaction.atomic
def generate_invoice_for_contract(
    contract,
    *,
    actor,
    template: Optional[InvoiceTemplate] = None,
    finalize: bool = True,
    period_start: Optional[date] = None,
    source: str = 'manual',
) -> GenerationResult:
    """
    Generate one invoice for a contract.

    Args:
        contract: the finance.Contract instance.
        actor:    the user triggering the generation (or a system user
                  for scheduled runs).
        template: override template. If None, uses contract.default_invoice_template.
        finalize: if True, allocate number + build PDF + save. If False,
                  leave as DRAFT for the user to review.
        period_start: start date of the billing period this invoice covers.
                      Defaults to contract.next_invoice_date or today.
        source: 'manual' or 'scheduled' — recorded on the link.

    Returns:
        GenerationResult(invoice, link, was_finalized).

    Raises:
        ContractInvoiceError on any pre-condition failure.
    """
    # local import to avoid circular: this module sits in finance, the link
    # model sits in finance/models.py too.
    from apps.finance.models import ContractInvoiceLink

    # Re-fetch with a row lock so two simultaneous generations can't race.
    contract = (
        contract.__class__.objects
        .select_for_update()
        .get(pk=contract.pk)
    )

    # ── 1. Pick a template ──────────────────────────────────────────────────
    tmpl = template or contract.default_invoice_template
    if tmpl is None:
        raise ContractInvoiceError(
            'No invoice template selected. Set a default on the contract or pick one when generating.'
        )

    # ── 2. Build draft from template ────────────────────────────────────────
    invoice = create_invoice_from_template(tmpl, actor)

    # ── 3. Pre-fill client info from the contract counterparty ──────────────
    invoice.client_name = (
        contract.counterparty_name
        or contract.vendor_name
        or contract.vendor_company
        or ''
    )[:200]
    invoice.client_email = (
        getattr(contract, 'counterparty_email', '') or contract.vendor_email or ''
    )[:254]
    invoice.client_address = (contract.vendor_address or '')

    # If the contract carries its own currency / payment terms, prefer them
    # over the template defaults.
    if contract.currency:
        invoice.currency = contract.currency

    # ── 4. Compute period + dates ───────────────────────────────────────────
    today = timezone.localdate()
    period_start = period_start or contract.next_invoice_date or today
    p_start, p_end = _period_for(contract, period_start)

    invoice.invoice_date = today
    invoice.due_date     = today + timedelta(days=_parse_payment_terms_days(invoice.payment_terms))
    invoice.po_reference = (contract.reference or '')[:100]

    # ── 5. Replace the seed line item with a contract-derived one ──────────
    invoice.items.all().delete()
    line_desc = (
        f'{contract.title} — {contract.get_billing_cycle_display()} '
        f'({p_start:%b %d, %Y} to {p_end:%b %d, %Y})'
    )[:500]
    InvoiceLineItem.objects.create(
        invoice=invoice,
        position=0,
        description=line_desc,
        quantity=Decimal('1.00'),
        unit_price=Decimal(contract.standard_cost or 0),
    )

    invoice.save()
    invoice.recalculate_totals(save=True)

    # ── 6. Finalize if asked (allocates number + writes PDF) ───────────────
    was_finalized = False
    if finalize:
        invoice = finalize_invoice(invoice, actor)
        was_finalized = True

    # ── 7. Audit row ────────────────────────────────────────────────────────
    link = ContractInvoiceLink.objects.create(
        contract=contract,
        invoice=invoice,
        period_start=p_start,
        period_end=p_end,
        source=source if source in {'manual', 'scheduled'} else 'manual',
        generated_by=actor,
    )

    # ── 8. Bump bookkeeping on the contract ────────────────────────────────
    contract.last_invoice_generated_at = timezone.now()
    next_period = _advance_date(period_start, contract.billing_cycle)
    if next_period:
        contract.next_invoice_date = next_period
    else:
        # one-off — clear next_invoice_date so we don't keep generating
        contract.next_invoice_date = None
    contract.save(update_fields=[
        'last_invoice_generated_at', 'next_invoice_date', 'updated_at'
    ] if hasattr(contract, 'updated_at') else [
        'last_invoice_generated_at', 'next_invoice_date'
    ])

    return GenerationResult(invoice=invoice, link=link, was_finalized=was_finalized)


# ── Scheduled runner ────────────────────────────────────────────────────────

def find_contracts_due(today: Optional[date] = None):
    """
    Yield contracts that are eligible for auto-generation today:
        - status is active
        - auto_generate_invoice is True
        - has a default_invoice_template
        - next_invoice_date is set and <= today
        - end_date hasn't passed (we don't bill after expiry)
    """
    from apps.finance.models import Contract  # local import — avoids circular
    today = today or timezone.localdate()

    qs = Contract.objects.filter(
        status='active',
        auto_generate_invoice=True,
        default_invoice_template__isnull=False,
        next_invoice_date__isnull=False,
        next_invoice_date__lte=today,
    )
    # Exclude contracts whose end_date has already passed
    qs = qs.filter(models_q_end_or_open(today))
    return qs


def models_q_end_or_open(today):
    """end_date is null OR end_date >= today"""
    from django.db.models import Q
    return Q(end_date__isnull=True) | Q(end_date__gte=today)


def run_scheduled_generation(today: Optional[date] = None, *, system_actor=None):
    """
    Walk every due contract and generate an invoice for it. Designed to be
    safe to call repeatedly (idempotent for a given period because we
    advance next_invoice_date after each successful generation).

    Returns a summary dict — useful for logging in the management command
    or Celery task.
    """
    from apps.finance.models import Contract  # noqa
    today = today or timezone.localdate()
    actor = system_actor or _get_system_user()

    summary = {
        'date': today.isoformat(),
        'attempted': 0,
        'succeeded': 0,
        'failed': 0,
        'errors': [],   # list of (contract_id, str)
        'invoices': [], # list of (contract_id, invoice_number)
    }

    for contract in find_contracts_due(today):
        summary['attempted'] += 1
        try:
            result = generate_invoice_for_contract(
                contract,
                actor=actor,
                template=None,                      # use contract default
                finalize=True,                       # auto-finalize per spec
                period_start=contract.next_invoice_date,
                source='scheduled',
            )
            summary['succeeded'] += 1
            summary['invoices'].append((str(contract.pk), result.invoice.number))

            # Send email if the contract opted in
            if contract.auto_send_invoice and result.invoice.client_email:
                try:
                    _send_invoice_email(result.invoice, contract)
                except Exception as e:
                    # Don't fail the whole generation just because email broke
                    summary['errors'].append((str(contract.pk), f'email failed: {e}'))
        except Exception as e:
            summary['failed'] += 1
            summary['errors'].append((str(contract.pk), str(e)))

    return summary


def _get_system_user():
    """
    Return a user to attribute scheduled generations to. Falls back to the
    first superuser if no dedicated 'system' account exists.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()
    user = (
        User.objects.filter(username='system').first()
        or User.objects.filter(is_superuser=True).order_by('pk').first()
    )
    if not user:
        raise ContractInvoiceError(
            'Cannot run scheduled generation: no system or superuser account exists.'
        )
    return user


def _send_invoice_email(invoice: InvoiceDocument, contract):
    """
    Email the finalized invoice PDF to the contract's counterparty.
    Lightweight — uses Django's mail layer + the SharedFile bytes.
    """
    from django.core.mail import EmailMessage
    from django.conf import settings

    if not invoice.client_email:
        return
    if not invoice.generated_pdf:
        return  # nothing to attach

    subject = f'{invoice.get_doc_type_display()} {invoice.number}'
    body = (
        f'Dear {invoice.client_name or "Customer"},\n\n'
        f'Please find attached {invoice.get_doc_type_display().lower()} '
        f'{invoice.number} for {contract.title}.\n\n'
        f'Total: {invoice.currency} {invoice.total}\n'
        f'Due: {invoice.due_date:%B %d, %Y}\n\n'
        f'Thank you.'
    )
    msg = EmailMessage(
        subject=subject,
        body=body,
        from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
        to=[invoice.client_email],
    )
    pdf_field = invoice.generated_pdf.file
    pdf_field.open('rb')
    try:
        msg.attach(f'{invoice.number}.pdf', pdf_field.read(), 'application/pdf')
    finally:
        pdf_field.close()
    msg.send(fail_silently=False)
