"""
apps/pos/models.py
──────────────────
Point of Sale for walk-in customers.

Design notes
────────────
• Sale lines SNAPSHOT the product (sku, barcode, name, unit price) at the
  moment of sale, so receipts stay correct forever even if inventory
  prices or names change later.
• The link to the live inventory row is a soft reference
  (`inventory_id` stored as text) resolved through apps/pos/inventory.py,
  so the POS works with the orders-app inventory regardless of its exact
  model — configured once in settings.
• Customer capture is OPTIONAL: name+phone personalises the printed
  receipt; an email triggers the digital (PDF) receipt. When the phone or
  email matches an existing customer_service.Customer we link them.
• Receipt numbers are PS-YYYYMMDD-#### via a per-day counter row taken
  under select_for_update — gapless and race-safe.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class POSSale(TimeStampedModel):
    """One checkout basket → one receipt."""

    class Status(models.TextChoices):
        DRAFT     = 'draft',     'Draft (open basket)'
        COMPLETED = 'completed', 'Completed'
        VOIDED    = 'voided',    'Voided'

    class Payment(models.TextChoices):
        CASH   = 'cash',   'Cash'
        CARD   = 'card',   'Card'
        QMONEY = 'qmoney', 'QMoney'
        AFRI   = 'afrimoney', 'Afrimoney'
        WAVE   = 'wave',   'Wave'
        BANK   = 'bank',   'Bank transfer'
        OTHER  = 'other',  'Other'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    sale_no = models.CharField(
        max_length=24, unique=True, null=True, blank=True, db_index=True,
        help_text='Receipt number, assigned at completion (PS-YYYYMMDD-####).',
    )
    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.DRAFT, db_index=True)

    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='pos_sales',
    )

    # ── Optional customer capture ────────────────────────────────────────
    customer_name  = models.CharField(max_length=180, blank=True)
    customer_phone = models.CharField(max_length=30, blank=True)
    customer_email = models.EmailField(blank=True)
    customer = models.ForeignKey(
        'customer_service.Customer', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='pos_sales',
        help_text='Linked automatically when phone/email matches an existing contact.',
    )

    # ── Money ────────────────────────────────────────────────────────────
    currency = models.CharField(max_length=8, default='GMD')
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    tax      = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total    = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

    payment_method  = models.CharField(max_length=12, choices=Payment.choices,
                                       default=Payment.CASH)
    payment_ref     = models.CharField(max_length=80, blank=True,
                                       help_text='Card auth / mobile-money txn ref.')
    amount_tendered = models.DecimalField(max_digits=12, decimal_places=2,
                                          null=True, blank=True)
    change_due      = models.DecimalField(max_digits=12, decimal_places=2,
                                          null=True, blank=True)

    completed_at = models.DateTimeField(null=True, blank=True)

    # ── Void / refund trail ──────────────────────────────────────────────
    voided_at  = models.DateTimeField(null=True, blank=True)
    voided_by  = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='pos_voided_sales',
    )
    void_reason = models.CharField(max_length=300, blank=True)

    # ── Digital receipt ─────────────────────────────────────────────────
    receipt_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False,
                                     help_text='Public no-login receipt link / QR target.')
    receipt_emailed_at = models.DateTimeField(null=True, blank=True)

    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['cashier', 'created_at']),
        ]

    def __str__(self):
        return self.sale_no or f'Basket {str(self.id)[:8]}'

    # ── Helpers ──────────────────────────────────────────────────────────
    @property
    def is_draft(self):
        return self.status == self.Status.DRAFT

    @property
    def item_count(self):
        return sum(i.quantity for i in self.items.all())

    def recalc_totals(self, save=True):
        """Recompute money fields from the lines. Discount/tax stay as set."""
        sub = sum((i.line_total for i in self.items.all()), Decimal('0.00'))
        self.subtotal = sub
        self.total = (sub - (self.discount or 0) + (self.tax or 0)).quantize(Decimal('0.01'))
        if self.total < 0:
            self.total = Decimal('0.00')
        if save:
            self.save(update_fields=['subtotal', 'total', 'updated_at'])
        return self.total


class POSSaleItem(TimeStampedModel):
    """One product line on the basket/receipt — snapshotted at scan time."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sale = models.ForeignKey(POSSale, on_delete=models.CASCADE, related_name='items')

    # Soft link to the live inventory row (resolved via apps/pos/inventory.py)
    inventory_id = models.CharField(max_length=64, blank=True, db_index=True)

    # Snapshot — what the receipt will always show
    sku        = models.CharField(max_length=64, blank=True)
    barcode    = models.CharField(max_length=64, blank=True, db_index=True)
    name       = models.CharField(max_length=255)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    quantity   = models.PositiveIntegerField(default=1)
    line_total = models.DecimalField(max_digits=12, decimal_places=2)

    # Set at completion so voids know what to put back
    stock_deducted = models.BooleanField(default=False)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.quantity} × {self.name}'

    def save(self, *args, **kwargs):
        self.line_total = (self.unit_price * self.quantity).quantize(Decimal('0.01'))
        super().save(*args, **kwargs)


class POSDailyCounter(models.Model):
    """Per-day receipt sequence. Locked with select_for_update at completion."""
    day = models.DateField(unique=True)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = 'POS daily receipt counter'

    @classmethod
    def next_sale_no(cls):
        """Must be called inside a transaction. Returns e.g. PS-20260704-0042."""
        today = timezone.localdate()
        row, _ = cls.objects.select_for_update().get_or_create(day=today)
        row.last_number += 1
        row.save(update_fields=['last_number'])
        return f'PS-{today:%Y%m%d}-{row.last_number:04d}'
