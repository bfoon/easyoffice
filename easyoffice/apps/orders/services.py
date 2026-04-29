"""
apps/orders/services.py
───────────────────────
Business logic for sales orders.

Two main flows:

1.  resolve_or_create_customer(...)
    Used by the public API to attach an incoming website order to an
    existing Customer. Lookup order:
        a. Match on CustomerPhone.normalized_number
        b. Match on Customer.email or Customer.alternate_email
        c. Create a new Customer (+ primary CustomerPhone) on miss
    No fuzzy matching — an agent can manually merge later if needed.

2.  Step-by-step document flow
        a. fulfill_order(order, actor) creates ONLY the finalized Proforma
        b. generate_invoice_for_order(order, actor) creates the Invoice
        c. generate_delivery_note_for_order(order, actor) creates the Delivery Note
    Each step is wrapped in transaction.atomic() so partial failures roll back.
"""
from decimal import Decimal
from django.db import transaction
from django.utils import timezone

from .models import (
    SalesOrder, SalesOrderItem, OrderEvent, OrderStatus,
)


# ── Customer resolution ─────────────────────────────────────────────────────

def resolve_or_create_customer(*, phone='', email='', name='', address=''):
    """
    Find or create a Customer for an incoming website/API order.

    Returns (customer, was_created: bool, match_field: str).
    `match_field` is one of: 'phone', 'email', 'created'.

    Side effects:
        • If a phone number is provided and no CustomerPhone matches, the
          number is added to the matched/created customer as a secondary.
        • A primary CustomerPhone is always created when we create a new
          customer with a phone.
    """
    # Local imports keep this file from blowing up at startup if the
    # customer_service app's model layout shifts — they're fetched lazily.
    from apps.customer_service.models import (
        Customer, CustomerPhone, normalize_phone,
    )

    # 1. Phone match first (most reliable for our market)
    if phone:
        normalized = normalize_phone(phone)
        if normalized:
            match = (
                CustomerPhone.objects
                .filter(normalized_number=normalized, is_active=True)
                .select_related('customer')
                .first()
            )
            if match:
                return match.customer, False, 'phone'

    # 2. Email match (primary OR alternate)
    if email:
        email_lc = email.strip().lower()
        from django.db.models import Q
        cust = Customer.objects.filter(
            Q(email__iexact=email_lc) | Q(alternate_email__iexact=email_lc)
        ).first()
        if cust:
            # Bonus: if they gave us a phone we don't have on file, store it
            if phone:
                normalized = normalize_phone(phone)
                if normalized and not cust.phones.filter(normalized_number=normalized).exists():
                    CustomerPhone.objects.create(
                        customer=cust, phone_number=phone,
                        phone_type='secondary', is_active=True,
                    )
            return cust, False, 'email'

    # 3. Create
    cust = Customer.objects.create(
        full_name=(name or 'Website Customer').strip()[:180],
        email=(email or '').strip().lower()[:254],
        address=(address or '').strip(),
    )
    if phone:
        CustomerPhone.objects.create(
            customer=cust, phone_number=phone,
            phone_type='primary', is_primary=True, is_active=True,
        )
    return cust, True, 'created'


# ── Order creation ──────────────────────────────────────────────────────────

@transaction.atomic
def create_order_from_payload(payload, *, source, actor=None):
    """
    Create a SalesOrder + items from a normalised payload dict.
    Used by both the API serializer and the internal "agent creates order" view.

    payload shape:
        {
            'customer_id'      : int | None,        # already-known customer
            'contact_name'     : str,
            'contact_phone'    : str,
            'contact_email'    : str,
            'delivery_address' : str,
            'notes'            : str,
            'currency'         : 'GMD',
            'tax_rate'         : Decimal | str,
            'discount_amount'  : Decimal | str,
            'external_ref'     : str,
            'idempotency_key'  : str,
            'items'            : [
                {'description': str, 'quantity': Decimal, 'unit_price': Decimal},
                ...
            ],
        }

    For website/API submissions the caller should also pass `customer_id=None`
    and set contact_phone/contact_email so resolve_or_create_customer() can
    match. We do that lookup HERE rather than in the serializer so the
    internal agent flow gets the same logic.
    """
    customer = None
    customer_id = payload.get('customer_id')
    if customer_id:
        from apps.customer_service.models import Customer
        customer = Customer.objects.filter(pk=customer_id).first()

    # Auto-resolve only for non-internal sources where the caller didn't
    # already pick a customer
    if customer is None and source in ('website', 'api', 'email'):
        customer, _, _ = resolve_or_create_customer(
            phone=payload.get('contact_phone', ''),
            email=payload.get('contact_email', ''),
            name=payload.get('contact_name', ''),
            address=payload.get('delivery_address', ''),
        )

    order = SalesOrder.objects.create(
        source=source,
        customer=customer,
        contact_name=(payload.get('contact_name') or '')[:180],
        contact_phone=(payload.get('contact_phone') or '')[:30],
        contact_email=(payload.get('contact_email') or '')[:254],
        delivery_address=payload.get('delivery_address') or '',
        notes=payload.get('notes') or '',
        currency=(payload.get('currency') or 'GMD')[:3].upper(),
        tax_rate=Decimal(str(payload.get('tax_rate') or '0')),
        discount_amount=Decimal(str(payload.get('discount_amount') or '0')),
        external_ref=(payload.get('external_ref') or '')[:80],
        idempotency_key=(payload.get('idempotency_key') or '')[:80],
        created_by=actor,
    )

    # Create line items in the order they were submitted
    for i, raw in enumerate(payload.get('items') or []):
        SalesOrderItem.objects.create(
            order=order,
            position=i,
            description=(raw.get('description') or '')[:300],
            quantity=Decimal(str(raw.get('quantity') or '1')),
            unit_price=Decimal(str(raw.get('unit_price') or '0')),
        )

    order.recalculate_totals(save=True)

    OrderEvent.objects.create(
        order=order, kind=OrderEvent.Kind.CREATED, actor=actor,
        message=f'Order created from {source}',
        payload={'item_count': order.items.count(), 'total': str(order.total)},
    )
    return order


# ── Fulfillment: step-by-step document generation ───────────────────────────


def _build_proforma_from_order(order, actor):
    """Create a draft Proforma InvoiceDocument matching the order."""
    # Local imports — see top-of-file note about lazy resolution.
    from apps.invoices.models import InvoiceDocument, InvoiceLineItem, DocType
    from apps.files.models import SharedFile

    # Pick a letterhead. We require ONE in the system; the agent can
    # always edit and pick another before finalizing if needed. Strategy:
    # most recently uploaded PDF whose name suggests a letterhead. Falls
    # back to any PDF.
    letterhead = (
        SharedFile.objects
        .filter(name__iendswith='.pdf')
        .filter(name__icontains='letterhead')
        .order_by('-created_at')
        .first()
        or
        SharedFile.objects
        .filter(name__iendswith='.pdf')
        .order_by('-created_at')
        .first()
    )
    if letterhead is None:
        raise ValueError(
            'No letterhead PDF available. Upload a letterhead in Files first.'
        )

    proforma = InvoiceDocument.objects.create(
        doc_type=DocType.PROFORMA,
        letterhead=letterhead,
        status=InvoiceDocument.Status.DRAFT,
        client_name=(order.customer.display_name if order.customer else order.contact_name) or '',
        client_email=(order.customer.email if order.customer else order.contact_email) or '',
        client_address=(order.customer.address if order.customer else '') or order.delivery_address,
        ship_to_address=order.delivery_address,
        po_reference=order.order_no,            # link back to the order on the PDF
        invoice_date=timezone.localdate(),
        currency=order.currency,
        tax_rate=order.tax_rate,
        discount_amount=order.discount_amount,
        notes=order.notes,
        created_by=actor,
    )
    for li in order.items.all().order_by('position', 'id'):
        InvoiceLineItem.objects.create(
            invoice=proforma,
            position=li.position,
            description=li.description,
            quantity=li.quantity,
            unit_price=li.unit_price,
        )
    proforma.recalculate_totals(save=True)
    return proforma


def _validate_order_can_generate_documents(order):
    """Shared guard for document generation actions."""
    if order.status == OrderStatus.CANCELLED:
        raise ValueError('Cancelled orders cannot generate documents.')
    if not order.items.exists():
        raise ValueError('Cannot generate documents for an order with no items.')
    if order.customer is None:
        raise ValueError('Attach a customer to this order before generating documents.')


@transaction.atomic
def fulfill_order(order, actor):
    """
    Sales fulfillment now creates ONLY the finalized Proforma.

    Invoice and Delivery Note are generated later through their own actions:
        Proforma generated by Sales fulfillment
        Invoice generated manually from the Proforma
        Delivery Note generated manually from the Invoice
    """
    from apps.invoices.services import finalize_invoice

    order = SalesOrder.objects.select_for_update().get(pk=order.pk)
    _validate_order_can_generate_documents(order)

    if order.proforma_id:
        raise ValueError('A proforma has already been generated for this order.')

    proforma = _build_proforma_from_order(order, actor)
    proforma = finalize_invoice(proforma, actor)

    order.proforma = proforma
    order.status = OrderStatus.IN_FULFILLMENT
    order.save(update_fields=['proforma', 'status', 'updated_at'])

    OrderEvent.objects.create(
        order=order,
        kind=OrderEvent.Kind.DOC_GENERATED,
        actor=actor,
        message=f'Proforma {proforma.number} generated. Invoice generation is now available.',
        payload={'doc_id': str(proforma.id), 'doc_type': 'proforma'},
    )
    return order


@transaction.atomic
def generate_invoice_for_order(order, actor):
    """Generate and finalize an Invoice from the order's finalized Proforma."""
    from apps.invoices.services import finalize_invoice, convert_invoice

    order = SalesOrder.objects.select_for_update().select_related('proforma').get(pk=order.pk)
    _validate_order_can_generate_documents(order)

    if not order.proforma_id:
        raise ValueError('Generate the Proforma before generating the Invoice.')
    if order.invoice_id:
        raise ValueError('An invoice has already been generated for this order.')
    if order.delivery_note_id:
        raise ValueError('This order already has a delivery note. Invoice cannot be regenerated.')

    invoice_draft = convert_invoice(order.proforma, actor)
    invoice = finalize_invoice(invoice_draft, actor)

    order.invoice = invoice
    order.status = OrderStatus.IN_FULFILLMENT
    order.save(update_fields=['invoice', 'status', 'updated_at'])

    OrderEvent.objects.create(
        order=order,
        kind=OrderEvent.Kind.DOC_GENERATED,
        actor=actor,
        message=f'Invoice {invoice.number} generated. Delivery Note generation is now available.',
        payload={
            'doc_id': str(invoice.id),
            'doc_type': 'invoice',
            'source_doc_id': str(order.proforma_id),
        },
    )
    return order


@transaction.atomic
def generate_delivery_note_for_order(order, actor):
    """Generate and finalize a Delivery Note from the order's finalized Invoice."""
    from apps.invoices.services import finalize_invoice, convert_invoice

    order = SalesOrder.objects.select_for_update().select_related('proforma', 'invoice').get(pk=order.pk)
    _validate_order_can_generate_documents(order)

    if not order.invoice_id:
        raise ValueError('Generate the Invoice before generating the Delivery Note.')
    if order.delivery_note_id:
        raise ValueError('A Delivery Note has already been generated for this order.')

    dn_draft = convert_invoice(order.invoice, actor)
    delivery_note = finalize_invoice(dn_draft, actor)

    order.delivery_note = delivery_note
    order.status = OrderStatus.FULFILLED
    order.fulfilled_by = actor
    order.fulfilled_at = timezone.now()
    order.save(update_fields=[
        'delivery_note', 'status', 'fulfilled_by', 'fulfilled_at', 'updated_at',
    ])

    OrderEvent.objects.create(
        order=order,
        kind=OrderEvent.Kind.DOC_GENERATED,
        actor=actor,
        message=f'Delivery Note {delivery_note.number} generated.',
        payload={
            'doc_id': str(delivery_note.id),
            'doc_type': 'delivery_note',
            'source_doc_id': str(order.invoice_id),
        },
    )
    OrderEvent.objects.create(
        order=order,
        kind=OrderEvent.Kind.FULFILLED,
        actor=actor,
        message='Order fulfilled — Proforma, Invoice and Delivery Note are complete.',
        payload={
            'proforma_no': order.proforma.number if order.proforma else '',
            'invoice_no': order.invoice.number if order.invoice else '',
            'delivery_note_no': delivery_note.number,
        },
    )
    return order


def cancel_order(order, actor, reason=''):
    """Mark order cancelled. No-op if already cancelled."""
    if order.status == OrderStatus.CANCELLED:
        return order
    if order.status == OrderStatus.FULFILLED:
        raise ValueError('Fulfilled orders cannot be cancelled. Void the invoice instead.')
    order.status = OrderStatus.CANCELLED
    order.cancelled_reason = (reason or '')[:300]
    order.save(update_fields=['status', 'cancelled_reason', 'updated_at'])
    OrderEvent.objects.create(
        order=order, kind=OrderEvent.Kind.CANCELLED, actor=actor,
        message=f'Order cancelled: {reason or "no reason given"}',
    )
    return order


def attach_customer(order, customer, actor):
    """Used by the agent UI when an order arrived without a customer link."""
    order.customer = customer
    order.save(update_fields=['customer', 'updated_at'])
    OrderEvent.objects.create(
        order=order, kind=OrderEvent.Kind.CUSTOMER_LINK, actor=actor,
        message=f'Customer linked: {customer}',
        payload={'customer_id': customer.pk},
    )
    return order
