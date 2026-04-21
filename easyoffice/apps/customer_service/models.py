
import re
import uuid
from datetime import timedelta
from django.conf import settings
from django.db import models
from django.utils import timezone


User = settings.AUTH_USER_MODEL


def normalize_phone(value: str) -> str:
    value = (value or "").strip()
    digits = re.sub(r"\D", "", value)
    if not digits:
        return ""
    if digits.startswith("00"):
        digits = digits[2:]
    if not digits.startswith("220") and len(digits) == 7:
        digits = "220" + digits
    if not digits.startswith("+"):
        return "+" + digits
    return digits


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Customer(TimeStampedModel):
    CUSTOMER_TYPES = (
        ("individual", "Individual"),
        ("business", "Business"),
    )

    customer_code = models.CharField(max_length=30, unique=True, editable=False)
    customer_type = models.CharField(max_length=20, choices=CUSTOMER_TYPES, default="individual")
    full_name = models.CharField(max_length=180)
    company_name = models.CharField(max_length=180, blank=True)
    email = models.EmailField(blank=True)
    alternate_email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    area = models.CharField(max_length=100, blank=True)
    preferred_contact_method = models.CharField(
        max_length=20,
        choices=(
            ("phone", "Phone"),
            ("email", "Email"),
            ("sms", "SMS"),
            ("whatsapp", "WhatsApp"),
        ),
        default="phone",
    )
    needs_feedback = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cs_created_customers",
    )

    class Meta:
        ordering = ["full_name"]

    def save(self, *args, **kwargs):
        if not self.customer_code:
            self.customer_code = f"CUS-{timezone.now():%Y%m}-{uuid.uuid4().hex[:6].upper()}"
        super().save(*args, **kwargs)

    @property
    def display_name(self):
        return self.company_name or self.full_name

    def __str__(self):
        return self.display_name


class CustomerPhone(TimeStampedModel):
    PHONE_TYPES = (
        ("primary", "Primary"),
        ("secondary", "Secondary"),
        ("whatsapp", "WhatsApp"),
        ("office", "Office"),
    )

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="phones")
    phone_type = models.CharField(max_length=20, choices=PHONE_TYPES, default="primary")
    phone_number = models.CharField(max_length=30)
    normalized_number = models.CharField(max_length=30, unique=True, db_index=True)
    is_active = models.BooleanField(default=True)
    is_primary = models.BooleanField(default=False)

    class Meta:
        ordering = ["-is_primary", "phone_number"]

    def save(self, *args, **kwargs):
        self.normalized_number = normalize_phone(self.phone_number)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.customer} - {self.phone_number}"


class CallRecord(TimeStampedModel):
    CALL_TYPES = (
        ("sales", "Sales Inquiry"),
        ("maintenance", "Maintenance Request"),
        ("support", "Support"),
        ("complaint", "Complaint"),
        ("callback", "Callback Request"),
        ("followup", "Follow Up"),
        ("general", "General Inquiry"),
    )

    OUTCOMES = (
        ("resolved", "Resolved by Agent"),
        ("routed", "Routed to Department"),
        ("escalated", "Escalated"),
        ("callback", "Callback Needed"),
        ("forwarded", "Forwarded"),
        ("closed", "Closed"),
    )

    direction = models.CharField(
        max_length=10,
        choices=(("inbound", "Inbound"), ("outbound", "Outbound")),
        default="inbound",
    )
    phone_number = models.CharField(max_length=30)
    normalized_number = models.CharField(max_length=30, db_index=True)
    customer = models.ForeignKey(
        Customer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="calls",
    )
    handled_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="handled_customer_calls",
    )
    call_type = models.CharField(max_length=20, choices=CALL_TYPES, default="general")
    subject = models.CharField(max_length=200)
    summary = models.TextField()
    outcome = models.CharField(max_length=20, choices=OUTCOMES, blank=True)
    forwarded_to_department = models.ForeignKey(
        "organization.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="customer_service_forwarded_calls",
    )
    callback_required = models.BooleanField(default=False)
    callback_number = models.CharField(max_length=30, blank=True)
    callback_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-started_at"]

    def save(self, *args, **kwargs):
        self.normalized_number = normalize_phone(self.phone_number)
        if self.started_at and self.ended_at:
            self.duration_seconds = int((self.ended_at - self.started_at).total_seconds())
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.phone_number} - {self.subject}"


class SLAPolicy(TimeStampedModel):
    PRIORITIES = (
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("critical", "Critical"),
    )

    department = models.ForeignKey("organization.Department", on_delete=models.CASCADE, related_name="customer_service_sla_policies")
    request_type = models.CharField(max_length=30)
    priority = models.CharField(max_length=20, choices=PRIORITIES, default="medium")
    first_response_minutes = models.PositiveIntegerField(default=30)
    resolution_hours = models.PositiveIntegerField(default=24)
    escalation_after_minutes = models.PositiveIntegerField(default=60)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("department", "request_type", "priority")

    def __str__(self):
        return f"{self.department} - {self.request_type} - {self.priority}"


class ServiceTicket(TimeStampedModel):
    TICKET_TYPES = (
        ("sales", "Sales"),
        ("maintenance", "Maintenance"),
        ("support", "Support"),
        ("complaint", "Complaint"),
        ("callback", "Callback"),
        ("product", "Product Inquiry"),
        ("general", "General"),
    )

    PRIORITIES = (
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("critical", "Critical"),
    )

    STATUSES = (
        ("new", "New"),
        ("open", "Open"),
        ("pending", "Pending"),
        ("assigned", "Assigned"),
        ("in_progress", "In Progress"),
        ("awaiting_customer", "Awaiting Customer"),
        ("resolved", "Resolved"),
        ("closed", "Closed"),
        ("escalated", "Escalated"),
        ("cancelled", "Cancelled"),
    )

    ticket_no = models.CharField(max_length=30, unique=True, editable=False)
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="tickets")
    call = models.ForeignKey(CallRecord, on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets")
    ticket_type = models.CharField(max_length=20, choices=TICKET_TYPES, default="general")
    priority = models.CharField(max_length=20, choices=PRIORITIES, default="medium")
    status = models.CharField(max_length=20, choices=STATUSES, default="new")
    subject = models.CharField(max_length=220)
    description = models.TextField()
    department = models.ForeignKey(
        "organization.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="customer_service_tickets",
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_customer_service_tickets",
    )
    current_owner = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_customer_service_tickets",
    )
    resolved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_customer_service_tickets",
    )
    response_due_at = models.DateTimeField(null=True, blank=True)
    resolution_due_at = models.DateTimeField(null=True, blank=True)
    escalated_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    callback_required = models.BooleanField(default=False)
    callback_number = models.CharField(max_length=30, blank=True)
    callback_email = models.EmailField(blank=True)
    forwarded_call = models.BooleanField(default=False)
    requires_feedback = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.ticket_no:
            self.ticket_no = f"TKT-{timezone.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}"
        super().save(*args, **kwargs)

    @property
    def is_overdue(self):
        return bool(self.resolution_due_at and self.status not in ["resolved", "closed"] and timezone.now() > self.resolution_due_at)

    def __str__(self):
        return self.ticket_no


class ServiceTicketRouting(TimeStampedModel):
    ACTIONS = (
        ("route", "Route"),
        ("escalate", "Escalate"),
        ("forward_call", "Forward Call"),
        ("callback_request", "Callback Request"),
    )

    ticket = models.ForeignKey(ServiceTicket, on_delete=models.CASCADE, related_name="routes")
    action = models.CharField(max_length=20, choices=ACTIONS)
    from_department = models.ForeignKey(
        "organization.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="customer_service_routed_from",
    )
    to_department = models.ForeignKey(
        "organization.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="customer_service_routed_to",
    )
    from_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="customer_service_routes_from_user",
    )
    to_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="customer_service_routes_to_user",
    )
    reason = models.TextField(blank=True)
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]


class ServiceTicketAssignment(TimeStampedModel):
    TASK_STATUS = (
        ("new", "New"),
        ("assigned", "Assigned"),
        ("in_progress", "In Progress"),
        ("waiting", "Waiting"),
        ("resolved", "Resolved"),
        ("closed", "Closed"),
        ("overdue", "Overdue"),
    )

    ticket = models.ForeignKey(ServiceTicket, on_delete=models.CASCADE, related_name="assignments")
    department = models.ForeignKey("organization.Department", on_delete=models.CASCADE, related_name="customer_service_assignments")
    assigned_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="customer_service_assignments_by",
    )
    assigned_to = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="customer_service_assignments_to",
    )
    title = models.CharField(max_length=220)
    instructions = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=TASK_STATUS, default="new")
    sla_due_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    task_ref = models.ForeignKey(
        "tasks.Task",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="customer_service_assignments",
    )

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_overdue(self):
        return bool(self.sla_due_at and self.status not in ["resolved", "closed"] and timezone.now() > self.sla_due_at)

    def __str__(self):
        return self.title


class ServiceTicketUpdate(TimeStampedModel):
    ticket = models.ForeignKey(ServiceTicket, on_delete=models.CASCADE, related_name="updates")
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    update_type = models.CharField(
        max_length=30,
        choices=(
            ("note", "Note"),
            ("status_change", "Status Change"),
            ("customer_update", "Customer Update"),
            ("resolution", "Resolution"),
            ("escalation", "Escalation"),
            ("callback", "Callback"),
        ),
        default="note",
    )
    body = models.TextField()

    class Meta:
        ordering = ["created_at"]


class FeedbackRequest(TimeStampedModel):
    CHANNELS = (
        ("email", "Email"),
        ("sms", "SMS"),
    )

    ticket = models.OneToOneField(ServiceTicket, on_delete=models.CASCADE, related_name="feedback_request")
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="feedback_requests")
    channel = models.CharField(max_length=10, choices=CHANNELS, default="email")
    sent_to = models.CharField(max_length=255, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    def __str__(self):
        return f"Feedback for {self.ticket.ticket_no}"


class ServiceRating(TimeStampedModel):
    ticket = models.OneToOneField(ServiceTicket, on_delete=models.CASCADE, related_name="rating")
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="ratings")
    service_quality = models.PositiveSmallIntegerField(default=0)
    product_quality = models.PositiveSmallIntegerField(default=0)
    response_time = models.PositiveSmallIntegerField(default=0)
    professionalism = models.PositiveSmallIntegerField(default=0)
    comment = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Rating - {self.ticket.ticket_no}"
