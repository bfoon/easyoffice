"""
apps/pos/models_cash.py
───────────────────────
Cash-drawer reconciliation for the POS.

A POSCashSession is a cashier's shift on a till: it records the OPENING
float (cash placed in the drawer at open), every CASH sale rung up during
the shift, any manual pay-ins / pay-outs (petty cash, bank drops), and
the CLOSING count (what the cashier physically counts at the end). The
difference between what SHOULD be in the drawer and what was COUNTED is
the over/short variance.

    expected_cash = opening_float
                  + cash_sales           (sum of CASH sale totals)
                  + cash_pay_ins          (money added mid-shift)
                  − cash_pay_outs         (money removed mid-shift)
                  − cash_refunds          (cash given back on voids)

    variance      = counted_cash − expected_cash
                    (positive = over, negative = short)

Non-cash takings (card / QMoney / Afrimoney / Wave / bank) are tracked
per method for the Z-report but do NOT affect the drawer variance —
only physical cash does.

Import these into apps/pos/models.py so migrations pick them up:

    from .models_cash import POSCashSession, POSCashMovement   # noqa
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from .models import TimeStampedModel, POSSale


class POSCashSession(TimeStampedModel):
    """One cashier's till shift, from opening float to closing count."""

    class Status(models.TextChoices):
        OPEN   = 'open',   'Open'
        CLOSED = 'closed', 'Closed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    session_no = models.CharField(
        max_length=24, unique=True, null=True, blank=True, db_index=True,
        help_text='Shift number, assigned at open (TS-YYYYMMDD-###).',
    )
    status = models.CharField(max_length=8, choices=Status.choices,
                              default=Status.OPEN, db_index=True)

    cashier = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='pos_cash_sessions',
    )

    # Optional label for the physical till / drawer / location.
    register_name = models.CharField(max_length=60, blank=True,
                                     help_text='e.g. "Front counter", "Till 2".')
    currency = models.CharField(max_length=8, default='GMD')

    # ── Open ─────────────────────────────────────────────────────────────
    opening_float = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        help_text='Cash placed in the drawer at the start of the shift.')
    opened_at = models.DateTimeField(default=timezone.now)
    open_note = models.CharField(max_length=300, blank=True)

    # ── Close (filled in at close time) ──────────────────────────────────
    counted_cash = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text='Physical cash counted in the drawer at close.')
    # Snapshot of the computed expectation at close, so historical
    # variance never shifts if a sale is voided afterwards.
    expected_cash = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True)
    variance = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text='counted − expected. Positive = over, negative = short.')
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='pos_cash_sessions_closed')
    close_note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['-opened_at']
        indexes = [
            models.Index(fields=['status', 'opened_at']),
            models.Index(fields=['cashier', 'status']),
        ]

    def __str__(self):
        return self.session_no or f'Shift {str(self.id)[:8]}'

    @property
    def is_open(self):
        return self.status == self.Status.OPEN

    # ── Live tallies (computed from linked sales + movements) ─────────────

    def _completed_sales(self):
        return self.sales.filter(status=POSSale.Status.COMPLETED)

    @property
    def cash_sales(self) -> Decimal:
        """Sum of CASH sale totals in this shift."""
        total = (self._completed_sales()
                 .filter(payment_method=POSSale.Payment.CASH)
                 .aggregate(s=models.Sum('total'))['s'])
        return total or Decimal('0.00')

    @property
    def noncash_sales_by_method(self) -> dict:
        """{'card': Decimal, 'qmoney': Decimal, …} for the Z-report."""
        rows = (self._completed_sales()
                .exclude(payment_method=POSSale.Payment.CASH)
                .values('payment_method')
                .annotate(total=models.Sum('total')))
        return {r['payment_method']: (r['total'] or Decimal('0.00')) for r in rows}

    @property
    def total_sales(self) -> Decimal:
        total = self._completed_sales().aggregate(s=models.Sum('total'))['s']
        return total or Decimal('0.00')

    @property
    def sales_count(self) -> int:
        return self._completed_sales().count()

    def _movement_sum(self, kind) -> Decimal:
        total = (self.movements.filter(kind=kind)
                 .aggregate(s=models.Sum('amount'))['s'])
        return total or Decimal('0.00')

    @property
    def cash_pay_ins(self) -> Decimal:
        return self._movement_sum(POSCashMovement.Kind.PAY_IN)

    @property
    def cash_pay_outs(self) -> Decimal:
        return self._movement_sum(POSCashMovement.Kind.PAY_OUT)

    @property
    def cash_refunds(self) -> Decimal:
        """Cash handed back when a CASH sale in this shift was voided."""
        total = (self.sales
                 .filter(status=POSSale.Status.VOIDED,
                         payment_method=POSSale.Payment.CASH)
                 .aggregate(s=models.Sum('total'))['s'])
        return total or Decimal('0.00')

    @property
    def expected_cash_live(self) -> Decimal:
        """What SHOULD be in the drawer right now."""
        return (
            (self.opening_float or Decimal('0.00'))
            + self.cash_sales
            + self.cash_pay_ins
            - self.cash_pay_outs
            - self.cash_refunds
        ).quantize(Decimal('0.01'))

    @property
    def variance_live(self):
        """Live variance if a count is provided; None before close."""
        if self.counted_cash is None:
            return None
        return (self.counted_cash - self.expected_cash_live).quantize(Decimal('0.01'))


class POSCashMovement(TimeStampedModel):
    """
    A manual cash adjustment during a shift that isn't a sale:
    a pay-in (float top-up, petty cash returned) or a pay-out
    (bank drop, supplier paid from the drawer, petty cash taken).
    """

    class Kind(models.TextChoices):
        PAY_IN  = 'pay_in',  'Pay in (cash added)'
        PAY_OUT = 'pay_out', 'Pay out (cash removed)'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(POSCashSession, on_delete=models.CASCADE,
                                related_name='movements')
    kind = models.CharField(max_length=8, choices=Kind.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=300, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='pos_cash_movements')

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.get_kind_display()} {self.amount}'


class POSCashSessionCounter(models.Model):
    """Per-day shift-number sequence (TS-YYYYMMDD-###)."""
    day = models.DateField(unique=True)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = 'POS cash session counter'

    @classmethod
    def next_session_no(cls):
        """Must be called inside a transaction."""
        today = timezone.localdate()
        row, _ = cls.objects.select_for_update().get_or_create(day=today)
        row.last_number += 1
        row.save(update_fields=['last_number'])
        return f'TS-{today:%Y%m%d}-{row.last_number:03d}'
