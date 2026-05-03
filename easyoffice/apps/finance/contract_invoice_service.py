"""
apps/finance/contract_invoice_service.py
=========================================

Generates invoices for contracts using the invoice app's template engine.

Key behaviours
--------------
* generate_invoice_for_contract()  – create ONE invoice for a contract
  (manual or scheduled).  Email is NOT sent here; sending is handled
  separately by send_due_invoice_emails() so the due-date trigger works.

* run_scheduled_generation()  – called by Celery Beat (daily at 6 am).
  Walks every active contract whose next_invoice_date <= today, generates
  the invoice and advances next_invoice_date.

* send_due_invoice_emails()  – called by a SEPARATE Celery Beat task (daily,
  same or slightly later time).  Walks every ContractInvoiceLink whose
  invoice.due_date == today and auto_send_invoice is True, and emails the PDF.

* find_contracts_due() / find_invoices_due_today()  – querysets.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from dateutil.relativedelta import relativedelta
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
    Given a start date and billing cycle, return the next period's start date.
    Returns None for one-off contracts.
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
    # one_off or unknown → no further invoices
    return None


def _period_for(contract, period_start: date) -> tuple[date, date]:
    """Return (start, end_inclusive) for the billing period beginning at start."""
    next_start = _advance_date(period_start, contract.billing_cycle)
    if next_start is None:
        end = contract.end_date or period_start
        return period_start, end
    return period_start, next_start - timedelta(days=1)


def _parse_payment_terms_days(terms: str) -> int:
    """Parse 'Net 30', 'Net 14', 'Net60' → int days. Falls back to 30."""
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
    link: 'ContractInvoiceLink'
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
        contract:      finance.Contract instance.
        actor:         user triggering generation (or system user for scheduled).
        template:      override template; defaults to contract.default_invoice_template.
        finalize:      if True, allocate number + build PDF. If False, leave as DRAFT.
        period_start:  start of the billing period; defaults to contract.next_invoice_date.
        source:        'manual' or 'scheduled'.

    Returns:
        GenerationResult(invoice, link, was_finalized)

    Raises:
        ContractInvoiceError on pre-condition failure.

    Note:
        This function does NOT send email — call send_due_invoice_emails()
        (or _send_invoice_email() directly) if you want to send immediately.
        For the scheduled path, sending is triggered on due_date by a separate task.
    """
    from apps.finance.models import ContractInvoiceLink

    # Row-lock the contract to prevent race conditions
    contract = (
        contract.__class__.objects
        .select_for_update()
        .get(pk=contract.pk)
    )

    # ── 1. Pick a template ──────────────────────────────────────────────────
    tmpl = template or contract.default_invoice_template
    if tmpl is None:
        raise ContractInvoiceError(
            'No invoice template is attached to this contract. '
            'Go to Edit Contract and select a Default Invoice Template first.'
        )

    # ── 2. Build draft from template ────────────────────────────────────────
    invoice = create_invoice_from_template(tmpl, actor)

    # ── 3. Pre-fill client info from counterparty ───────────────────────────
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

    if contract.currency:
        invoice.currency = contract.currency

    # ── 4. Compute period + dates ───────────────────────────────────────────
    today = timezone.localdate()
    period_start = period_start or contract.next_invoice_date or today
    p_start, p_end = _period_for(contract, period_start)

    invoice.invoice_date = today
    terms_days = _parse_payment_terms_days(invoice.payment_terms)
    invoice.due_date = today + timedelta(days=terms_days)
    invoice.po_reference = (contract.reference or '')[:100]

    # ── 5. Replace seed line item with a contract-derived one ───────────────
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

    # ── 6. Finalize (allocates number + writes PDF) ─────────────────────────
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

    # ── 8. Bump contract bookkeeping ────────────────────────────────────────
    # The Contract model uses `last_invoice_date` (DateField), not
    # `last_invoice_generated_at`.  Store today's date, not a datetime.
    contract.last_invoice_date = today
    next_period = _advance_date(period_start, contract.billing_cycle)
    if next_period:
        contract.next_invoice_date = next_period
    else:
        contract.next_invoice_date = None  # one-off — stop scheduling

    save_fields = ['last_invoice_date', 'next_invoice_date']
    if hasattr(contract, 'updated_at'):
        save_fields.append('updated_at')
    contract.save(update_fields=save_fields)

    return GenerationResult(invoice=invoice, link=link, was_finalized=was_finalized)


# ── Scheduled generation runner ─────────────────────────────────────────────

def find_contracts_due(today: Optional[date] = None):
    """
    Return a queryset of contracts eligible for auto-invoice generation today:
      - status == 'active'
      - auto_generate_invoice is True
      - default_invoice_template is set
      - next_invoice_date is set and <= today
      - end_date hasn't passed
    """
    from apps.finance.models import Contract
    from django.db.models import Q
    today = today or timezone.localdate()

    qs = Contract.objects.filter(
        status='active',
        auto_generate_invoice=True,
        default_invoice_template__isnull=False,
        next_invoice_date__isnull=False,
        next_invoice_date__lte=today,
    )
    # Exclude contracts whose end_date has already passed
    qs = qs.filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
    return qs


def run_scheduled_generation(today: Optional[date] = None, *, system_actor=None) -> dict:
    """
    Walk every due contract and generate an invoice. Safe to call repeatedly
    (idempotent per period because next_invoice_date advances after each run).

    Does NOT send emails — use send_due_invoice_emails() for that.

    Returns a summary dict for logging.
    """
    today = today or timezone.localdate()
    actor = system_actor or _get_system_user()

    summary = {
        'date':      today.isoformat(),
        'attempted': 0,
        'succeeded': 0,
        'failed':    0,
        'errors':    [],   # [(contract_id, str)]
        'invoices':  [],   # [(contract_id, invoice_number)]
    }

    for contract in find_contracts_due(today):
        summary['attempted'] += 1
        try:
            result = generate_invoice_for_contract(
                contract,
                actor=actor,
                template=None,          # use contract.default_invoice_template
                finalize=True,
                period_start=contract.next_invoice_date,
                source='scheduled',
            )
            summary['succeeded'] += 1
            summary['invoices'].append((str(contract.pk), result.invoice.number))
        except Exception as e:
            summary['failed'] += 1
            summary['errors'].append((str(contract.pk), str(e)))

    return summary


# ── Due-date email sender ────────────────────────────────────────────────────

def find_invoices_due_today(today: Optional[date] = None):
    """
    Return ContractInvoiceLink rows whose invoice.due_date == today AND whose
    contract has auto_send_invoice=True AND invoice has a client_email.

    Called by the separate send_due_invoice_emails Celery task.
    """
    from apps.finance.models import ContractInvoiceLink
    from apps.invoices.models import InvoiceDocument
    today = today or timezone.localdate()

    return (
        ContractInvoiceLink.objects
        .filter(
            contract__auto_send_invoice=True,
            invoice__due_date=today,
            invoice__status=InvoiceDocument.Status.FINALIZED,
        )
        .exclude(invoice__client_email='')
        .select_related('contract', 'invoice', 'invoice__generated_pdf')
    )


def send_due_invoice_emails(today: Optional[date] = None) -> dict:
    """
    Send invoice PDFs to counterparties for every contract invoice whose
    due_date is today and whose contract has auto_send_invoice=True.

    Safe to call multiple times — tracking whether an email was already sent
    should be added if needed (e.g. add a `sent_at` field to ContractInvoiceLink).

    Returns a summary dict.
    """
    today = today or timezone.localdate()
    summary = {
        'date':      today.isoformat(),
        'attempted': 0,
        'sent':      0,
        'failed':    0,
        'errors':    [],
    }

    for link in find_invoices_due_today(today):
        summary['attempted'] += 1
        try:
            _send_invoice_email(link.invoice, link.contract)
            summary['sent'] += 1
        except Exception as e:
            summary['failed'] += 1
            summary['errors'].append((str(link.invoice.pk), str(e)))

    return summary


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_system_user():
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


def _send_invoice_email(invoice: InvoiceDocument, contract) -> None:
    """
    Email the finalized invoice PDF to the contract counterparty.
    Raises on failure — caller decides whether to swallow or propagate.
    """
    from django.core.mail import EmailMessage
    from django.conf import settings

    if not invoice.client_email:
        raise ValueError('No client email on invoice.')
    if not invoice.generated_pdf:
        raise ValueError('Invoice has no generated PDF.')

    subject = f'{invoice.get_doc_type_display()} {invoice.number} — {contract.title}'
    body = (
        f'Dear {invoice.client_name or "Customer"},\n\n'
        f'Please find attached {invoice.get_doc_type_display().lower()} '
        f'{invoice.number} for {contract.title}.\n\n'
        f'Total: {invoice.currency} {invoice.total}\n'
        f'Due:   {invoice.due_date:%B %d, %Y}\n\n'
        f'Thank you for your business.'
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