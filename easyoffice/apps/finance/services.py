"""
Service layer for invoices.

Keeps views thin. Handles:
    - Auto-creating the system "Invoices" folder on first use and sharing it
      with CEO / Sales / HR group members.
    - Finalizing an invoice: generates PDF, saves it as a SharedFile in the
      Invoices folder, updates invoice status, and (for billable invoices)
      auto-creates a matching IncomingPaymentRequest in the finance app.
    - Marking invoices paid: flips the receivable to PAID, creates a Payment
      ledger entry, denormalizes paid_* fields on the invoice. All side
      effects are audit-logged via apps/finance/signals.py.
    - Templates: save-as-template, create-from-template, push-template.
    - Conversion chain: Proforma → Invoice → Delivery Note.
"""
import io
import logging
from datetime import date as _date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import transaction, models
from django.utils import timezone

from apps.files.models import (
    FileFolder, SharedFile, FolderShareAccess, SharePermission,
)
from .permissions import INVOICE_ACCESS_GROUPS
from .models import InvoiceDocument, InvoiceTemplate, InvoiceLineItem, DocType
from .pdf_generator import build_invoice_pdf


User = get_user_model()
log = logging.getLogger(__name__)

INVOICES_FOLDER_NAME = 'Invoices'


# ─────────────────────────────────────────────────────────────────────────────
#  Folder bootstrapping
# ─────────────────────────────────────────────────────────────────────────────

def get_or_create_invoices_folder(actor):
    """
    Return the system-wide Invoices folder, creating it if missing.
    On creation, it's made OFFICE-wide visible and each user in the CEO/Sales/HR
    groups is granted FULL access via FileShareAccess so they can put files inside it.
    """
    folder = FileFolder.objects.filter(
        parent__isnull=True,
        name=INVOICES_FOLDER_NAME,
    ).first()
    if folder:
        return folder

    # Create fresh
    folder = FileFolder.objects.create(
        name=INVOICES_FOLDER_NAME,
        owner=actor,
        parent=None,
        visibility=FileFolder.Visibility.OFFICE,
        share_children=True,
        color='#0ea5e9',
    )

    # Grant FULL access to every user in the privileged groups
    # (defensive: group names may be lowercase variants)
    group_users = User.objects.filter(
        groups__name__in=INVOICE_ACCESS_GROUPS
    ).distinct()
    # Also include case-insensitive matches
    ci_users = User.objects.filter(
        groups__name__iregex=r'^(CEO|Sales|HR|Admin)$'
    ).distinct()
    eligible = (group_users | ci_users).distinct()

    for u in eligible:
        FolderShareAccess.objects.get_or_create(
            folder=folder, user=u,
            defaults={
                'permission': SharePermission.FULL,
                'granted_by': actor,
            },
        )
    return folder


# ─────────────────────────────────────────────────────────────────────────────
#  Finalize / Preview / Void
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def finalize_invoice(invoice: InvoiceDocument, actor) -> InvoiceDocument:
    """
    Atomically:
        1. Lock this invoice row
        2. Recalculate totals
        3. Allocate the invoice number (if not yet assigned)
        4. Generate the PDF
        5. Save it as a SharedFile in the Invoices folder
        6. Flip status to FINALIZED
        7. For billable invoices only: auto-create a linked
           IncomingPaymentRequest in the finance app so this invoice
           shows up in AR/ageing/revenue analytics.
    Returns the updated invoice.
    """
    # Re-select with lock
    invoice = InvoiceDocument.objects.select_for_update().get(pk=invoice.pk)
    if invoice.status == InvoiceDocument.Status.FINALIZED:
        return invoice

    if not invoice.items.exists():
        raise ValueError('Cannot finalize an invoice with no line items.')

    invoice.recalculate_totals(save=False)
    invoice.allocate_number()

    pdf_bytes = build_invoice_pdf(invoice)

    folder = get_or_create_invoices_folder(actor)
    filename = f'{invoice.number}.pdf'

    shared = SharedFile(
        name=filename,
        folder=folder,
        uploaded_by=actor,
        visibility=SharedFile.Visibility.OFFICE,
        file_size=len(pdf_bytes),
        file_type='application/pdf',
        description=f'{invoice.get_doc_type_display()} for {invoice.client_name or "—"}',
    )
    shared.file.save(filename, ContentFile(pdf_bytes), save=False)
    shared.save()
    # Best-effort hash
    try:
        shared.file_hash = shared.compute_hash()
        shared.save(update_fields=['file_hash'])
    except Exception:
        pass

    invoice.generated_pdf = shared
    invoice.status = InvoiceDocument.Status.FINALIZED
    invoice.finalized_at = timezone.now()
    invoice.save()

    # ── Bridge into finance for billable docs ───────────────────────────────
    # Proformas and Delivery Notes are skipped (not billable, not real revenue).
    # Failures here MUST NOT block finalization — a missing receivable can
    # always be back-filled later by calling create_receivable_for_invoice().
    if invoice.is_billable and invoice.linked_receivable_id is None:
        try:
            create_receivable_for_invoice(invoice, actor)
        except Exception:
            log.exception(
                'Failed to create receivable for invoice %s', invoice.pk
            )

    return invoice


def build_preview_pdf(invoice: InvoiceDocument) -> bytes:
    """Draft preview — NOT saved anywhere. Recalculates totals on the fly."""
    invoice.recalculate_totals(save=False)
    return build_invoice_pdf(invoice)


def void_invoice(invoice: InvoiceDocument, actor, reason: str = ''):
    """Mark a finalized invoice as voided. Number is preserved."""
    if invoice.status != InvoiceDocument.Status.FINALIZED:
        raise ValueError('Only finalized invoices can be voided.')
    invoice.status = InvoiceDocument.Status.VOIDED
    invoice.voided_at = timezone.now()
    invoice.voided_by = actor
    invoice.void_reason = reason[:300]
    invoice.save(update_fields=['status', 'voided_at', 'voided_by', 'void_reason', 'updated_at'])
    # The post_save signal in apps/invoices/signals.py picks up the void
    # transition and calls sync_invoice_payment_state() to cancel the
    # linked receivable. We don't need to call it directly here.
    return invoice


# ─────────────────────────────────────────────────────────────────────────────
#  Receivable bridge — auto-creates an IncomingPaymentRequest on finalize
# ─────────────────────────────────────────────────────────────────────────────

def create_receivable_for_invoice(invoice: InvoiceDocument, actor):
    """
    Create an IncomingPaymentRequest mirroring this finalized invoice. Idempotent:
    if invoice.linked_receivable already exists, returns it.

    The receivable is created in SENT status (the invoice has been issued to
    the client). It carries the same customer info, dates, currency, and
    pre-tax / tax / discount split, so all existing analytics queries pick
    it up automatically.
    """
    from apps.finance.models import IncomingPaymentRequest

    if invoice.linked_receivable_id:
        return invoice.linked_receivable

    if not invoice.is_billable:
        raise ValueError(
            f'{invoice.get_doc_type_display()} documents are not billable.'
        )
    if not invoice.is_finalized:
        raise ValueError('Only finalized invoices generate receivables.')

    # Pre-tax subtotal goes in `amount`, tax in `tax_amount`, discount as-is.
    # `discount_amount` on IncomingPaymentRequest is a positive deduction.
    receivable = IncomingPaymentRequest.objects.create(
        invoice_number   = invoice.number,
        title            = f'{invoice.get_doc_type_display()} {invoice.number}',
        customer_name    = invoice.client_name or '(unspecified)',
        customer_email   = invoice.client_email or '',
        amount           = invoice.subtotal,
        tax_amount       = invoice.tax_amount or Decimal('0.00'),
        discount_amount  = invoice.discount_amount or Decimal('0.00'),
        currency         = invoice.currency,
        issue_date       = invoice.invoice_date,
        due_date         = invoice.due_date,
        status           = IncomingPaymentRequest.Status.SENT,
        created_by       = actor,
        notes            = (
            f'Auto-generated from invoice document {invoice.number} '
            f'(InvoiceDocument id={invoice.pk}).'
        ),
    )

    # Link both directions
    InvoiceDocument.objects.filter(pk=invoice.pk).update(
        linked_receivable=receivable,
    )
    invoice.linked_receivable = receivable
    return receivable


def _create_payment_for_invoice(invoice: InvoiceDocument, actor, *,
                                amount, method, reference, paid_on):
    """
    Internal: create a Payment ledger row (PAYMENT_IN) for a paid invoice.
    Returns the Payment instance.
    """
    from apps.finance.models import Payment

    # Pick the correct "incoming" direction value. If your Payment model uses
    # a Direction enum, prefer that; otherwise fall back to the string 'in'.
    direction_value = 'in'
    if hasattr(Payment, 'Direction') and hasattr(Payment.Direction, 'IN'):
        direction_value = Payment.Direction.IN

    # Build a robust reference. Falls back if either side is missing.
    payment_ref = reference or f'INV-{invoice.number or invoice.pk}'

    # Build kwargs — only include user field if Payment model supports it.
    payment_kwargs = dict(
        reference     = payment_ref[:100],
        amount        = amount,
        currency      = invoice.currency,
        method        = method or 'bank_transfer',
        direction     = direction_value,
        payment_type  = 'invoice',
        recipient     = invoice.client_name or '(unspecified)',
        payment_date  = paid_on,
        notes         = (
            f'Receipt for invoice {invoice.number} '
            f'(InvoiceDocument id={invoice.pk}).'
            + (f' Ref: {reference}.' if reference else '')
        ),
    )
    # Payment models vary — try common actor field names in priority order.
    _field_names = {f.name for f in Payment._meta.get_fields()}
    for _actor_field in ('processed_by', 'created_by', 'recorded_by', 'paid_by', 'actor'):
        if _actor_field in _field_names:
            payment_kwargs[_actor_field] = actor
            break

    payment = Payment.objects.create(**payment_kwargs)
    return payment


@transaction.atomic
def mark_invoice_paid(invoice: InvoiceDocument,
                     actor,
                     *,
                     amount=None,
                     method: str = 'bank_transfer',
                     reference: str = '',
                     paid_on=None,
                     create_payment: bool = True) -> InvoiceDocument:
    """
    Mark a finalized billable invoice as paid. Atomically:
        1. Lock the invoice row.
        2. Validate state (must be billable, finalized, not voided, not paid).
        3. If no linked receivable yet (legacy invoices finalized before the
           bridge existed), create one.
        4. Flip the linked IncomingPaymentRequest to PAID and stamp paid_at.
        5. (Optional) create a Payment ledger entry.
        6. Update the invoice's denormalized paid_* fields.

    All side-effects fire signals — so audit logs and anomaly checks happen
    automatically via apps/finance/signals.py without us calling them here.

    Returns the updated InvoiceDocument.

    Raises ValueError if the invoice is not in a state to be marked paid.
    """
    from apps.finance.models import IncomingPaymentRequest

    invoice = InvoiceDocument.objects.select_for_update().get(pk=invoice.pk)

    if not invoice.is_billable:
        raise ValueError(
            f'{invoice.get_doc_type_display()} documents cannot be marked paid.'
        )
    if invoice.is_voided:
        raise ValueError('Voided invoices cannot be marked paid.')
    if not invoice.is_finalized:
        raise ValueError('Only finalized invoices can be marked paid.')
    if invoice.is_paid:
        # Idempotent: just return.
        return invoice

    # Defaults
    paid_on = paid_on or timezone.localdate()
    amount = Decimal(str(amount)) if amount is not None else (invoice.total or Decimal('0.00'))
    if amount <= 0:
        raise ValueError('Payment amount must be greater than zero.')

    # Make sure a receivable exists. Legacy invoices finalized before the
    # bridge was added won't have one — back-fill it now.
    if not invoice.linked_receivable_id:
        create_receivable_for_invoice(invoice, actor)
        invoice.refresh_from_db(fields=['linked_receivable'])

    receivable = invoice.linked_receivable
    if receivable:
        # Re-select with lock to avoid races with another mark-paid action.
        receivable = (
            IncomingPaymentRequest.objects
            .select_for_update()
            .get(pk=receivable.pk)
        )
        if receivable.status != IncomingPaymentRequest.Status.PAID:
            receivable.status = IncomingPaymentRequest.Status.PAID
            receivable.paid_at = timezone.now()
            update_fields = ['status', 'paid_at']
            if hasattr(receivable, 'updated_at'):
                update_fields.append('updated_at')
            receivable.save(update_fields=update_fields)

    # Optionally create the Payment ledger entry.
    payment = None
    if create_payment:
        payment = _create_payment_for_invoice(
            invoice, actor,
            amount=amount, method=method, reference=reference, paid_on=paid_on,
        )

    # Stamp the invoice
    invoice.paid_at           = timezone.now()
    invoice.paid_by           = actor
    invoice.paid_amount       = amount
    invoice.payment_method    = (method or '')[:30]
    invoice.payment_reference = (reference or '')[:100]
    if payment:
        invoice.linked_payment = payment
    invoice.save(update_fields=[
        'paid_at', 'paid_by', 'paid_amount',
        'payment_method', 'payment_reference', 'linked_payment',
        'updated_at',
    ])

    return invoice


def sync_invoice_payment_state(invoice: InvoiceDocument):
    """
    Called by a signal handler when an invoice is voided. If the invoice was
    paid or has a linked receivable, we DON'T delete the historical Payment
    or receivable rows (they're financial records). We just mark the
    receivable as CANCELLED so it stops appearing in AR/ageing.

    Returns nothing.
    """
    from apps.finance.models import IncomingPaymentRequest

    if not invoice.linked_receivable_id:
        return

    rec = invoice.linked_receivable
    # Only flip if not already paid — paid receivables are real revenue and
    # shouldn't be retroactively cancelled. If you DO need to cancel a paid
    # receivable, that's a refund flow, not a void.
    if rec.status not in (
        IncomingPaymentRequest.Status.PAID,
        IncomingPaymentRequest.Status.CANCELLED,
    ):
        rec.status = IncomingPaymentRequest.Status.CANCELLED
        update_fields = ['status']
        if hasattr(rec, 'updated_at'):
            update_fields.append('updated_at')
        rec.save(update_fields=update_fields)


# ─────────────────────────────────────────────────────────────────────────────
#  Templates
# ─────────────────────────────────────────────────────────────────────────────

def save_invoice_as_template(invoice: InvoiceDocument, user, *,
                             name, description='', visibility='personal',
                             locked_layout=False, doc_type=''):
    """
    Create a new InvoiceTemplate from a draft/finalized invoice.
    Captures: letterhead + layout + currency + tax_rate + payment_terms +
    bank_details + notes. Does NOT capture: client info, items, dates.
    """
    tmpl = InvoiceTemplate.objects.create(
        name=name[:150],
        description=description[:300],
        letterhead=invoice.letterhead,
        layout_json=invoice.layout_json or {},
        locked_layout=bool(locked_layout),
        doc_type=doc_type if doc_type in DocType.values else '',
        default_currency=invoice.currency,
        default_tax_rate=invoice.tax_rate,
        default_payment_terms=invoice.payment_terms,
        default_bank_details=invoice.bank_details,
        default_notes=invoice.notes,
        visibility=visibility if visibility in ('personal', 'shared') else 'personal',
        owner=user,
    )
    return tmpl


def create_invoice_from_template(template: InvoiceTemplate, user) -> InvoiceDocument:
    """
    Build a fresh draft invoice using the template's letterhead, layout and
    defaults. Client info and line items are left blank.
    """
    doc_type = template.doc_type or DocType.INVOICE
    invoice = InvoiceDocument.objects.create(
        doc_type=doc_type,
        letterhead=template.letterhead,
        layout_json=template.layout_json or {},
        status=InvoiceDocument.Status.DRAFT,
        currency=template.default_currency,
        tax_rate=template.default_tax_rate,
        payment_terms=template.default_payment_terms,
        bank_details=template.default_bank_details,
        notes=template.default_notes,
        created_by=user,
        template=template,
    )
    # Seed with one empty line so the items table isn't empty on first load
    InvoiceLineItem.objects.create(
        invoice=invoice, position=0,
        description='', quantity=Decimal('1.00'), unit_price=Decimal('0.00'),
    )
    # Bump usage counter
    InvoiceTemplate.objects.filter(pk=template.pk).update(
        usage_count=models.F('usage_count') + 1,
    )
    return invoice


def apply_template_update_to_invoices(template: InvoiceTemplate):
    """
    Retroactively push the template's current layout + defaults to every DRAFT
    invoice that was created from it. Finalized/voided invoices are NOT
    touched — they are already locked-in records.

    Returns the number of invoices updated.
    """
    qs = InvoiceDocument.objects.filter(
        template=template,
        status=InvoiceDocument.Status.DRAFT,
    )
    count = 0
    for inv in qs:
        inv.layout_json = template.layout_json or {}
        # Only update defaults that are "template-owned" — don't overwrite
        # values the user has actively edited on their draft. Heuristic:
        # copy the template value only when the invoice's current value
        # equals the template's previous default. Since we don't track
        # "previous", we take a simpler approach: always push layout
        # (positions are unambiguously template-owned), but leave bank
        # details/terms/notes alone unless they were empty.
        if not (inv.bank_details or '').strip():
            inv.bank_details = template.default_bank_details
        if not (inv.payment_terms or '').strip():
            inv.payment_terms = template.default_payment_terms
        if not (inv.notes or '').strip():
            inv.notes = template.default_notes
        inv.save(update_fields=[
            'layout_json', 'bank_details', 'payment_terms', 'notes', 'updated_at',
        ])
        count += 1
    return count


# ─────────────────────────────────────────────────────────────────────────────
#  Conversion chain (Proforma → Invoice → Delivery Note)
# ─────────────────────────────────────────────────────────────────────────────

NEXT_DOC_TYPE = {
    DocType.PROFORMA:      DocType.INVOICE,
    DocType.INVOICE:       DocType.DELIVERY_NOTE,
    DocType.DELIVERY_NOTE: None,
}


@transaction.atomic
def convert_invoice(source: InvoiceDocument, actor, new_letterhead=None) -> InvoiceDocument:
    """
    Convert a finalized Proforma → Invoice, or Invoice → Delivery Note.

    The child draft inherits:
        - Letterhead (or the user-provided replacement via `new_letterhead`)
        - Layout (positions)
        - Template link (so lock flags are inherited)
        - Client info, ship-to, currency, tax, discount, payment terms
        - Bank details, notes
        - All line items

    The child starts fresh on:
        - Number, sequence, year, status (draft, no number until finalize)
        - Dates (invoice_date = today, due_date blank)
        - PO reference (blank — usually a new one for each stage)

    Raises ValueError if source is not convertible.
    """
    # Re-fetch with lock to prevent two simultaneous conversions
    source = InvoiceDocument.objects.select_for_update().get(pk=source.pk)

    if source.status != InvoiceDocument.Status.FINALIZED:
        raise ValueError('Only finalized documents can be converted.')
    if source.is_converted_source:
        raise ValueError('This document has already been converted.')
    target_type = NEXT_DOC_TYPE.get(source.doc_type)
    if target_type is None:
        raise ValueError(f'{source.get_doc_type_display()} cannot be converted further.')

    letterhead = new_letterhead or source.letterhead

    child = InvoiceDocument.objects.create(
        doc_type=target_type,
        letterhead=letterhead,
        layout_json=source.layout_json or {},
        template=source.template,      # inherit locks from the same template
        status=InvoiceDocument.Status.DRAFT,

        client_name=source.client_name,
        client_address=source.client_address,
        client_email=source.client_email,
        ship_to_address=source.ship_to_address,
        po_reference='',                # fresh PO per stage

        invoice_date=timezone.localdate(),
        due_date=None,
        payment_terms=source.payment_terms,

        currency=source.currency,
        tax_rate=source.tax_rate,
        discount_amount=source.discount_amount,

        bank_details=source.bank_details,
        notes=source.notes,

        created_by=actor,
        converted_from=source,
    )

    # Copy all line items
    for li in source.items.all().order_by('position', 'id'):
        InvoiceLineItem.objects.create(
            invoice=child,
            position=li.position,
            description=li.description,
            quantity=li.quantity,
            unit_price=li.unit_price,
        )

    child.recalculate_totals(save=True)
    return child