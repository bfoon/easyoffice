"""
apps/orders/services.py
───────────────────────
Business logic for sales orders. Views stay thin; everything that touches
multiple models or has policy implications lives here.

Document chain with CEO signature gates
═══════════════════════════════════════

    Order (CS/Sales creates)
        │
        ▼
    Confirmed   ◄── only Sales Supervisor / Manager / Admin / CEO
        │
        │  ─► email buyer "your order is confirmed"
        │
        ▼
    Generate Proforma  ◄── creator OR any orders-team member
        │
        │  → creates SignatureRequest addressed to the CEO
        │  → status = PROFORMA_PENDING_SIGNATURE
        │
        ▼  (CEO signs in the existing files-app signature UI)
    on_proforma_signed()  ◄── triggered by SignatureRequest completion
        │
        │  → email signed Proforma PDF to buyer
        │  → status = PROFORMA_SIGNED   (Invoice button now active)
        │
        ▼
    Generate Invoice  → CEO sig gate → on_invoice_signed → email buyer
        │
        ▼
    Generate Delivery Note → CEO sig gate → on_delivery_note_signed
        │
        ▼
    Fulfilled

Cancel can fire from any non-terminal state. Documents already issued are
preserved (they remain in the chain, marked as "issued before cancellation").
"""
from __future__ import annotations

import io
import logging
from decimal import Decimal
from typing import Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from django.db.models import Q

from .models import (
    SalesOrder, SalesOrderItem, OrderEvent, OrderStatus, OrderSource,
)
from . import notifications
from .permissions import find_ceo_user

logger = logging.getLogger(__name__)
User = get_user_model()


# ════════════════════════════════════════════════════════════════════════
# Order creation
# ════════════════════════════════════════════════════════════════════════

@transaction.atomic
def create_order_from_payload(payload: dict, *, source: str = OrderSource.PHONE,
                              actor=None) -> SalesOrder:
    """
    Build a SalesOrder + SalesOrderItems from a dict payload (used by both
    the agent UI and the public API).

    payload keys:
        customer_id, contact_name, contact_phone, contact_email,
        delivery_address, notes, currency, tax_rate, discount_amount,
        external_ref, idempotency_key, items (list of {description,
        quantity, unit_price})
    """
    items = payload.get('items') or []
    if not items:
        raise ValueError('Order must have at least one line item.')

    # Idempotency check
    idem = (payload.get('idempotency_key') or '').strip()
    if idem:
        existing = SalesOrder.objects.filter(idempotency_key=idem).first()
        if existing:
            return existing

    order = SalesOrder.objects.create(
        source=source,
        external_ref=payload.get('external_ref', '') or '',
        idempotency_key=idem,
        customer_id=payload.get('customer_id'),
        contact_name=payload.get('contact_name', '') or '',
        contact_phone=payload.get('contact_phone', '') or '',
        contact_email=payload.get('contact_email', '') or '',
        delivery_address=payload.get('delivery_address', '') or '',
        notes=payload.get('notes', '') or '',
        currency=payload.get('currency', 'GMD') or 'GMD',
        tax_rate=Decimal(str(payload.get('tax_rate') or 0)),
        discount_amount=Decimal(str(payload.get('discount_amount') or 0)),
        status=OrderStatus.NEW,
        created_by=actor if (actor and getattr(actor, 'is_authenticated', False)) else None,
    )

    for i, it in enumerate(items):
        SalesOrderItem.objects.create(
            order=order,
            position=i,
            description=str(it.get('description', '')).strip()[:300],
            quantity=Decimal(str(it.get('quantity') or 1)),
            unit_price=Decimal(str(it.get('unit_price') or 0)),
        )

    order.recalculate_totals(save=True)
    OrderEvent.objects.create(
        order=order, kind=OrderEvent.Kind.CREATED, actor=actor,
        message=f'Order created from {order.get_source_display()}',
        payload={'item_count': len(items), 'total': str(order.total)},
    )

    transaction.on_commit(lambda: notifications.notify_order_created(order, actor=actor))
    if not order.customer_id:
        transaction.on_commit(
            lambda: notifications.notify_order_needs_attention(
                order, reason='No customer linked at intake.',
            )
        )
    return order


# ════════════════════════════════════════════════════════════════════════
# Confirm  (NEW → CONFIRMED)
# ════════════════════════════════════════════════════════════════════════

@transaction.atomic
def confirm_order(order: SalesOrder, actor) -> SalesOrder:
    """
    Soft-confirm. Permission is checked by the view; this just enforces
    state machine + writes audit trail + sends notifications.
    """
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)
    if order.status == OrderStatus.CANCELLED:
        raise ValueError('Cancelled orders cannot be confirmed.')
    if order.status != OrderStatus.NEW:
        raise ValueError('Only new orders can be confirmed.')
    if not order.items.exists():
        raise ValueError('Cannot confirm an order with no items.')

    order.status = OrderStatus.CONFIRMED
    order.save(update_fields=['status', 'updated_at'])
    OrderEvent.objects.create(
        order=order, kind=OrderEvent.Kind.CONFIRMED, actor=actor,
        message='Order confirmed by supervisor.',
    )
    transaction.on_commit(lambda: notifications.notify_order_confirmed(order, actor=actor))
    return order


# ════════════════════════════════════════════════════════════════════════
# Document generation — each step issues a sig request to the CEO
# ════════════════════════════════════════════════════════════════════════
#
# Why we go through invoices.services.finalize_invoice():
#   It already writes the PDF to the Invoices folder as a SharedFile, which
#   is exactly the file we then attach to a SignatureRequest. One SharedFile,
#   one signed PDF — no duplication, no orphans.

def _exclude_generated_invoice_pdfs(qs):
    """
    Never use generated INV/PRO/DN documents as letterheads.
    This prevents old document content being used as the background
    for a newly generated invoice/proforma/delivery note.
    """
    qs = qs.exclude(folder__name__iexact='Invoices')
    for prefix in ('INV-', 'PRO-', 'DN-'):
        qs = qs.exclude(name__istartswith=prefix)
    return qs


def _resolve_default_invoice_template(actor, doc_type: str):
    """
    Prefer a real InvoiceTemplate for order-generated documents.
    This preserves the correct letterhead + layout_json from the builder.
    """
    try:
        from apps.invoices.models import InvoiceTemplate
    except Exception:
        return None

    qs = (
        InvoiceTemplate.objects
        .select_related('letterhead')
        .filter(letterhead__is_latest=True, letterhead__name__iendswith='.pdf')
        .filter(Q(doc_type=doc_type) | Q(doc_type=''))
    )

    if actor and getattr(actor, 'is_authenticated', False):
        if not actor.is_superuser:
            qs = qs.filter(Q(visibility=InvoiceTemplate.Visibility.SHARED) | Q(owner=actor))
    else:
        qs = qs.filter(visibility=InvoiceTemplate.Visibility.SHARED)

    exact = qs.filter(doc_type=doc_type).order_by('-updated_at').first()
    if exact:
        return exact

    return qs.filter(doc_type='').order_by('-updated_at').first()


def _resolve_default_letterhead(actor):
    """
    Pick only a real letterhead PDF.

    Important:
    Do NOT fall back to "any office PDF", because generated invoice PDFs
    are also office-visible and will cause text to overlap.
    """
    try:
        from apps.files.models import SharedFile
    except ImportError:
        return None

    base = SharedFile.objects.filter(is_latest=True, name__iendswith='.pdf')
    base = _exclude_generated_invoice_pdfs(base)

    hint = (
        Q(name__icontains='letterhead') |
        Q(description__icontains='letterhead') |
        Q(tags__icontains='letterhead') |
        Q(name__icontains='template') |
        Q(description__icontains='template') |
        Q(tags__icontains='template')
    )

    office_visible = Q(visibility=SharedFile.Visibility.OFFICE)

    # 1. Office-wide letterhead/template PDF
    lh = base.filter(office_visible & hint).order_by('-updated_at').first()
    if lh:
        return lh

    # 2. Actor-visible letterhead/template PDF
    if actor and getattr(actor, 'is_authenticated', False):
        if actor.is_superuser:
            return base.filter(hint).order_by('-updated_at').first()

        visible = (
            Q(uploaded_by=actor) |
            office_visible |
            Q(visibility=SharedFile.Visibility.SHARED_WITH, shared_with=actor) |
            Q(share_access__user=actor)
        )

        if hasattr(actor, 'unit_id') and actor.unit_id:
            visible |= Q(visibility=SharedFile.Visibility.UNIT, unit=actor.unit)

        if hasattr(actor, 'department_id') and actor.department_id:
            visible |= Q(visibility=SharedFile.Visibility.DEPARTMENT, department=actor.department)

        return base.filter(visible & hint).order_by('-updated_at').distinct().first()

    return None


def _build_doc_for_order(order: 'SalesOrder', doc_type: str, actor):
    """
    Build and finalize an InvoiceDocument from an order.

    Uses InvoiceTemplate first, so the order-generated document keeps the
    same letterhead and layout saved in the invoice generator template.
    """
    from apps.invoices.models import InvoiceDocument, InvoiceLineItem
    from apps.invoices.services import finalize_invoice

    template = _resolve_default_invoice_template(actor, doc_type)
    letterhead = template.letterhead if template else _resolve_default_letterhead(actor)

    if letterhead is None:
        raise ValueError(
            'No valid letterhead/template PDF is available. Upload a clean PDF '
            'letterhead and make sure its filename, description, or tags include '
            '"letterhead" or "template". Do not use generated INV/PRO/DN PDFs as letterheads.'
        )

    inv = InvoiceDocument.objects.create(
        doc_type=doc_type,
        letterhead=letterhead,
        template=template,
        layout_json=(template.layout_json if template else {}) or {},
        status=InvoiceDocument.Status.DRAFT,

        client_name=(
            (order.customer.display_name if order.customer_id else '')
            or order.contact_name
            or ''
        )[:180],
        client_address=order.delivery_address or '',
        client_email=order.contact_email or (
            getattr(order.customer, 'email', '') if order.customer_id else ''
        ),
        ship_to_address=order.delivery_address or '',

        invoice_date=timezone.localdate(),
        payment_terms=(template.default_payment_terms if template else 'Net 30') or 'Net 30',

        currency=order.currency,
        tax_rate=order.tax_rate,
        discount_amount=order.discount_amount,

        bank_details=(template.default_bank_details if template else ''),
        notes=order.notes or (template.default_notes if template else ''),

        created_by=actor,
    )

    for li in order.items.all().order_by('position', 'id'):
        InvoiceLineItem.objects.create(
            invoice=inv,
            position=li.position,
            description=li.description,
            quantity=li.quantity,
            unit_price=li.unit_price,
        )

    return finalize_invoice(inv, actor)


def _create_ceo_signature_request(order: 'SalesOrder', invoice_doc, *,
                                  doc_label: str, actor) -> 'SignatureRequest':
    """
    Create the files-app SignatureRequest AND the CEO signer row.
    Without SignatureRequestSigner, the CEO cannot open/sign the request.
    """
    from apps.files.models import SignatureRequest, SignatureRequestSigner, SignatureAuditEvent

    ceo = find_ceo_user()
    if ceo is None:
        raise ValueError(
            'No CEO user is configured. Add one active user to the "CEO" group.'
        )

    ceo_email = (getattr(ceo, 'email', '') or '').strip()
    if not ceo_email:
        raise ValueError(
            'The CEO user has no email address. Add an email address before sending documents for signature.'
        )

    shared_file = invoice_doc.generated_pdf
    if shared_file is None:
        raise ValueError(f'{doc_label} PDF is missing — the document failed to finalize.')

    metadata = {
        'orders.order_id': str(order.pk),
        'orders.stage': doc_label.lower(),
        'orders.invoice_id': str(invoice_doc.pk),
        'orders.document_number': invoice_doc.number,
    }

    create_kwargs = {
        'title': f'Sign {doc_label} — {invoice_doc.number} (Order {order.order_no})',
        'message': (
            f'Please review and sign this {doc_label.lower()} for '
            f'{order.customer.display_name if order.customer_id else order.contact_name or "customer"} '
            f'(Order {order.order_no}). Once signed, the document will be automatically emailed to the buyer.'
        ),
        'document': shared_file,
        'created_by': actor,
        'status': SignatureRequest.Status.SENT,
        'ordered_signing': False,
    }

    # Works after you add metadata field. Kept defensive in case migrations are not yet applied.
    if any(f.name == 'metadata' for f in SignatureRequest._meta.get_fields()):
        create_kwargs['metadata'] = metadata

    sig_req = SignatureRequest.objects.create(**create_kwargs)

    ceo_name = (
        getattr(ceo, 'full_name', '')
        or ceo.get_full_name()
        or getattr(ceo, 'username', '')
        or ceo_email
    )

    SignatureRequestSigner.objects.get_or_create(
        request=sig_req,
        user=ceo,
        defaults={
            'email': ceo_email,
            'name': ceo_name,
            'order': 1,
            'status': SignatureRequestSigner.Status.PENDING,
        },
    )

    try:
        SignatureAuditEvent.objects.create(
            request=sig_req,
            event='created',
            signer_email=ceo_email,
            signer_name=ceo_name,
            notes=f'Order-generated {doc_label}; waiting for CEO signature.',
        )
        SignatureAuditEvent.objects.create(
            request=sig_req,
            event='sent',
            signer_email=ceo_email,
            signer_name=ceo_name,
            notes='CEO signer row created automatically from orders service.',
        )
        sig_req.rebuild_audit_hash()
    except Exception:
        pass

    return sig_req

# ── Proforma ────────────────────────────────────────────────────────────────

@transaction.atomic
def generate_proforma_for_order(order: SalesOrder, actor) -> SalesOrder:
    """
    Build the Proforma PDF, attach it to the order, and queue a CEO signature
    request. The buyer is NOT emailed here — that happens in
    on_proforma_signed() once the CEO has signed.
    """
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)
    if order.status == OrderStatus.CANCELLED:
        raise ValueError('Cancelled orders cannot be fulfilled.')
    if order.status != OrderStatus.CONFIRMED:
        raise ValueError(
            'Order must be confirmed before generating a proforma.'
        )
    if order.proforma_id:
        raise ValueError('Proforma already generated for this order.')
    if not order.is_fulfillable:
        raise ValueError(
            'Order is not fulfillable. Attach a customer and ensure items exist.'
        )

    from apps.invoices.models import DocType
    proforma = _build_doc_for_order(order, DocType.PROFORMA, actor)
    sig = _create_ceo_signature_request(order, proforma, doc_label='Proforma', actor=actor)

    order.proforma = proforma
    order.status = OrderStatus.PROFORMA_PENDING_SIGNATURE
    order.fulfilled_by = actor
    order.save(update_fields=['proforma', 'status', 'fulfilled_by', 'updated_at'])

    OrderEvent.objects.create(
        order=order, kind=OrderEvent.Kind.DOC_GENERATED, actor=actor,
        message=f'Proforma {proforma.number} generated; sent to CEO for signature.',
        payload={'doc': 'proforma', 'doc_id': str(proforma.pk),
                 'signature_request_id': str(sig.pk)},
    )
    transaction.on_commit(
        lambda: notifications.notify_proforma_pending_signature(
            order, actor=actor, signature_request=sig,
        )
    )
    return order


# ── Invoice ─────────────────────────────────────────────────────────────────

@transaction.atomic
def generate_invoice_for_order(order: SalesOrder, actor) -> SalesOrder:
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)
    if order.status == OrderStatus.CANCELLED:
        raise ValueError('Cancelled orders cannot be invoiced.')
    if not order.proforma_id:
        raise ValueError('Generate the proforma first.')
    if order.status != OrderStatus.PROFORMA_SIGNED:
        raise ValueError(
            'The proforma must be signed by the CEO before invoicing.'
        )
    if order.invoice_id:
        raise ValueError('Invoice already generated for this order.')

    from apps.invoices.models import DocType
    invoice = _build_doc_for_order(order, DocType.INVOICE, actor)
    sig = _create_ceo_signature_request(order, invoice, doc_label='Invoice', actor=actor)

    order.invoice = invoice
    order.status = OrderStatus.INVOICE_PENDING_SIGNATURE
    order.save(update_fields=['invoice', 'status', 'updated_at'])

    OrderEvent.objects.create(
        order=order, kind=OrderEvent.Kind.DOC_GENERATED, actor=actor,
        message=f'Invoice {invoice.number} generated; sent to CEO for signature.',
        payload={'doc': 'invoice', 'doc_id': str(invoice.pk),
                 'signature_request_id': str(sig.pk)},
    )
    transaction.on_commit(
        lambda: notifications.notify_invoice_pending_signature(
            order, actor=actor, signature_request=sig,
        )
    )
    return order


# ── Delivery Note ───────────────────────────────────────────────────────────

@transaction.atomic
def generate_delivery_note_for_order(order: SalesOrder, actor) -> SalesOrder:
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)
    if order.status == OrderStatus.CANCELLED:
        raise ValueError('Cancelled orders cannot be fulfilled.')
    if not order.invoice_id:
        raise ValueError('Generate the invoice first.')
    if order.status != OrderStatus.INVOICE_SIGNED:
        raise ValueError(
            'The invoice must be signed by the CEO before issuing a '
            'delivery note.'
        )
    if order.delivery_note_id:
        raise ValueError('Delivery note already generated.')

    from apps.invoices.models import DocType
    dn = _build_doc_for_order(order, DocType.DELIVERY_NOTE, actor)
    sig = _create_ceo_signature_request(order, dn, doc_label='Delivery Note', actor=actor)

    order.delivery_note = dn
    order.status = OrderStatus.DN_PENDING_SIGNATURE
    order.save(update_fields=['delivery_note', 'status', 'updated_at'])

    OrderEvent.objects.create(
        order=order, kind=OrderEvent.Kind.DOC_GENERATED, actor=actor,
        message=f'Delivery Note {dn.number} generated; sent to CEO for signature.',
        payload={'doc': 'delivery_note', 'doc_id': str(dn.pk),
                 'signature_request_id': str(sig.pk)},
    )
    transaction.on_commit(
        lambda: notifications.notify_delivery_note_pending_signature(
            order, actor=actor, signature_request=sig,
        )
    )
    return order


# ════════════════════════════════════════════════════════════════════════
# Signature-completion hooks
# ════════════════════════════════════════════════════════════════════════
#
# These are invoked by a signal handler in apps/orders/signals.py that fires
# when the file-app's SignatureRequest is completed. They:
#   1. flip the order status to the next "signed" state
#   2. email the signed PDF to the buyer
#   3. record an OrderEvent
#
# Each is idempotent: if called twice for the same stage, the second call is
# a no-op. This protects against double-firing of the post_save signal.

def _read_signed_pdf_bytes(signature_request) -> Optional[bytes]:
    """
    Read the final signed PDF from the files signature flow.

    In this project, SignatureRequest uses:
      - document: SharedFile currently attached to the request
      - signed_document: optional FileField
    It does NOT use signature_request.file.
    """
    # 1. If signed_document FileField exists and has a file, use it first.
    signed_document = getattr(signature_request, 'signed_document', None)
    if signed_document and getattr(signed_document, 'name', ''):
        try:
            with signed_document.open('rb') as fh:
                return fh.read()
        except Exception:
            logger.exception(
                'Could not read signed_document for signature request %s',
                signature_request.pk,
            )

    # 2. Your signing flow creates a signed SharedFile and assigns it to document.
    shared_file = getattr(signature_request, 'document', None)
    if shared_file and getattr(shared_file, 'file', None):
        try:
            with shared_file.file.open('rb') as fh:
                return fh.read()
        except Exception:
            logger.exception(
                'Could not read signature request document for %s',
                signature_request.pk,
            )

    return None


@transaction.atomic
def on_proforma_signed(order: SalesOrder, *, signature_request, signer) -> SalesOrder:
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)

    if order.status != OrderStatus.PROFORMA_PENDING_SIGNATURE:
        return order

    pdf_bytes = _read_signed_pdf_bytes(signature_request)

    order.status = OrderStatus.PROFORMA_SIGNED
    order.save(update_fields=['status', 'updated_at'])

    OrderEvent.objects.create(
        order=order,
        kind=OrderEvent.Kind.DOC_SIGNED,
        actor=signer,
        message=f'Proforma {order.proforma.number} signed by CEO.',
        payload={
            'doc': 'proforma',
            'signature_request_id': str(signature_request.pk),
            'signed_file_id': str(signature_request.document_id) if getattr(signature_request, 'document_id', None) else '',
            'has_pdf_bytes': bool(pdf_bytes),
        },
    )

    transaction.on_commit(
        lambda: notifications.notify_proforma_ready(
            order,
            actor=signer,
            pdf_bytes=pdf_bytes,
        )
    )
    return order


@transaction.atomic
def on_invoice_signed(order: SalesOrder, *, signature_request, signer) -> SalesOrder:
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)

    if order.status != OrderStatus.INVOICE_PENDING_SIGNATURE:
        return order

    pdf_bytes = _read_signed_pdf_bytes(signature_request)

    order.status = OrderStatus.INVOICE_SIGNED
    order.save(update_fields=['status', 'updated_at'])

    OrderEvent.objects.create(
        order=order,
        kind=OrderEvent.Kind.DOC_SIGNED,
        actor=signer,
        message=f'Invoice {order.invoice.number} signed by CEO.',
        payload={
            'doc': 'invoice',
            'signature_request_id': str(signature_request.pk),
            'signed_file_id': str(signature_request.document_id) if getattr(signature_request, 'document_id', None) else '',
            'has_pdf_bytes': bool(pdf_bytes),
        },
    )

    transaction.on_commit(
        lambda: notifications.notify_invoice_issued(
            order,
            actor=signer,
            pdf_bytes=pdf_bytes,
        )
    )
    return order


@transaction.atomic
def on_delivery_note_signed(order: SalesOrder, *, signature_request, signer) -> SalesOrder:
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)

    if order.status != OrderStatus.DN_PENDING_SIGNATURE:
        return order

    pdf_bytes = _read_signed_pdf_bytes(signature_request)

    order.status = OrderStatus.FULFILLED
    order.fulfilled_at = timezone.now()
    if not order.fulfilled_by_id:
        order.fulfilled_by = signer

    order.save(update_fields=['status', 'fulfilled_at', 'fulfilled_by', 'updated_at'])

    OrderEvent.objects.create(
        order=order,
        kind=OrderEvent.Kind.FULFILLED,
        actor=signer,
        message=f'Delivery Note {order.delivery_note.number} signed by CEO; order fulfilled.',
        payload={
            'doc': 'delivery_note',
            'signature_request_id': str(signature_request.pk),
            'signed_file_id': str(signature_request.document_id) if getattr(signature_request, 'document_id', None) else '',
            'has_pdf_bytes': bool(pdf_bytes),
        },
    )

    transaction.on_commit(
        lambda: notifications.notify_delivery_note_issued(
            order,
            actor=signer,
            pdf_bytes=pdf_bytes,
        )
    )
    return order


# ════════════════════════════════════════════════════════════════════════
# Cancel / Attach customer
# ════════════════════════════════════════════════════════════════════════

@transaction.atomic
def cancel_order(order: SalesOrder, actor, *, reason: str = '') -> SalesOrder:
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)
    if order.status == OrderStatus.CANCELLED:
        raise ValueError('Order is already cancelled.')
    if order.status == OrderStatus.FULFILLED:
        raise ValueError('A fulfilled order cannot be cancelled.')

    order.status = OrderStatus.CANCELLED
    order.cancelled_reason = (reason or '')[:300]
    order.save(update_fields=['status', 'cancelled_reason', 'updated_at'])
    OrderEvent.objects.create(
        order=order, kind=OrderEvent.Kind.CANCELLED, actor=actor,
        message=reason or 'Cancelled.',
    )
    transaction.on_commit(
        lambda: notifications.notify_order_cancelled(order, actor=actor, reason=reason)
    )
    return order


@transaction.atomic
def attach_customer(order: SalesOrder, customer, actor) -> SalesOrder:
    order = SalesOrder.objects.select_for_update().get(pk=order.pk)
    if order.customer_id == customer.pk:
        return order
    order.customer = customer
    if not order.contact_email:
        order.contact_email = getattr(customer, 'email', '') or ''
    if not order.contact_phone:
        order.contact_phone = getattr(customer, 'phone', '') or ''
    if not order.contact_name:
        order.contact_name = getattr(customer, 'display_name', '') or getattr(customer, 'full_name', '') or ''
    order.save()
    OrderEvent.objects.create(
        order=order, kind=OrderEvent.Kind.CUSTOMER_LINK, actor=actor,
        message=f'Customer linked: {order.contact_name or customer.pk}',
    )
    transaction.on_commit(lambda: notifications.notify_customer_attached(order, actor=actor))
    return order