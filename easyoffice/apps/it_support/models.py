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