import uuid
from decimal import Decimal
from django.db import models
from django.utils import timezone
from apps.core.models import User


class Budget(models.Model):
    class Status(models.TextChoices):
        DRAFT    = 'draft',    'Draft'
        APPROVED = 'approved', 'Approved'
        ACTIVE   = 'active',   'Active'
        CLOSED   = 'closed',   'Closed'

    id               = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name             = models.CharField(max_length=200)
    department       = models.ForeignKey('organization.Department', on_delete=models.SET_NULL, null=True, blank=True)
    unit             = models.ForeignKey('organization.Unit', on_delete=models.SET_NULL, null=True, blank=True)
    fiscal_year      = models.PositiveSmallIntegerField()
    total_amount     = models.DecimalField(max_digits=15, decimal_places=2)
    allocated_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    spent_amount     = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    status           = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    approved_by      = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='approved_budgets'
    )
    created_at       = models.DateTimeField(auto_now_add=True)

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
            'draft': '#94a3b8',
            'approved': '#10b981',
            'active': '#3b82f6',
            'closed': '#64748b',
        }.get(self.status, '#64748b')


class PurchaseRequest(models.Model):
    class Status(models.TextChoices):
        DRAFT        = 'draft',        'Draft'
        SUBMITTED    = 'submitted',    'Submitted'
        CEO_APPROVED = 'ceo_approved', 'CEO Approved'
        REJECTED     = 'rejected',     'Rejected'
        PROCESSING   = 'processing',   'Processing'
        ORDERED      = 'ordered',      'Ordered'
        DELIVERED    = 'delivered',    'Delivered'
        CLOSED       = 'closed',       'Closed'

    class Priority(models.TextChoices):
        LOW    = 'low',    'Low'
        NORMAL = 'normal', 'Normal'
        URGENT = 'urgent', 'Urgent'

    id                = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    title             = models.CharField(max_length=300)
    description       = models.TextField()
    requested_by      = models.ForeignKey(User, on_delete=models.CASCADE, related_name='purchase_requests')
    department        = models.ForeignKey('organization.Department', on_delete=models.SET_NULL, null=True, blank=True)
    budget            = models.ForeignKey(Budget, on_delete=models.SET_NULL, null=True, blank=True, related_name='purchase_requests')
    project           = models.ForeignKey('projects.Project', on_delete=models.SET_NULL, null=True, blank=True, related_name='purchase_requests')
    estimated_cost    = models.DecimalField(max_digits=12, decimal_places=2)
    actual_cost       = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    priority          = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL)
    status            = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    justification     = models.TextField()
    vendor            = models.CharField(max_length=200, blank=True)
    expected_delivery = models.DateField(null=True, blank=True)

    approved_by       = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_purchases')
    approval_date     = models.DateTimeField(null=True, blank=True)
    approval_notes    = models.TextField(blank=True)

    processed_by      = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='processed_purchase_requests')
    processed_at      = models.DateTimeField(null=True, blank=True)

    notes             = models.TextField(blank=True)
    attachment        = models.FileField(upload_to='purchase_docs/', null=True, blank=True)
    created_at        = models.DateTimeField(auto_now_add=True)
    updated_at        = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title

    @property
    def priority_color(self):
        return {
            'low': '#10b981',
            'normal': '#3b82f6',
            'urgent': '#ef4444',
        }.get(self.priority, '#64748b')

    @property
    def status_color(self):
        return {
            'draft': '#94a3b8',
            'submitted': '#f59e0b',
            'ceo_approved': '#10b981',
            'rejected': '#ef4444',
            'processing': '#8b5cf6',
            'ordered': '#8b5cf6',
            'delivered': '#06b6d4',
            'closed': '#64748b',
        }.get(self.status, '#64748b')


class EmployeeFinanceRequest(models.Model):
    class RequestType(models.TextChoices):
        LOAN             = 'loan',             'Loan'
        OVERTIME         = 'overtime',         'Overtime'
        TRANSPORT_REFUND = 'transport_refund', 'Transport Refund'
        LEAVE_SELL_BACK  = 'leave_sell_back',  'Leave Sell-Back'
        SALARY_ADVANCE   = 'salary_advance',   'Salary Advance'
        ALLOWANCE        = 'allowance',        'Allowance'
        REIMBURSEMENT    = 'reimbursement',    'Reimbursement'
        OTHER            = 'other',            'Other'

    class Status(models.TextChoices):
        DRAFT        = 'draft',        'Draft'
        SUBMITTED    = 'submitted',    'Submitted'
        CEO_APPROVED = 'ceo_approved', 'CEO Approved'
        REJECTED     = 'rejected',     'Rejected'
        PROCESSING   = 'processing',   'Processing'
        PAID         = 'paid',         'Paid'
        CLOSED       = 'closed',       'Closed'

    id                = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee          = models.ForeignKey(User, on_delete=models.CASCADE, related_name='finance_requests')
    request_type      = models.CharField(max_length=30, choices=RequestType.choices)
    title             = models.CharField(max_length=250)
    reason            = models.TextField()
    amount_requested  = models.DecimalField(max_digits=12, decimal_places=2)
    amount_approved   = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    status            = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    # leave sell-back / overtime support
    quantity          = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    unit_label        = models.CharField(max_length=50, blank=True, help_text='e.g. hours, days, trips')

    project           = models.ForeignKey('projects.Project', on_delete=models.SET_NULL, null=True, blank=True, related_name='finance_requests')
    budget            = models.ForeignKey(Budget, on_delete=models.SET_NULL, null=True, blank=True, related_name='employee_requests')

    approved_by       = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='ceo_approved_finance_requests')
    approval_date     = models.DateTimeField(null=True, blank=True)
    approval_notes    = models.TextField(blank=True)

    processed_by      = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='processed_finance_requests')
    processed_at      = models.DateTimeField(null=True, blank=True)

    linked_payment    = models.ForeignKey('Payment', on_delete=models.SET_NULL, null=True, blank=True, related_name='source_finance_requests')
    attachment        = models.FileField(upload_to='finance_request_docs/', null=True, blank=True)
    created_at        = models.DateTimeField(auto_now_add=True)
    updated_at        = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.employee} — {self.get_request_type_display()}'

    @property
    def status_color(self):
        return {
            'draft': '#94a3b8',
            'submitted': '#f59e0b',
            'ceo_approved': '#10b981',
            'rejected': '#ef4444',
            'processing': '#8b5cf6',
            'paid': '#06b6d4',
            'closed': '#64748b',
        }.get(self.status, '#64748b')


class EmployeeLoan(models.Model):
    class Status(models.TextChoices):
        ACTIVE    = 'active',    'Active'
        PAID_OFF  = 'paid_off',  'Paid Off'
        DEFAULTED = 'defaulted', 'Defaulted'
        CANCELLED = 'cancelled', 'Cancelled'

    id                = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employee          = models.ForeignKey(User, on_delete=models.CASCADE, related_name='employee_loans')
    source_request    = models.OneToOneField(EmployeeFinanceRequest, on_delete=models.SET_NULL, null=True, blank=True, related_name='loan_record')
    principal_amount  = models.DecimalField(max_digits=12, decimal_places=2)
    approved_amount   = models.DecimalField(max_digits=12, decimal_places=2)
    disbursed_amount  = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    repayment_months  = models.PositiveIntegerField(default=12)
    monthly_payment   = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    amount_repaid     = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status            = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    approved_by       = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_loans')
    created_at        = models.DateTimeField(auto_now_add=True)
    updated_at        = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Loan {self.employee} — {self.approved_amount}'

    @property
    def balance(self):
        return (self.disbursed_amount or Decimal('0')) - (self.amount_repaid or Decimal('0'))


class Payment(models.Model):
    class Method(models.TextChoices):
        BANK_TRANSFER = 'bank_transfer', 'Bank Transfer'
        CASH          = 'cash',          'Cash'
        CHEQUE        = 'cheque',        'Cheque'
        CARD          = 'card',          'Card'

    class Direction(models.TextChoices):
        COMPANY_TO_STAFF = 'company_to_staff', 'Company to Staff'
        STAFF_TO_COMPANY = 'staff_to_company', 'Staff to Company'
        COMPANY_TO_VENDOR = 'company_to_vendor', 'Company to Vendor'

    class PaymentType(models.TextChoices):
        SALARY            = 'salary',            'Salary'
        ALLOWANCE         = 'allowance',         'Allowance'
        LOAN_DISBURSEMENT = 'loan_disbursement', 'Loan Disbursement'
        LOAN_REPAYMENT    = 'loan_repayment',    'Loan Repayment'
        OVERTIME          = 'overtime',          'Overtime'
        TRANSPORT_REFUND  = 'transport_refund',  'Transport Refund'
        LEAVE_SELL_BACK   = 'leave_sell_back',   'Leave Sell-Back'
        SALARY_ADVANCE    = 'salary_advance',    'Salary Advance'
        REIMBURSEMENT     = 'reimbursement',     'Reimbursement'
        PURCHASE          = 'purchase',          'Purchase'
        OTHER             = 'other',             'Other'

    id                = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference         = models.CharField(max_length=100, unique=True)
    description       = models.TextField()
    purchase_request  = models.ForeignKey(PurchaseRequest, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')
    employee_request  = models.ForeignKey(EmployeeFinanceRequest, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')
    loan              = models.ForeignKey(EmployeeLoan, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')

    amount            = models.DecimalField(max_digits=12, decimal_places=2)
    currency          = models.CharField(max_length=10, default='USD')
    method            = models.CharField(max_length=20, choices=Method.choices)
    direction         = models.CharField(max_length=30, choices=Direction.choices, default=Direction.COMPANY_TO_VENDOR)
    payment_type      = models.CharField(max_length=30, choices=PaymentType.choices, default=PaymentType.OTHER)

    paid_by           = models.ForeignKey(User, on_delete=models.CASCADE, related_name='payments_made')
    processed_by      = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments_processed')
    approved_by       = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='approved_payments')

    employee          = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='finance_payments')
    recipient         = models.CharField(max_length=200)

    payment_date      = models.DateField()
    receipt           = models.FileField(upload_to='receipts/%Y/%m/', null=True, blank=True)
    budget            = models.ForeignKey(Budget, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')
    project           = models.ForeignKey('projects.Project', on_delete=models.SET_NULL, null=True, blank=True, related_name='finance_payments')
    notes             = models.TextField(blank=True)
    created_at        = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-payment_date', '-created_at']

    def __str__(self):
        return f'{self.reference} — {self.amount}'


class EmployeeLoanPayment(models.Model):
    class Source(models.TextChoices):
        SALARY_DEDUCTION = 'salary_deduction', 'Salary Deduction'
        CASH             = 'cash',             'Cash'
        BANK_TRANSFER    = 'bank_transfer',    'Bank Transfer'
        OTHER            = 'other',            'Other'

    id           = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    loan         = models.ForeignKey(EmployeeLoan, on_delete=models.CASCADE, related_name='repayment_rows')
    payment      = models.ForeignKey(Payment, on_delete=models.SET_NULL, null=True, blank=True, related_name='loan_repayment_rows')
    amount       = models.DecimalField(max_digits=12, decimal_places=2)
    source       = models.CharField(max_length=30, choices=Source.choices, default=Source.SALARY_DEDUCTION)
    payment_date = models.DateField(default=timezone.now)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-payment_date', '-created_at']

    def __str__(self):
        return f'{self.loan.employee} repayment — {self.amount}'