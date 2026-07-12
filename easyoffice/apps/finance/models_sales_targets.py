"""
apps/finance/models_sales_targets.py
====================================

Sales Targets & Rewards system.

The CEO or Office Administrator sets monthly / quarterly sales targets,
each with a cash reward. Targets can be scoped to:

    * an individual sales agent  (achievement = finalized InvoiceDocuments
                                  created by that agent in the period), or
    * the whole sales department (achievement = ALL finalized invoices
                                  in the period).

When a target's period ends and the target was met, a SalesRewardPayout
record is created (idempotently) so the CEO/Admin can approve it and mark
the cash reward as paid.

IMPORTANT — model discovery:
    Because this lives outside apps/finance/models.py, Django will only
    pick these models up for migrations if they're imported there. Add
    this single line at the very BOTTOM of apps/finance/models.py:

        from apps.finance.models_sales_targets import SalesTarget, SalesRewardPayout  # noqa

    Then run:  python manage.py makemigrations finance && python manage.py migrate
"""
import calendar
import uuid
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone


User = settings.AUTH_USER_MODEL


class SalesTarget(models.Model):
    """A monthly or quarterly sales target with an attached cash reward."""

    class PeriodType(models.TextChoices):
        MONTHLY = 'monthly', 'Monthly'
        QUARTERLY = 'quarterly', 'Quarterly'

    class Scope(models.TextChoices):
        AGENT = 'agent', 'Sales Agent'
        DEPARTMENT = 'department', 'Sales Department'

    class Basis(models.TextChoices):
        INVOICED = 'invoiced', 'Invoiced (invoice date)'
        PAID = 'paid', 'Paid (payment received)'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    scope = models.CharField(max_length=20, choices=Scope.choices, default=Scope.AGENT)
    period_type = models.CharField(max_length=20, choices=PeriodType.choices,
                                   default=PeriodType.MONTHLY)
    year = models.PositiveIntegerField()
    month = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text='1-12 — required for monthly targets.',
    )
    quarter = models.PositiveSmallIntegerField(
        null=True, blank=True,
        help_text='1-4 — required for quarterly targets.',
    )

    agent = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='sales_targets',
        help_text='Required when scope is "agent"; empty for department targets.',
    )

    target_amount = models.DecimalField(max_digits=14, decimal_places=2)
    reward_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        help_text='Cash reward paid out if the target is met.',
    )
    currency = models.CharField(max_length=8, default='GMD')
    basis = models.CharField(max_length=20, choices=Basis.choices,
                             default=Basis.INVOICED)

    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    set_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='sales_targets_set',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-year', '-quarter', '-month', 'scope']
        indexes = [
            models.Index(fields=['year', 'period_type', 'is_active']),
        ]

    # ── Period helpers ───────────────────────────────────────────────────

    @property
    def period_start(self) -> date:
        if self.period_type == self.PeriodType.MONTHLY:
            return date(self.year, self.month or 1, 1)
        start_month = ((self.quarter or 1) - 1) * 3 + 1
        return date(self.year, start_month, 1)

    @property
    def period_end(self) -> date:
        if self.period_type == self.PeriodType.MONTHLY:
            m = self.month or 1
            return date(self.year, m, calendar.monthrange(self.year, m)[1])
        end_month = ((self.quarter or 1) - 1) * 3 + 3
        return date(self.year, end_month,
                    calendar.monthrange(self.year, end_month)[1])

    @property
    def period_label(self) -> str:
        if self.period_type == self.PeriodType.MONTHLY:
            return f'{calendar.month_name[self.month or 1]} {self.year}'
        return f'Q{self.quarter or 1} {self.year}'

    @property
    def is_period_over(self) -> bool:
        return timezone.localdate() > self.period_end

    @property
    def is_period_current(self) -> bool:
        today = timezone.localdate()
        return self.period_start <= today <= self.period_end

    @property
    def scope_label(self) -> str:
        if self.scope == self.Scope.DEPARTMENT:
            return 'Sales Department'
        if self.agent_id:
            name = (self.agent.get_full_name() or self.agent.username)
            return name
        return 'Unassigned agent'

    # ── Achievement ──────────────────────────────────────────────────────

    def _invoice_qs(self):
        """
        Finalized billable invoices that count toward this target,
        filtered by currency, period, basis and (for agent scope) creator.
        Lazy import keeps the finance ↔ invoices coupling at runtime only.
        """
        from apps.invoices.models import InvoiceDocument, DocType

        qs = InvoiceDocument.objects.filter(
            doc_type=DocType.INVOICE,
            status=InvoiceDocument.Status.FINALIZED,
            currency=self.currency,
        )
        if self.basis == self.Basis.PAID:
            qs = qs.filter(
                paid_at__isnull=False,
                paid_at__date__gte=self.period_start,
                paid_at__date__lte=self.period_end,
            )
        else:
            qs = qs.filter(
                invoice_date__gte=self.period_start,
                invoice_date__lte=self.period_end,
            )
        if self.scope == self.Scope.AGENT and self.agent_id:
            qs = qs.filter(created_by_id=self.agent_id)
        return qs

    def compute_achieved(self) -> Decimal:
        total = self._invoice_qs().aggregate(s=models.Sum('total'))['s']
        return total or Decimal('0.00')

    def compute_invoice_count(self) -> int:
        return self._invoice_qs().count()

    def progress_pct(self, achieved: Decimal | None = None) -> float:
        if achieved is None:
            achieved = self.compute_achieved()
        if not self.target_amount:
            return 0.0
        return min(float(achieved / self.target_amount * 100), 999.0)

    def is_met(self, achieved: Decimal | None = None) -> bool:
        if achieved is None:
            achieved = self.compute_achieved()
        return achieved >= self.target_amount

    # ── Payout evaluation ────────────────────────────────────────────────

    def evaluate_payout(self):
        """
        If the period is over, the target is active and met, ensure a
        SalesRewardPayout exists. Idempotent (OneToOne on target).
        Returns the payout or None.
        """
        if not (self.is_active and self.is_period_over):
            return None
        achieved = self.compute_achieved()
        if achieved < self.target_amount:
            return None
        payout, _created = SalesRewardPayout.objects.get_or_create(
            target=self,
            defaults={
                'achieved_amount': achieved,
                'reward_amount': self.reward_amount,
                'currency': self.currency,
            },
        )
        return payout

    def __str__(self):
        return f'{self.scope_label} — {self.period_label} — {self.currency} {self.target_amount}'


class SalesRewardPayout(models.Model):
    """Cash reward earned by meeting a SalesTarget."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending approval'
        APPROVED = 'approved', 'Approved — awaiting payment'
        PAID = 'paid', 'Paid'
        REJECTED = 'rejected', 'Rejected'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    target = models.OneToOneField(
        SalesTarget,
        on_delete=models.CASCADE,
        related_name='payout',
    )

    achieved_amount = models.DecimalField(max_digits=14, decimal_places=2)
    reward_amount = models.DecimalField(max_digits=14, decimal_places=2)
    currency = models.CharField(max_length=8, default='GMD')

    status = models.CharField(max_length=20, choices=Status.choices,
                              default=Status.PENDING)

    approved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sales_rewards_approved',
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    paid_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='sales_rewards_paid',
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    payment_method = models.CharField(max_length=30, blank=True)
    payment_reference = models.CharField(max_length=120, blank=True)

    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def beneficiary_label(self):
        return self.target.scope_label

    def __str__(self):
        return f'Reward {self.currency} {self.reward_amount} → {self.beneficiary_label} ({self.get_status_display()})'
