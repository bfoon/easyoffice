import uuid
from django.db import models
from apps.core.models import User


class ITRequest(models.Model):
    class Type(models.TextChoices):
        CREATE_USER = 'create_user', 'Create User Account'
        RESET_PASSWORD = 'reset_password', 'Password Reset'
        ISSUE_EQUIPMENT = 'issue_equipment', 'Issue Equipment'
        RETURN_EQUIPMENT = 'return_equipment', 'Return Equipment'
        CREATE_EMAIL = 'create_email', 'Create Email Account'
        SOFTWARE_INSTALL = 'software_install', 'Software Installation'
        TECHNICAL_SUPPORT = 'technical_support', 'Technical Support'
        NETWORK_ACCESS = 'network_access', 'Network Access'
        OTHER = 'other', 'Other'

    class Priority(models.TextChoices):
        LOW = 'low', 'Low'
        NORMAL = 'normal', 'Normal'
        HIGH = 'high', 'High'
        CRITICAL = 'critical', 'Critical'

    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        IN_PROGRESS = 'in_progress', 'In Progress'
        PENDING_INFO = 'pending_info', 'Pending Information'
        RESOLVED = 'resolved', 'Resolved'
        CLOSED = 'closed', 'Closed'
        CANCELLED = 'cancelled', 'Cancelled'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket_number = models.CharField(max_length=20, unique=True)
    request_type = models.CharField(max_length=30, choices=Type.choices)
    title = models.CharField(max_length=300)
    description = models.TextField()
    requested_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='it_requests')
    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='it_tickets_assigned')
    priority = models.CharField(max_length=20, choices=Priority.choices, default=Priority.NORMAL)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    resolution = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    sla_hours = models.PositiveSmallIntegerField(default=24)
    attachment = models.FileField(upload_to='it_requests/', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'[{self.ticket_number}] {self.title}'

    @property
    def priority_color(self):
        return {
            'low': '#10b981', 'normal': '#3b82f6',
            'high': '#f59e0b', 'critical': '#ef4444',
        }.get(self.priority, '#64748b')

    @property
    def status_color(self):
        return {
            'open': '#3b82f6', 'in_progress': '#8b5cf6',
            'pending_info': '#f59e0b', 'resolved': '#10b981',
            'closed': '#64748b', 'cancelled': '#ef4444',
        }.get(self.status, '#64748b')

    @property
    def is_open(self):
        return self.status in ('open', 'in_progress', 'pending_info')


class TicketComment(models.Model):
    """Discussion thread on a ticket — visible to requester and IT staff."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    ticket = models.ForeignKey(ITRequest, on_delete=models.CASCADE, related_name='comments')
    author = models.ForeignKey(User, on_delete=models.CASCADE)
    content = models.TextField()
    is_internal = models.BooleanField(
        default=False,
        help_text='Internal notes are only visible to IT staff'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.ticket.ticket_number} — {self.author}'


class Equipment(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = 'available', 'Available'
        ISSUED = 'issued', 'Issued'
        MAINTENANCE = 'maintenance', 'In Maintenance'
        RETIRED = 'retired', 'Retired'
        LOST = 'lost', 'Lost/Stolen'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    asset_tag = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=100)
    make = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)
    serial_number = models.CharField(max_length=100, blank=True)
    purchase_date = models.DateField(null=True, blank=True)
    purchase_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.AVAILABLE)
    current_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name='issued_equipment')
    issued_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'[{self.asset_tag}] {self.name}'

    @property
    def status_color(self):
        return {
            'available': '#10b981', 'issued': '#3b82f6',
            'maintenance': '#f59e0b', 'retired': '#64748b', 'lost': '#ef4444',
        }.get(self.status, '#64748b')
# =============================================================================
# Equipment Maintenance Management
# =============================================================================

from decimal import Decimal
from django.core.validators import MinValueValidator
from django.db import transaction
from django.utils import timezone


class MaintenanceCounter(models.Model):
    """Thread-safe yearly sequence for maintenance numbers."""
    year = models.PositiveIntegerField(unique=True)
    last_sequence = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def next_number(cls):
        year = timezone.localdate().year
        with transaction.atomic():
            row, _ = cls.objects.select_for_update().get_or_create(
                year=year, defaults={'last_sequence': 0}
            )
            row.last_sequence += 1
            row.save(update_fields=['last_sequence', 'updated_at'])
        return f'MNT-{year}-{row.last_sequence:05d}'


class MaintenanceJob(models.Model):
    """
    Full repair/maintenance record for company assets and walk-in equipment.

    The owner can follow the public timeline through MaintenanceAccessToken.
    Financial documents are generated by apps.invoices.InvoiceDocument.
    """

    class Status(models.TextChoices):
        INTAKE = 'intake', 'Received / Intake'
        ASSIGNED = 'assigned', 'Assigned to Technician'
        DIAGNOSIS = 'diagnosis', 'Diagnosis in Progress'
        AWAITING_APPROVAL = 'awaiting_approval', 'Awaiting Customer Approval'
        WAITING_PARTS = 'waiting_parts', 'Waiting for Parts'
        IN_REPAIR = 'in_repair', 'Repair in Progress'
        QUALITY_CHECK = 'quality_check', 'Quality Check'
        READY_PICKUP = 'ready_pickup', 'Ready for Pickup'
        COLLECTED = 'collected', 'Collected'
        CLOSED = 'closed', 'Closed'
        UNREPAIRABLE = 'unrepairable', 'Unrepairable'
        CANCELLED = 'cancelled', 'Cancelled'

    class Priority(models.TextChoices):
        LOW = 'low', 'Low'
        NORMAL = 'normal', 'Normal'
        HIGH = 'high', 'High'
        URGENT = 'urgent', 'Urgent'

    class PaymentStatus(models.TextChoices):
        NOT_REQUIRED = 'not_required', 'No Payment Required'
        UNPAID = 'unpaid', 'Unpaid'
        PARTIAL = 'partial', 'Partially Paid'
        PAID = 'paid', 'Paid'
        REFUNDED = 'refunded', 'Refunded'

    class IntakeChannel(models.TextChoices):
        WALK_IN = 'walk_in', 'Walk-In'
        STAFF = 'staff', 'Staff / Internal Asset'
        PHONE = 'phone', 'Phone Call'
        EMAIL = 'email', 'Email'
        FIELD = 'field', 'Field Collection'
        OTHER = 'other', 'Other'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    maintenance_number = models.CharField(max_length=30, unique=True, blank=True)

    # Optional links to existing records.
    ticket = models.ForeignKey(
        ITRequest, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='maintenance_jobs'
    )
    equipment = models.ForeignKey(
        Equipment, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='maintenance_jobs'
    )

    # Snapshot of the equipment at intake; supports external/walk-in equipment.
    equipment_name = models.CharField(max_length=200)
    equipment_category = models.CharField(max_length=100, blank=True)
    make = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)
    serial_number = models.CharField(max_length=120, blank=True, db_index=True)
    asset_tag = models.CharField(max_length=80, blank=True, db_index=True)

    # Owner/customer. owner_user is optional so walk-in customers are supported.
    #
    # Two distinct notions of "who owns this", deliberately kept separate:
    #   owner_user → internal staff member (an office laptop). Not a contact.
    #   customer   → external contact in the customer-service book (walk-in).
    # A job has at most one of these set; both may be NULL for a one-off the
    # desk chose not to file. See apps/it_support/customer_link.py.
    #
    # The owner_* text fields below are a SNAPSHOT taken at intake, not a
    # cache of the FK. They record who actually handed the equipment over. If
    # a customer later changes their company name, historical service records
    # — which may carry a signature — must not silently rewrite themselves.
    owner_user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='owned_maintenance_jobs'
    )
    customer = models.ForeignKey(
        'customer_service.Customer', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='maintenance_jobs',
        help_text='Customer-service contact this equipment owner corresponds to.',
    )
    owner_name = models.CharField(max_length=200)
    owner_email = models.EmailField(blank=True)
    owner_phone = models.CharField(max_length=50, blank=True)
    owner_company = models.CharField(max_length=200, blank=True)
    owner_address = models.TextField(blank=True)

    intake_channel = models.CharField(
        max_length=20, choices=IntakeChannel.choices,
        default=IntakeChannel.WALK_IN
    )
    priority = models.CharField(
        max_length=20, choices=Priority.choices, default=Priority.NORMAL
    )
    status = models.CharField(
        max_length=30, choices=Status.choices, default=Status.INTAKE,
        db_index=True
    )

    created_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name='created_maintenance_jobs'
    )
    assigned_to = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='assigned_maintenance_jobs'
    )

    # Intake, diagnosis, and repair records.
    reported_problem = models.TextField()
    condition_received = models.TextField(blank=True)
    accessories_received = models.TextField(blank=True)
    data_privacy_consent = models.BooleanField(default=False)
    backup_authorized = models.BooleanField(default=False)
    diagnosis = models.TextField(blank=True)
    work_performed = models.TextField(blank=True)
    parts_used = models.TextField(blank=True)
    quality_check_notes = models.TextField(blank=True)
    owner_visible_note = models.TextField(blank=True)
    internal_note = models.TextField(blank=True)

    # Customer approval.
    quote_approved_at = models.DateTimeField(null=True, blank=True)
    quote_declined_at = models.DateTimeField(null=True, blank=True)
    quote_response_note = models.TextField(blank=True)

    # SLA and pickup deadlines.
    opened_at = models.DateTimeField(default=timezone.now)
    assigned_at = models.DateTimeField(null=True, blank=True)
    diagnosis_due_at = models.DateTimeField(null=True, blank=True)
    promised_completion_at = models.DateTimeField(null=True, blank=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    pickup_deadline_at = models.DateTimeField(null=True, blank=True, db_index=True)
    collected_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    # Financial summary.
    currency = models.CharField(max_length=3, default='GMD')
    tax_rate = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))]
    )
    discount_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))]
    )
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    tax_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    amount_paid = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('0.00'))]
    )
    payment_status = models.CharField(
        max_length=20, choices=PaymentStatus.choices,
        default=PaymentStatus.UNPAID
    )
    payment_method = models.CharField(max_length=40, blank=True)
    payment_reference = models.CharField(max_length=120, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    invoice = models.ForeignKey(
        'invoices.InvoiceDocument', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='maintenance_invoices'
    )
    receipt = models.ForeignKey(
        'invoices.InvoiceDocument', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='maintenance_receipts'
    )

    warranty_days = models.PositiveIntegerField(default=30)
    warranty_until = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'priority']),
            models.Index(fields=['assigned_to', 'status']),
            models.Index(fields=['pickup_deadline_at', 'status']),
            models.Index(fields=['promised_completion_at', 'status']),
            # Customer history lookups (CustomerDetailView lists a contact's jobs).
            models.Index(fields=['customer', '-created_at']),
        ]

    def __str__(self):
        return f'{self.maintenance_number} — {self.equipment_name}'

    def save(self, *args, **kwargs):
        if not self.maintenance_number:
            self.maintenance_number = MaintenanceCounter.next_number()
        super().save(*args, **kwargs)

    @property
    def owner_display_name(self):
        # The intake snapshot wins: it's what the owner was called when they
        # handed the equipment over, and it's what's printed on the service
        # record they signed. Falls back to the linked records only when the
        # snapshot is empty.
        if self.owner_name:
            return self.owner_name
        if self.owner_user:
            return getattr(self.owner_user, 'full_name', str(self.owner_user))
        if self.customer_id:
            return self.customer.display_name
        return ''

    @property
    def effective_owner_email(self):
        if self.owner_email:
            return self.owner_email
        if self.owner_user and getattr(self.owner_user, 'email', ''):
            return self.owner_user.email
        if self.customer_id and self.customer.email:
            return self.customer.email
        return ''

    @property
    def owner_link_label(self):
        """Human label for who this job's owner is linked to, for the UI."""
        if self.customer_id:
            return f'Customer · {self.customer.customer_code}'
        if self.owner_user_id:
            return 'Internal staff'
        return 'Unlinked walk-in'

    @property
    def is_terminal(self):
        # The owner tracking link and record remain active until IT formally
        # closes the job, including unrepairable/cancelled items awaiting return.
        return self.status == self.Status.CLOSED

    @property
    def is_overdue(self):
        return bool(
            self.promised_completion_at
            and self.promised_completion_at < timezone.now()
            and self.status not in {
                self.Status.READY_PICKUP, self.Status.COLLECTED,
                self.Status.CLOSED, self.Status.CANCELLED,
                self.Status.UNREPAIRABLE,
            }
        )

    @property
    def pickup_is_overdue(self):
        return bool(
            self.pickup_deadline_at
            and self.pickup_deadline_at < timezone.now()
            and self.status == self.Status.READY_PICKUP
        )

    @property
    def balance_due(self):
        return max(Decimal('0.00'), self.total_amount - self.amount_paid)

    @property
    def progress_percent(self):
        order = [
            self.Status.INTAKE, self.Status.ASSIGNED, self.Status.DIAGNOSIS,
            self.Status.AWAITING_APPROVAL, self.Status.WAITING_PARTS,
            self.Status.IN_REPAIR, self.Status.QUALITY_CHECK,
            self.Status.READY_PICKUP, self.Status.COLLECTED, self.Status.CLOSED,
        ]
        if self.status in (self.Status.CANCELLED, self.Status.UNREPAIRABLE):
            return 100
        try:
            return round(order.index(self.status) / (len(order) - 1) * 100)
        except ValueError:
            return 0

    @property
    def status_color(self):
        return {
            self.Status.INTAKE: '#64748b',
            self.Status.ASSIGNED: '#3b82f6',
            self.Status.DIAGNOSIS: '#8b5cf6',
            self.Status.AWAITING_APPROVAL: '#f59e0b',
            self.Status.WAITING_PARTS: '#f97316',
            self.Status.IN_REPAIR: '#06b6d4',
            self.Status.QUALITY_CHECK: '#0ea5e9',
            self.Status.READY_PICKUP: '#10b981',
            self.Status.COLLECTED: '#14b8a6',
            self.Status.CLOSED: '#475569',
            self.Status.UNREPAIRABLE: '#ef4444',
            self.Status.CANCELLED: '#94a3b8',
        }.get(self.status, '#64748b')

    def recalculate_totals(self, save=True):
        subtotal = sum(
            (line.line_total for line in self.charge_lines.filter(billable=True)),
            Decimal('0.00')
        )
        tax = (subtotal * self.tax_rate / Decimal('100')).quantize(Decimal('0.01'))
        total = (subtotal + tax - self.discount_amount).quantize(Decimal('0.01'))
        self.subtotal = subtotal.quantize(Decimal('0.01'))
        self.tax_amount = tax
        self.total_amount = max(Decimal('0.00'), total)

        if self.total_amount == Decimal('0.00'):
            self.payment_status = self.PaymentStatus.NOT_REQUIRED
        elif self.amount_paid >= self.total_amount:
            self.payment_status = self.PaymentStatus.PAID
        elif self.amount_paid > 0:
            self.payment_status = self.PaymentStatus.PARTIAL
        else:
            self.payment_status = self.PaymentStatus.UNPAID

        if save:
            self.save(update_fields=[
                'subtotal', 'tax_amount', 'total_amount',
                'payment_status', 'updated_at'
            ])
        return self.total_amount


class MaintenanceChargeLine(models.Model):
    class ChargeType(models.TextChoices):
        DIAGNOSIS = 'diagnosis', 'Diagnosis'
        LABOUR = 'labour', 'Labour'
        PART = 'part', 'Part / Material'
        SERVICE = 'service', 'Service'
        OTHER = 'other', 'Other'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(
        MaintenanceJob, on_delete=models.CASCADE, related_name='charge_lines'
    )
    position = models.PositiveIntegerField(default=0)
    charge_type = models.CharField(
        max_length=20, choices=ChargeType.choices, default=ChargeType.SERVICE
    )
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('1.00'))
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    line_total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    billable = models.BooleanField(default=True)
    covered_by_warranty = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['position', 'created_at']

    def save(self, *args, **kwargs):
        self.line_total = (
            Decimal(self.quantity) * Decimal(self.unit_price)
        ).quantize(Decimal('0.01'))
        super().save(*args, **kwargs)
        self.job.recalculate_totals(save=True)

    def delete(self, *args, **kwargs):
        job = self.job
        result = super().delete(*args, **kwargs)
        job.recalculate_totals(save=True)
        return result


class MaintenanceEvent(models.Model):
    class Kind(models.TextChoices):
        CREATED = 'created', 'Created'
        STATUS = 'status', 'Status Changed'
        ASSIGNED = 'assigned', 'Assigned'
        NOTE = 'note', 'Note Added'
        APPROVAL = 'approval', 'Customer Approval'
        DEADLINE = 'deadline', 'Deadline Updated'
        PAYMENT = 'payment', 'Payment Recorded'
        INVOICE = 'invoice', 'Invoice Generated'
        RECEIPT = 'receipt', 'Receipt Generated'
        NOTIFICATION = 'notification', 'Notification Sent'
        TOKEN = 'token', 'Tracking Link Updated'
        ATTACHMENT = 'attachment', 'Attachment Added'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(
        MaintenanceJob, on_delete=models.CASCADE, related_name='events'
    )
    kind = models.CharField(max_length=30, choices=Kind.choices)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    old_status = models.CharField(max_length=30, blank=True)
    new_status = models.CharField(max_length=30, blank=True)
    message = models.TextField()
    is_public = models.BooleanField(default=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']


class MaintenanceAttachment(models.Model):
    """
    Evidence attached to a maintenance record.

    Two sources, one model:
      • Uploaded from the technician's computer → `file` holds the upload.
      • Picked from the files app              → `shared_file` points at the
        existing SharedFile and `file` stays empty.

    Picking references rather than copies: a 4MB intake photo already in the
    files app shouldn't be duplicated on disk, and the file keeps its single
    version history / audit trail there. Use `.url`, `.display_name` and
    `.size_bytes` below instead of touching `.file` directly, so callers
    don't have to care which source a row came from.
    """

    class Category(models.TextChoices):
        INTAKE = 'intake', 'Intake / Condition'
        DIAGNOSIS = 'diagnosis', 'Diagnosis'
        REPAIR = 'repair', 'Repair'
        QUALITY_CHECK = 'quality_check', 'Quality Check'
        CUSTOMER = 'customer', 'Customer Document'
        OTHER = 'other', 'Other'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(
        MaintenanceJob, on_delete=models.CASCADE, related_name='attachments'
    )
    category = models.CharField(
        max_length=30, choices=Category.choices, default=Category.OTHER
    )
    # Blank when the evidence is a reference to a files-app document.
    file = models.FileField(upload_to='it_maintenance/%Y/%m/', blank=True)
    # SET_NULL, not CASCADE: if someone deletes the source document in the
    # files app, the maintenance record must keep its audit row (with the
    # caption and who added it) rather than silently losing evidence.
    shared_file = models.ForeignKey(
        'files.SharedFile', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='maintenance_attachments',
        help_text='Set when this evidence references a document in the files app.',
    )
    caption = models.CharField(max_length=250, blank=True)
    is_public = models.BooleanField(default=False)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        constraints = [
            # Forbid only the AMBIGUOUS case (both sources set) — never
            # require one to be present.
            #
            # The obvious "exactly one of" constraint is a trap here:
            # shared_file is SET_NULL, so deleting a referenced document in
            # the files app would leave file='' AND shared_file=NULL. A
            # CHECK demanding one-or-the-other is enforced on UPDATE, so
            # Postgres would raise mid-cascade and make any file referenced
            # by a maintenance record permanently undeletable. The orphan
            # state is legitimate and expected — see `is_missing`.
            models.CheckConstraint(
                check=~(models.Q(shared_file__isnull=False) & ~models.Q(file='')),
                name='maintenance_attachment_single_source',
            ),
        ]

    def __str__(self):
        return f'{self.job_id} — {self.display_name}'

    @property
    def is_reference(self):
        """True when this points at a files-app document instead of an upload."""
        return self.shared_file_id is not None

    @property
    def is_missing(self):
        """Source document was deleted from the files app after linking."""
        return not self.file and self.shared_file_id is None

    @property
    def display_name(self):
        if self.caption:
            return self.caption
        if self.shared_file_id:
            return self.shared_file.name
        if self.file:
            return self.file.name.rsplit('/', 1)[-1]
        return 'Missing file'

    @property
    def url(self):
        """
        Where to fetch this attachment.

        Files-app documents go through the files app's own download view so
        its permission checks and download counter still apply — never a
        direct media URL that would bypass them.
        """
        if self.shared_file_id:
            from django.urls import reverse
            return reverse('file_download', kwargs={'pk': self.shared_file_id})
        if self.file:
            return self.file.url
        return ''

    @property
    def size_bytes(self):
        if self.shared_file_id:
            return self.shared_file.file_size or 0
        try:
            return self.file.size if self.file else 0
        except Exception:
            return 0


class MaintenanceAccessToken(models.Model):
    """Public owner tracking token. Revoked automatically when a job closes."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.OneToOneField(
        MaintenanceJob, on_delete=models.CASCADE, related_name='tracking_token'
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    view_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def is_valid(self):
        if not self.is_active or self.revoked_at:
            return False
        if self.expires_at and self.expires_at <= timezone.now():
            return False
        return not self.job.is_terminal

    def revoke(self):
        self.is_active = False
        self.revoked_at = timezone.now()
        self.save(update_fields=['is_active', 'revoked_at'])

    def rotate(self):
        self.token = uuid.uuid4()
        self.is_active = True
        self.revoked_at = None
        self.expires_at = None
        self.save(update_fields=['token', 'is_active', 'revoked_at', 'expires_at'])
        return self.token


class MaintenanceReminder(models.Model):
    """Persistent deadline/reminder queue processed by Celery or a cron command."""

    class Target(models.TextChoices):
        OWNER = 'owner', 'Equipment Owner'
        TECHNICIAN = 'technician', 'Assigned Technician'
        IT = 'it', 'IT Team'

    class Channel(models.TextChoices):
        EMAIL = 'email', 'Email'
        IN_APP = 'in_app', 'In-App'
        BOTH = 'both', 'Email and In-App'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(
        MaintenanceJob, on_delete=models.CASCADE, related_name='reminders'
    )
    key = models.CharField(max_length=80)
    target = models.CharField(max_length=20, choices=Target.choices)
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.BOTH)
    subject = models.CharField(max_length=250)
    message = models.TextField()
    next_send_at = models.DateTimeField(db_index=True)
    repeat_every_hours = models.PositiveIntegerField(default=0)
    max_sends = models.PositiveIntegerField(default=1)
    send_count = models.PositiveIntegerField(default=0)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['next_send_at']
        constraints = [
            models.UniqueConstraint(fields=['job', 'key'], name='uniq_maintenance_reminder_key')
        ]


class MaintenanceNotificationLog(models.Model):
    class Status(models.TextChoices):
        SENT = 'sent', 'Sent'
        FAILED = 'failed', 'Failed'
        SKIPPED = 'skipped', 'Skipped'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(
        MaintenanceJob, on_delete=models.CASCADE, related_name='notification_logs'
    )
    event_type = models.CharField(max_length=80)
    channel = models.CharField(max_length=30)
    recipient = models.CharField(max_length=250, blank=True)
    subject = models.CharField(max_length=250, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
