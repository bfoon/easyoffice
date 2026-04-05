import uuid
from django.db import models
from apps.core.models import User


class Budget(models.Model):
    class Status(models.TextChoices):
        DRAFT    = 'draft',    'Draft'
        APPROVED = 'approved', 'Approved'
        ACTIVE   = 'active',   'Active'
        CLOSED   = 'closed',   'Closed'

    id             = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name           = models.CharField(max_length=200)
    department     = models.ForeignKey('organization.Department', on_delete=models.SET_NULL, null=True, blank=True)
    unit           = models.ForeignKey('organization.Unit', on_delete=models.SET_NULL, null=True, blank=True)
    fiscal_year    = models.PositiveSmallIntegerField()
    total_amount   = models.DecimalField(max_digits=15, decimal_places=2)
    allocated_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    spent_amount   = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    status         = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    approved_by    = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name='approved_budgets')
    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-fiscal_year', 'name']

    def __str__(self):
        return f'{self.name} — FY{self.fiscal_year}'

    @property
    def balance(self):
        return self.total_amount - self.spent_amount

    @property
    def utilization_pct(self):
        if self.total_amount > 0:
            return min(100, round(float(self.spent_amount) / float(self.total_amount) * 100, 1))
        return 0

    @property
    def status_color(self):
        return {
            'draft': '#94a3b8', 'approved': '#10b981',
            'active': '#3b82f6', 'closed': '#64748b',
        }.get(self.status, '#64748b')


class PurchaseRequest(models.Model):
    class Status(models.TextChoices):
        DRAFT     = 'draft',     'Draft'
        SUBMITTED = 'submitted', 'Submitted'
        APPROVED  = 'approved',  'Approved'
        REJECTED  = 'rejected',  'Rejected'
        ORDERED   = 'ordered',   'Ordered'
        DELIVERED = 'delivered', 'Delivered'
        CLOSED    = 'closed',    'Closed'

    class Priority(models.TextChoices):
        LOW    = 'low',    'Low'
        NORMAL = 'normal', 'Normal'
        URGENT = 'urgent', 'Urgent'

    id               = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title            = models.CharField(max_length=300)
    description      = models.TextField()
    requested_by     = models.ForeignKey(User, on_delete=models.CASCADE, related_name='purchase_requests')
    department       = models.ForeignKey('organization.Department', on_delete=models.SET_NULL, null=True, blank=True)
    budget           = models.ForeignKey(Budget, on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name='purchase_requests')
    # ── Project link ──────────────────────────────────────────────────────────
    project          = models.ForeignKey('projects.Project', on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name='purchase_requests')
    # ─────────────────────────────────────────────────────────────────────────
    estimated_cost   = models.DecimalField(max_digits=12, decimal_places=2)
    actual_cost      = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    priority         = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL)
    status           = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    justification    = models.TextField()
    vendor           = models.CharField(max_length=200, blank=True)
    expected_delivery = models.DateField(null=True, blank=True)
    approved_by      = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                          related_name='approved_purchases')
    approval_date    = models.DateTimeField(null=True, blank=True)
    approval_notes   = models.TextField(blank=True)
    notes            = models.TextField(blank=True)
    attachment       = models.FileField(upload_to='purchase_docs/', null=True, blank=True)
    created_at       = models.DateTimeField(auto_now_add=True)
    updated_at       = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title

    @property
    def priority_color(self):
        return {'low': '#10b981', 'normal': '#3b82f6', 'urgent': '#ef4444'}.get(self.priority, '#64748b')

    @property
    def status_color(self):
        return {
            'draft': '#94a3b8', 'submitted': '#f59e0b', 'approved': '#10b981',
            'rejected': '#ef4444', 'ordered': '#8b5cf6',
            'delivered': '#06b6d4', 'closed': '#64748b',
        }.get(self.status, '#64748b')


class Payment(models.Model):
    class Method(models.TextChoices):
        BANK_TRANSFER = 'bank_transfer', 'Bank Transfer'
        CASH          = 'cash',          'Cash'
        CHEQUE        = 'cheque',        'Cheque'
        CARD          = 'card',          'Card'

    id               = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference        = models.CharField(max_length=100, unique=True)
    description      = models.TextField()
    purchase_request = models.ForeignKey(PurchaseRequest, on_delete=models.SET_NULL,
                                          null=True, blank=True, related_name='payments')
    amount           = models.DecimalField(max_digits=12, decimal_places=2)
    currency         = models.CharField(max_length=10, default='USD')
    method           = models.CharField(max_length=20, choices=Method.choices)
    paid_by          = models.ForeignKey(User, on_delete=models.CASCADE, related_name='payments_made')
    recipient        = models.CharField(max_length=200)
    payment_date     = models.DateField()
    receipt          = models.FileField(upload_to='receipts/%Y/%m/', null=True, blank=True)
    budget           = models.ForeignKey(Budget, on_delete=models.SET_NULL, null=True, blank=True,
                                          related_name='payments')
    notes            = models.TextField(blank=True)
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-payment_date']

    def __str__(self):
        return f'{self.reference} — {self.amount}'