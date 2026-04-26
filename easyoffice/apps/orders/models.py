"""
apps/orders/models.py
─────────────────────
Sales orders that originate from:
    • Phone calls (entered by a Customer Service agent)
    • Walk-ins   (entered by a Customer Service agent)
    • The public website (POSTed via the public API)
    • Any other API integration

An order is a *commitment* from a customer; fulfillment turns it into a
Proforma → Invoice → Delivery Note chain in apps.invoices via the
existing services.convert_invoice() function.

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
    Lifecycle:

        new ──► confirmed ──► in_fulfillment ──► fulfilled
                  │                │
                  └──► cancelled ◄─┘

    Once an order has a Proforma / Invoice / Delivery Note attached it
    moves through the fulfillment states automatically. The agent never
    has to flip status manually for the happy path.
    """
    NEW            = 'new',            'New'
    CONFIRMED      = 'confirmed',      'Confirmed'
    IN_FULFILLMENT = 'in_fulfillment', 'In Fulfillment'
    FULFILLED      = 'fulfilled',      'Fulfilled'
    CANCELLED      = 'cancelled',      'Cancelled'


# ── SalesOrder ──────────────────────────────────────────────────────────────

class SalesOrder(models.Model):
    # Identification
    id            = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order_no      = models.CharField(max_length=30, unique=True, editable=False, db_index=True)

    # Where it came from
    source        = models.CharField(max_length=20, choices=OrderSource.choices, default=OrderSource.PHONE)
    # If the website/API call sent its own reference, store it for reconciliation.
    external_ref  = models.CharField(
        max_length=80, blank=True, db_index=True,
        help_text='External reference (e.g. website checkout id, integration id)',
    )
    # Idempotency key — protects against the website POSTing the same order
    # twice (network retry, double-click). Unique when set.
    idempotency_key = models.CharField(max_length=80, blank=True, db_index=True)

    # Customer link (optional at intake — agent attaches later if blank)
    customer = models.ForeignKey(
        'customer_service.Customer', on_delete=models.PROTECT,
        related_name='orders', null=True, blank=True,
    )
    # Snapshot of customer-provided contact info at the time of order.
    # We keep this even after `customer` is linked because the customer
    # record may be edited later and the order should preserve what was
    # originally submitted.
    contact_name    = models.CharField(max_length=180, blank=True)
    contact_phone   = models.CharField(max_length=30,  blank=True)
    contact_email   = models.EmailField(blank=True)
    delivery_address = models.TextField(blank=True)
    notes           = models.TextField(blank=True)

    # Status
    status        = models.CharField(max_length=20, choices=OrderStatus.choices,
                                      default=OrderStatus.NEW, db_index=True)
    cancelled_reason = models.CharField(max_length=300, blank=True)

    # Money (mirrors invoice money fields, recalculated from items)
    currency      = models.CharField(max_length=3, default='GMD')
    tax_rate      = models.DecimalField(max_digits=5,  decimal_places=2, default=Decimal('0.00'))
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    subtotal      = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    tax_amount    = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total         = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))

    # ── Document chain ──────────────────────────────────────────────────────
    # OneToOneFields prevent the same invoice doc from being attached to two
    # orders. SET_NULL on delete because invoices have their own lifecycle.
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
    # If the order came in during a phone call, the agent may already have
    # an open ticket. Linking the two keeps the customer history clean.
    ticket = models.ForeignKey(
        'customer_service.ServiceTicket', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='orders',
    )

    # Audit
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='created_orders',
        help_text='Set when an internal agent creates the order. NULL for API-created orders.',
    )
    fulfilled_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='fulfilled_orders',
    )
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)
    fulfilled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['source', 'status']),
            models.Index(fields=['status', '-created_at']),
        ]
        constraints = [
            # Idempotency key, when present, must be unique. Empty strings are
            # allowed to repeat (most internal orders won't use one).
            models.UniqueConstraint(
                fields=['idempotency_key'],
                condition=~models.Q(idempotency_key=''),
                name='orders_idempotency_key_unique_when_set',
            ),
        ]

    def __str__(self):
        return self.order_no or f'Order {self.id}'

    # ── Auto-numbering ──────────────────────────────────────────────────────
    def save(self, *args, **kwargs):
        if not self.order_no:
            # SO-YYYYMMDD-XXXXXX — date prefix for ops legibility, hex suffix
            # avoids the need for a sequence table on every insert.
            self.order_no = f'SO-{timezone.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}'
        super().save(*args, **kwargs)

    # ── Money ───────────────────────────────────────────────────────────────
    def recalculate_totals(self, save=True):
        """Sum line items, apply discount, then tax. Mirrors InvoiceDocument."""
        sub = sum(
            (li.line_total for li in self.items.all()),
            Decimal('0.00'),
        )
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
            self.status in (OrderStatus.NEW, OrderStatus.CONFIRMED)
            and self.items.exists()
            and self.customer_id is not None
        )

    @property
    def has_full_chain(self):
        return bool(self.proforma_id and self.invoice_id and self.delivery_note_id)


class SalesOrderItem(models.Model):
    """
    Free-text line item, structurally identical to invoices.InvoiceLineItem
    so we can copy 1:1 when generating the proforma.
    """
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
    """
    Append-only audit trail. Every state change, every doc generation,
    every API submission lands here for traceability.
    """
    class Kind(models.TextChoices):
        CREATED       = 'created',       'Created'
        UPDATED       = 'updated',       'Updated'
        CONFIRMED     = 'confirmed',     'Confirmed'
        FULFILLED     = 'fulfilled',     'Fulfilled'
        CANCELLED     = 'cancelled',     'Cancelled'
        DOC_GENERATED = 'doc_generated', 'Document Generated'
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
