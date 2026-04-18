"""
Service layer for invoices.

Keeps views thin. Handles:
    - Auto-creating the system "Invoices" folder on first use and sharing it
      with CEO / Sales / HR group members.
    - Finalizing an invoice: generates PDF, saves it as a SharedFile in the
      Invoices folder, updates invoice status.
"""
import io
from django.core.files.base import ContentFile
from django.db import transaction, models
from django.utils import timezone
from django.contrib.auth import get_user_model

from apps.files.models import (
    FileFolder, SharedFile, FolderShareAccess, SharePermission,
)
from .permissions import INVOICE_ACCESS_GROUPS
from .models import InvoiceDocument
from .pdf_generator import build_invoice_pdf


User = get_user_model()

INVOICES_FOLDER_NAME = 'Invoices'


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
    return invoice


# ── Templates ────────────────────────────────────────────────────────────────

from .models import InvoiceTemplate, InvoiceLineItem, DocType
from decimal import Decimal


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


# ── Conversion chain (Proforma → Invoice → Delivery Note) ────────────────────

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
    from .models import InvoiceLineItem as _LI
    for li in source.items.all().order_by('position', 'id'):
        _LI.objects.create(
            invoice=child,
            position=li.position,
            description=li.description,
            quantity=li.quantity,
            unit_price=li.unit_price,
        )

    child.recalculate_totals(save=True)
    return child