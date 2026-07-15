"""
apps/invoices/receipt_services.py
==================================

Receipt generation for finalized invoices. This keeps receipt creation
inside the invoice app, so IT maintenance, POS, finance, orders and
customer service can all use one implementation.

Design
──────
A Receipt is NOT a stage in the Proforma → Invoice → Delivery Note
conversion chain. It does not use `converted_from`, so issuing a receipt
never consumes the invoice's single `converted_to` slot — an invoice can
be receipted AND still converted to a Delivery Note afterwards.

  Proforma ──convert──> Invoice ──convert──> Delivery Note
                           │
                           └──receipt──> Receipt   (side branch)

Receipts:
  • Get their own RCT-YYYY-NNNN number from the shared InvoiceCounter.
  • Never create a receivable (`is_billable` is Invoice-only) — the money
    is already in; creating one would double-count revenue.
  • Never deduct stock (signals.py filters on doc_type == INVOICE).
  • Are terminal — they cannot be converted into anything else.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from .models import DocType, InvoiceDocument, InvoiceLineItem

log = logging.getLogger(__name__)


class ReceiptError(ValueError):
    """Raised when a receipt cannot be created from the given source."""


# ─────────────────────────────────────────────────────────────────────────────
#  Queries
# ─────────────────────────────────────────────────────────────────────────────

def get_receipt_for_invoice(source_invoice: InvoiceDocument):
    """
    Return the live (non-voided) receipt issued for this invoice, or None.

    Voided receipts are ignored so a bad receipt can be voided and re-issued
    without an admin having to delete rows.
    """
    if source_invoice is None or source_invoice.pk is None:
        return None
    return (
        InvoiceDocument.objects
        .filter(doc_type=DocType.RECEIPT, receipt_for=source_invoice)
        .exclude(status=InvoiceDocument.Status.VOIDED)
        .order_by('-created_at')
        .first()
    )


def get_all_receipts_for_invoice(source_invoice: InvoiceDocument):
    """Every receipt ever issued for this invoice, voided ones included."""
    if source_invoice is None or source_invoice.pk is None:
        return InvoiceDocument.objects.none()
    return (
        InvoiceDocument.objects
        .filter(doc_type=DocType.RECEIPT, receipt_for=source_invoice)
        .order_by('-created_at')
    )


def can_issue_receipt(source_invoice: InvoiceDocument) -> bool:
    """
    True if a receipt can be issued for this document right now.

    Rules:
      - Must be a document of type INVOICE (never a Proforma / Delivery
        Note / Receipt).
      - Must be finalized (a draft has no number to receipt against).
      - Must not be voided.
      - Must not already have a live receipt.
    """
    if source_invoice is None:
        return False
    if source_invoice.doc_type != DocType.INVOICE:
        return False
    if not source_invoice.is_finalized:
        return False
    if source_invoice.is_voided:
        return False
    return get_receipt_for_invoice(source_invoice) is None


# ─────────────────────────────────────────────────────────────────────────────
#  Creation
# ─────────────────────────────────────────────────────────────────────────────

@transaction.atomic
def create_receipt_from_invoice(
    source_invoice: InvoiceDocument,
    *,
    actor,
    amount_paid=None,
    payment_method='',
    payment_reference='',
    paid_at=None,
    notes='',
    finalize=True,
    allow_partial=False,
) -> InvoiceDocument:
    """
    Create a receipt using the same letterhead, customer and line items.

    Args:
        source_invoice:    the finalized INVOICE being receipted.
        actor:             the user issuing the receipt.
        amount_paid:       amount received. Defaults to the invoice's
                           recorded `paid_amount`, falling back to `total`.
        payment_method:    'cash', 'bank_transfer', 'cheque', etc. Falls
                           back to whatever is stamped on the invoice.
        payment_reference: bank ref / cheque no / txn id. Same fallback.
        paid_at:           datetime money was received (default: now).
        notes:             extra free text appended to the receipt notes.
        finalize:          allocate RCT-YYYY-NNNN + generate and file the
                           PDF immediately (default True).
        allow_partial:     permit a receipt for less than the full total
                           (part-payment acknowledgement).

    Returns:
        The receipt InvoiceDocument.

    Raises:
        ReceiptError on any invalid state.
    """
    # Lock the source row so two concurrent requests can't both pass the
    # duplicate check and issue two receipts for the same invoice.
    source_invoice = (
        InvoiceDocument.objects
        .select_for_update()
        .get(pk=source_invoice.pk)
    )

    if source_invoice.doc_type != DocType.INVOICE:
        raise ReceiptError(
            f'Receipts can only be issued for an Invoice — '
            f'{source_invoice.get_doc_type_display()} is not receiptable.'
        )
    if not source_invoice.is_finalized:
        raise ReceiptError('Only a finalized invoice can be receipted.')
    if source_invoice.is_voided:
        raise ReceiptError('A voided invoice cannot be receipted.')

    existing = get_receipt_for_invoice(source_invoice)
    if existing is not None:
        raise ReceiptError(
            f'A receipt ({existing.number or "draft"}) already exists for '
            f'{source_invoice.number}. Void it first if you need to re-issue.'
        )

    paid_at = paid_at or timezone.now()

    # Default to whatever was actually recorded as paid on the invoice.
    if amount_paid is None:
        amount_paid = source_invoice.paid_amount or source_invoice.total
    try:
        paid = Decimal(str(amount_paid))
    except (InvalidOperation, TypeError, ValueError):
        raise ReceiptError('Invalid payment amount.')

    if paid <= 0:
        raise ReceiptError('Receipt amount must be greater than zero.')

    invoice_total = source_invoice.total or Decimal('0.00')
    if paid < invoice_total and not allow_partial:
        raise ReceiptError(
            f'Amount ({paid} {source_invoice.currency}) is less than the '
            f'invoice total ({invoice_total} {source_invoice.currency}). '
            f'Tick "allow part-payment" to issue a part-payment receipt.'
        )

    method = (payment_method or source_invoice.payment_method or '').strip()
    reference = (payment_reference or source_invoice.payment_reference or '').strip()
    balance = invoice_total - paid

    note_lines = [
        f'RECEIPT FOR: {source_invoice.number or source_invoice.preview_number}',
        f'Amount received: {paid} {source_invoice.currency}',
        f'Payment method: {method or "Not specified"}',
        f'Payment reference: {reference or "N/A"}',
        f'Received on: {timezone.localtime(paid_at):%d %b %Y %H:%M}',
    ]
    if balance > 0:
        note_lines.append(
            f'Balance outstanding: {balance} {source_invoice.currency}'
        )
    else:
        note_lines.append('PAID IN FULL — received with thanks.')
    if notes:
        note_lines.append('')
        note_lines.append(notes)

    receipt = InvoiceDocument.objects.create(
        doc_type=DocType.RECEIPT,
        letterhead=source_invoice.letterhead,
        layout_json=source_invoice.layout_json or {},
        template=source_invoice.template,
        status=InvoiceDocument.Status.DRAFT,

        client_name=source_invoice.client_name,
        client_address=source_invoice.client_address,
        client_email=source_invoice.client_email,
        ship_to_address=source_invoice.ship_to_address,
        po_reference=source_invoice.po_reference,

        invoice_date=timezone.localdate(),
        due_date=None,               # a receipt is never "due"
        payment_terms='PAID',

        currency=source_invoice.currency,
        tax_rate=source_invoice.tax_rate,
        discount_amount=source_invoice.discount_amount,

        bank_details=source_invoice.bank_details,
        notes='\n'.join(note_lines).strip(),

        # Receipt-specific linkage + payment stamps.
        receipt_for=source_invoice,
        paid_at=paid_at,
        paid_by=actor,
        paid_amount=paid,
        payment_method=method[:30],
        payment_reference=reference[:100],

        created_by=actor,
    )

    # Mirror the invoice's line items so the receipt itemises what was paid for.
    for line in source_invoice.items.all().order_by('position', 'id'):
        InvoiceLineItem.objects.create(
            invoice=receipt,
            position=line.position,
            description=line.description,
            quantity=line.quantity,
            unit_price=line.unit_price,
        )
    receipt.recalculate_totals(save=True)

    if finalize:
        # Imported here, not at module level, to avoid a circular import:
        # services imports models, and this module is imported by services.
        from .services import finalize_invoice
        receipt = finalize_invoice(receipt, actor)

    log.info(
        'Receipt %s issued for invoice %s (%s %s) by %s',
        receipt.number or receipt.pk, source_invoice.number,
        paid, source_invoice.currency, actor,
    )
    return receipt


@transaction.atomic
def void_receipt(receipt: InvoiceDocument, actor, reason: str = '') -> InvoiceDocument:
    """
    Void a receipt so a corrected one can be issued for the same invoice.
    The number stays reserved — receipts are financial records.
    """
    if receipt.doc_type != DocType.RECEIPT:
        raise ReceiptError('void_receipt() only accepts Receipt documents.')
    if receipt.is_voided:
        return receipt
    if not receipt.is_finalized:
        raise ReceiptError('Only a finalized receipt can be voided.')

    receipt.status = InvoiceDocument.Status.VOIDED
    receipt.voided_at = timezone.now()
    receipt.voided_by = actor
    receipt.void_reason = (reason or 'Voided to re-issue')[:300]
    receipt.save(update_fields=[
        'status', 'voided_at', 'voided_by', 'void_reason', 'updated_at',
    ])
    log.info('Receipt %s voided by %s', receipt.number, actor)
    return receipt
