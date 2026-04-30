"""
apps/orders/models.py
─────────────────────
Sales orders that originate from:
    • Phone calls (entered by a Customer Service agent)
    • Walk-ins   (entered by a Customer Service agent)
    • The public website (POSTed via the public API)
    • Any other API integration

An order is a *commitment* from a customer; fulfillment turns it into a
Proforma → Invoice → Delivery Note chain in apps.invoices.

Each generated document is sent to the CEO for signature (via the existing
files-app SignatureRequest). When the CEO signs, the document is auto-
emailed to the buyer and the next stage unlocks.

Free-text line items per project decision — no Product catalog.
"""
import uuid
from decimal import Decimal
from django.conf import settings
from django.db import models
from django.utils import timezone


User = settings.AUTH_USER_MODEL


# ── Choice constants ────────────────────────────────────────────────────────

class OrderSource(models.TextChoices):
    PHONE    = 'phone',    'Phone Call'
    WALK_IN  = 'walk_in',  'Walk-In'
    WEBSITE  = 'website',  'Website'
    API      = 'api',      'API / Integration'
    EMAIL    = 'email',    'Email'
    OTHER    = 'other',    'Other'


class OrderStatus(models.TextChoices):
    """
    Lifecycle (with CEO signature gates):

        new
         │
         ▼
        confirmed
         │  (Proforma generated)
         ▼
        proforma_pending_signature   ──┐
         │  (CEO signs)                │
         ▼                             │
        proforma_signed                │
         │  (Invoice generated)        │
         ▼                             ├─► cancelled
        invoice_pending_signature      │   (any non-terminal state)
         │  (CEO signs)                │
         ▼                             │
        invoice_signed                 │
         │  (DN generated)             │
         ▼                             │
        dn_pending_signature           │
         │  (CEO signs)                │
         ▼                             │
        fulfilled                    ──┘
    """
    NEW                          = 'new',                          'New'
    CONFIRMED                    = 'confirmed',                    'Confirmed'
    PROFORMA_PENDING_SIGNATURE   = 'proforma_pending_signature',   'Proforma — Awaiting CEO Signature'
    PROFORMA_SIGNED              = 'proforma_signed',              'Proforma Signed'
    INVOICE_PENDING_SIGNATURE    = 'invoice_pending_signature',    'Invoice — Awaiting CEO Signature'
    INVOICE_SIGNED               = 'invoice_signed',               'Invoice Signed'
    DN_PENDING_SIGNATURE         = 'dn_pending_signature',         'Delivery Note — Awaiting CEO Signature'
    FULFILLED                    = 'fulfilled',                    'Fulfilled'
    CANCELLED                    = 'cancelled',                    'Cancelled'


# Status groups — convenient for queries & UI
PENDING_SIGNATURE_STATUSES = {
    OrderStatus.PROFORMA_PENDING_SIGNATURE,
    OrderStatus.INVOICE_PENDING_SIGNATURE,
    OrderStatus.DN_PENDING_SIGNATURE,
}
ACTIVE_STATUSES = {
    OrderStatus.NEW, OrderStatus.CONFIRMED,
    OrderStatus.PROFORMA_PENDING_SIGNATURE, OrderStatus.PROFORMA_SIGNED,
    OrderStatus.INVOICE_PENDING_SIGNATURE,  OrderStatus.INVOICE_SIGNED,
    OrderStatus.DN_PENDING_SIGNATURE,
}
TERMINAL_STATUSES = {OrderStatus.FULFILLED, OrderStatus.CANCELLED}


# ── SalesOrder ──────────────────────────────────────────────────────────────

class SalesOrder(models.Model):
    # Identification
    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order_no      = models.CharField(max_length=30, unique=True, editable=False, db_index=True)

    # Where it came from
    source        = models.CharField(max_length=20, choices=OrderSource.choices, default=OrderSource.PHONE)
    external_ref  = models.CharField(
        max_length=80, blank=True, db_index=True,
        help_text='External reference (e.g. website checkout id, integration id)',
    )
    idempotency_key = models.CharField(max_length=80, blank=True, db_index=True)

    # Customer link (optional at intake)
    customer = models.ForeignKey(
        'customer_service.Customer', on_delete=models.PROTECT,
        related_name='orders', null=True, blank=True,
    )
    contact_name     = models.CharField(max_length=180, blank=True)
    contact_phone    = models.CharField(max_length=30,  blank=True)
    contact_email    = models.EmailField(blank=True)
    delivery_address = models.TextField(blank=True)
    notes            = models.TextField(blank=True)

    # Status
    status = models.CharField(
        max_length=32, choices=OrderStatus.choices,
        default=OrderStatus.NEW, db_index=True,
    )
    cancelled_reason = models.CharField(max_length=300, blank=True)

    # Money
    currency        = models.CharField(max_length=3, default='GMD')
    tax_rate        = models.DecimalField(max_digits=5,  decimal_places=2, default=Decimal('0.00'))
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    subtotal        = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    tax_amount      = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total           = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))

    # ── Document chain ──────────────────────────────────────────────────────
    proforma      = models.OneToOneField(
        'invoices.InvoiceDocument', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='source_order_proforma',
    )
    invoice       = models.OneToOneField(
        'invoices.InvoiceDocument', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='source_order_invoice',
    )
    delivery_note = models.OneToOneField(
        'invoices.InvoiceDocument', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='source_order_delivery_note',
    )

    # ── Optional ServiceTicket link ─────────────────────────────────────────
    ticket = models.ForeignKey(
        'customer_service.ServiceTicket', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='orders',
    )

    # Audit
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_orders',
    )
    fulfilled_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='fulfilled_orders',
    )
    confirmed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='confirmed_orders',
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)
    fulfilled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['source', 'status']),
            models.Index(fields=['status', '-created_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['idempotency_key'],
                condition=~models.Q(idempotency_key=''),
                name='orders_idempotency_key_unique_when_set',
            ),
        ]

    def __str__(self):
        return self.order_no or f'Order {self.id}'

    def save(self, *args, **kwargs):
        if not self.order_no:
            self.order_no = f'SO-{timezone.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}'
        super().save(*args, **kwargs)

    def recalculate_totals(self, save=True):
        sub = sum((li.line_total for li in self.items.all()), Decimal('0.00'))
        sub = sub - (self.discount_amount or Decimal('0.00'))
        if sub < 0:
            sub = Decimal('0.00')
        tax = (sub * (self.tax_rate or Decimal('0.00')) / Decimal('100')).quantize(Decimal('0.01'))
        self.subtotal   = sub
        self.tax_amount = tax
        self.total      = sub + tax
        if save:
            self.save(update_fields=['subtotal', 'tax_amount', 'total', 'updated_at'])
        return self.total

    # ── Convenience flags used by templates ────────────────────────────────
    @property
    def is_fulfillable(self):
        return (
            self.status == OrderStatus.CONFIRMED
            and self.items.exists()
            and self.customer_id is not None
        )

    @property
    def has_full_chain(self):
        return bool(self.proforma_id and self.invoice_id and self.delivery_note_id)

    @property
    def is_awaiting_signature(self):
        return self.status in PENDING_SIGNATURE_STATUSES

    @property
    def pending_signature_doc_label(self):
        """Used by templates to show 'Awaiting CEO signature on the Proforma'."""
        return {
            OrderStatus.PROFORMA_PENDING_SIGNATURE: 'Proforma',
            OrderStatus.INVOICE_PENDING_SIGNATURE:  'Invoice',
            OrderStatus.DN_PENDING_SIGNATURE:       'Delivery Note',
        }.get(self.status, '')


class SalesOrderItem(models.Model):
    id          = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order       = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name='items')
    position    = models.PositiveSmallIntegerField(default=0)
    description = models.CharField(max_length=300)
    quantity    = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('1.00'))
    unit_price  = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

    class Meta:
        ordering = ['position', 'id']

    @property
    def line_total(self) -> Decimal:
        return (self.quantity or Decimal('0')) * (self.unit_price or Decimal('0'))

    def __str__(self):
        return f'{self.description} ({self.quantity} × {self.unit_price})'


class OrderEvent(models.Model):
    """Append-only audit trail."""
    class Kind(models.TextChoices):
        CREATED       = 'created',       'Created'
        UPDATED       = 'updated',       'Updated'
        CONFIRMED     = 'confirmed',     'Confirmed'
        FULFILLED     = 'fulfilled',     'Fulfilled'
        CANCELLED     = 'cancelled',     'Cancelled'
        DOC_GENERATED = 'doc_generated', 'Document Generated'
        DOC_SIGNED    = 'doc_signed',    'Document Signed'
        CUSTOMER_LINK = 'customer_link', 'Customer Linked'
        NOTE          = 'note',          'Note'

    id        = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order     = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name='events')
    kind      = models.CharField(max_length=20, choices=Kind.choices)
    actor     = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='order_events',
    )
    message   = models.CharField(max_length=400)
    payload   = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.order.order_no}: {self.get_kind_display()}'