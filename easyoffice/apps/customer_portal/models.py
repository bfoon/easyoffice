"""
apps/customer_portal/models.py
==============================

Models for the contract-driven customer self-service portal.

The portal is deliberately separate from the staff-facing customer_service app
because:

  * Portal pages are accessed by external customers via a one-time tokenised
    URL — there is NO Django login. Mixing token-auth views with
    LoginRequiredMixin views in the same module is the easiest way to leak a
    permission check by accident.
  * The portal has its own object lifecycle (token, maintenance schedule,
    on-site events) that the staff CS module shouldn't have to know about.

What lives here:

  * ContractContact          — one row per portal-eligible person on a contract.
                               (A company can have multiple contacts.)
  * PortalAccessToken        — a UUID-based token tied to (contact, contract).
                               Issued on contract creation; revoked + reissued
                               on renewal. Expires when the contract ends.
  * DeviceBinding            — fingerprint of a verified machine for a given
                               token. New machines must clear an OTP gate
                               before they can use the token.
  * DeviceOTP                — one-time codes emailed to the contact for
                               device verification.
  * MaintenanceSchedule      — created only for maintenance contracts.
                               Holds cadence (monthly / quarterly / etc.) and
                               points at the next due date.
  * MaintenanceVisit         — one record per actual or planned PM visit.
                               Linked to the Task the technician works against.
  * OnSiteEvent              — every "I'm on site" / "I've left site" tap.
                               Mirrored to the linked ticket assignment for the
                               staff side, kept here for the portal timeline.
  * PortalSupportRequest     — what the customer submits through the portal.
                               Auto-creates a ServiceTicket on the staff side.
                               Now carries its own lifecycle (open → closed,
                               cancellable, ratable, reopenable).
  * TicketComment            — a customer or staff message on a request.
  * TicketAttachment         — a file / photo uploaded against a request.
  * TicketRating             — 1-5 star rating + optional written feedback.
  * ReopenRequest            — customer asks for a resolved ticket to be
                               reopened, with reason.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


# ─────────────────────────────────────────────────────────────────────────────
# Configurable model labels
# ─────────────────────────────────────────────────────────────────────────────

CONTRACT_MODEL = getattr(
    settings, 'CUSTOMER_PORTAL_CONTRACT_MODEL', 'finance.Contract'
)
TASK_MODEL = getattr(
    settings, 'CUSTOMER_PORTAL_TASK_MODEL', 'tasks.Task'
)
TICKET_MODEL = getattr(
    settings, 'CUSTOMER_PORTAL_TICKET_MODEL', 'customer_service.ServiceTicket'
)
ASSIGNMENT_MODEL = getattr(
    settings, 'CUSTOMER_PORTAL_ASSIGNMENT_MODEL',
    'customer_service.ServiceTicketAssignment',
)
CUSTOMER_MODEL = getattr(
    settings, 'CUSTOMER_PORTAL_CUSTOMER_MODEL', 'customer_service.Customer'
)

User = settings.AUTH_USER_MODEL


# ─────────────────────────────────────────────────────────────────────────────
# Mixins
# ─────────────────────────────────────────────────────────────────────────────

class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ─────────────────────────────────────────────────────────────────────────────
# Contract contacts
# ─────────────────────────────────────────────────────────────────────────────

class ContractContact(TimeStampedModel):
    """
    A person on the customer side who should have portal access for a given
    contract. There can be multiple contacts per contract — Operations,
    Finance, etc. each get their own token.
    """
    ROLE_CHOICES = (
        ('primary',     'Primary'),
        ('operations',  'Operations'),
        ('finance',     'Finance / Billing'),
        ('technical',   'Technical'),
        ('other',       'Other'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contract = models.ForeignKey(
        CONTRACT_MODEL,
        on_delete=models.CASCADE,
        related_name='portal_contacts',
    )
    customer = models.ForeignKey(
        CUSTOMER_MODEL,
        on_delete=models.CASCADE,
        related_name='contract_contacts',
        help_text='The customer_service.Customer this contact belongs to.',
    )

    full_name = models.CharField(max_length=180)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='primary')

    receives_pm_notices = models.BooleanField(
        default=True,
        help_text='Email this contact when preventive maintenance is scheduled '
                  'or a technician is dispatched.',
    )
    receives_billing_notices = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['contract', 'role', 'full_name']
        constraints = [
            models.UniqueConstraint(
                fields=['contract', 'email'],
                name='unique_contact_email_per_contract',
            ),
        ]
        indexes = [
            models.Index(fields=['email']),
            models.Index(fields=['contract', 'is_active']),
        ]

    def __str__(self):
        return f'{self.full_name} <{self.email}> [{self.contract_id}]'

    @property
    def active_token(self):
        return (
            self.tokens
            .filter(revoked_at__isnull=True, valid_until__gt=timezone.now())
            .order_by('-valid_until')
            .first()
        )


# ─────────────────────────────────────────────────────────────────────────────
# Portal access tokens
# ─────────────────────────────────────────────────────────────────────────────

def _make_token() -> str:
    return secrets.token_urlsafe(48)


class PortalAccessToken(TimeStampedModel):
    """
    A token grants ONE contact ONE-link access to the portal for the
    duration of ONE contract period.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token = models.CharField(
        max_length=128,
        unique=True,
        default=_make_token,
        editable=False,
    )

    contact = models.ForeignKey(
        ContractContact,
        on_delete=models.CASCADE,
        related_name='tokens',
    )
    contract = models.ForeignKey(
        CONTRACT_MODEL,
        on_delete=models.CASCADE,
        related_name='portal_tokens',
    )

    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(
        help_text='Set to contract.end_date + 1 day at 23:59 by signal.',
    )

    issued_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    use_count = models.PositiveIntegerField(default=0)

    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['-issued_at']
        indexes = [
            models.Index(fields=['contact', 'revoked_at']),
            models.Index(fields=['contract', 'revoked_at']),
        ]

    def __str__(self):
        return f'Token for {self.contact_id} (contract {self.contract_id})'

    @property
    def is_active(self) -> bool:
        if self.revoked_at:
            return False
        now = timezone.now()
        return self.valid_from <= now <= self.valid_until

    def revoke(self, reason: str = '') -> None:
        if self.revoked_at:
            return
        self.revoked_at = timezone.now()
        self.revoked_reason = (reason or '')[:200]
        self.save(update_fields=['revoked_at', 'revoked_reason', 'updated_at'])

    def touch(self) -> None:
        PortalAccessToken.objects.filter(pk=self.pk).update(
            last_used_at=timezone.now(),
            use_count=models.F('use_count') + 1,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Device binding & OTP verification
# ─────────────────────────────────────────────────────────────────────────────
#
# WHY:
#   The portal token alone is enough to access the portal — but if a customer
#   forwards their email to a colleague (or worse, the link gets indexed in
#   somebody's browser sync), we need a second factor that ties usage to a
#   specific machine. A "device fingerprint" (UA + accept-lang + a stable
#   client-side seed stored in localStorage) is far from cryptographic, but
#   it's good enough to detect "this is a NEW machine" and force a one-time
#   email OTP before access is granted.
#
# FLOW:
#   1. First visit: portal computes a fingerprint → no DeviceBinding for this
#      (token, fingerprint) → render verify_device.html → email a 6-digit OTP
#      to the contact's email on record.
#   2. Customer enters the code → we create a DeviceBinding marked verified.
#      A signed cookie + the localStorage seed make sure the same browser
#      keeps working without re-prompting.
#   3. Switching to another machine? New fingerprint → step 1 repeats.
#   4. Bindings can be revoked from the staff admin if a device is lost.

def _make_device_seed() -> str:
    """Random opaque blob the client stores in localStorage."""
    return secrets.token_urlsafe(24)


def _hash_fingerprint(*parts: str) -> str:
    """
    Deterministic hash of the parts that make up a device's identity.
    We hash so we never persist the raw UA/IP/seed in plaintext — privacy +
    storage hygiene.
    """
    blob = '|'.join((p or '') for p in parts).encode('utf-8')
    return hashlib.sha256(blob).hexdigest()


class DeviceBinding(TimeStampedModel):
    """
    One row per (token, device fingerprint) pair that has cleared OTP
    verification. A token can have many bindings — that's fine; a customer
    who works from laptop + phone shouldn't have to fight us.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token = models.ForeignKey(
        PortalAccessToken,
        on_delete=models.CASCADE,
        related_name='device_bindings',
    )

    # Hash, not raw — see _hash_fingerprint above.
    fingerprint_hash = models.CharField(max_length=64, db_index=True)

    # Human-readable hints so staff can recognise the device in the admin
    # ("MacBook · Chrome 123 · Banjul").
    label = models.CharField(max_length=120, blank=True)
    user_agent = models.CharField(max_length=400, blank=True)
    ip_first_seen = models.GenericIPAddressField(null=True, blank=True)
    ip_last_seen = models.GenericIPAddressField(null=True, blank=True)

    verified_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    use_count = models.PositiveIntegerField(default=1)

    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['-last_seen_at']
        constraints = [
            models.UniqueConstraint(
                fields=['token', 'fingerprint_hash'],
                name='unique_device_per_token',
            ),
        ]
        indexes = [
            models.Index(fields=['token', 'revoked_at']),
        ]

    def __str__(self):
        return f'Device for token {self.token_id} — {self.label or self.fingerprint_hash[:12]}'

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def touch(self, *, ip: str | None = None) -> None:
        DeviceBinding.objects.filter(pk=self.pk).update(
            last_seen_at=timezone.now(),
            ip_last_seen=ip or self.ip_last_seen,
            use_count=models.F('use_count') + 1,
        )

    def revoke(self, reason: str = '') -> None:
        if self.revoked_at:
            return
        self.revoked_at = timezone.now()
        self.revoked_reason = (reason or '')[:200]
        self.save(update_fields=['revoked_at', 'revoked_reason', 'updated_at'])


def _make_otp_code() -> str:
    """6-digit numeric code, leading zeros preserved."""
    return f'{secrets.randbelow(1_000_000):06d}'


class DeviceOTP(TimeStampedModel):
    """
    Email-delivered one-time code for new-device verification.

    Security notes:
      * Codes are 6 digits — easy for humans, but we cap attempts and expire
        them in 10 minutes.
      * We store the SHA-256 of the code, not the code itself. The plaintext
        only exists in the email (and in process memory for the few ms it
        takes to send).
      * Stale codes (issued but not yet consumed) are invalidated when a new
        one is issued — prevents a hoarded inbox of valid OTPs.
    """
    PURPOSE_CHOICES = (
        ('device_verify',  'Device verification'),
        ('device_rebind',  'Switch to a new device'),
    )

    OTP_TTL = timedelta(minutes=10)
    MAX_ATTEMPTS = 5

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    token = models.ForeignKey(
        PortalAccessToken,
        on_delete=models.CASCADE,
        related_name='device_otps',
    )
    fingerprint_hash = models.CharField(max_length=64, db_index=True)

    code_hash = models.CharField(max_length=64)
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES, default='device_verify')

    issued_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    invalidated_at = models.DateTimeField(null=True, blank=True)

    attempts = models.PositiveSmallIntegerField(default=0)
    delivered_to_email = models.EmailField()
    requested_ip = models.GenericIPAddressField(null=True, blank=True)
    requested_user_agent = models.CharField(max_length=400, blank=True)

    class Meta:
        ordering = ['-issued_at']
        indexes = [
            models.Index(fields=['token', 'fingerprint_hash', 'consumed_at']),
        ]

    def __str__(self):
        return f'OTP for token {self.token_id} ({self.purpose})'

    @classmethod
    def hash_code(cls, code: str) -> str:
        return hashlib.sha256((code or '').strip().encode('utf-8')).hexdigest()

    @property
    def is_consumable(self) -> bool:
        return (
            self.consumed_at is None
            and self.invalidated_at is None
            and timezone.now() <= self.expires_at
            and self.attempts < self.MAX_ATTEMPTS
        )

    def mark_attempt(self) -> None:
        DeviceOTP.objects.filter(pk=self.pk).update(
            attempts=models.F('attempts') + 1,
        )
        self.refresh_from_db(fields=['attempts'])

    def consume(self) -> None:
        self.consumed_at = timezone.now()
        self.save(update_fields=['consumed_at', 'updated_at'])

    def invalidate(self, reason: str = '') -> None:
        if self.invalidated_at or self.consumed_at:
            return
        self.invalidated_at = timezone.now()
        self.save(update_fields=['invalidated_at', 'updated_at'])


# ─────────────────────────────────────────────────────────────────────────────
# Maintenance schedule (only for maintenance contracts)
# ─────────────────────────────────────────────────────────────────────────────

class MaintenanceSchedule(TimeStampedModel):
    CADENCE_CHOICES = (
        ('weekly',     'Weekly'),
        ('biweekly',   'Bi-weekly'),
        ('monthly',    'Monthly'),
        ('quarterly',  'Quarterly'),
        ('semi_annual','Semi-annual'),
        ('annual',     'Annual'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contract = models.OneToOneField(
        CONTRACT_MODEL,
        on_delete=models.CASCADE,
        related_name='pm_schedule',
    )

    cadence = models.CharField(
        max_length=20, choices=CADENCE_CHOICES, default='quarterly',
    )
    has_preventive = models.BooleanField(default=True)
    has_on_call = models.BooleanField(default=False)

    next_due_date = models.DateField(null=True, blank=True)
    last_completed_date = models.DateField(null=True, blank=True)

    notes = models.TextField(blank=True)

    def __str__(self):
        return f'PM schedule for contract {self.contract_id} ({self.cadence})'

    def advance(self) -> None:
        from dateutil.relativedelta import relativedelta
        if not self.next_due_date:
            return
        offsets = {
            'weekly':       timedelta(weeks=1),
            'biweekly':     timedelta(weeks=2),
            'monthly':      relativedelta(months=1),
            'quarterly':    relativedelta(months=3),
            'semi_annual':  relativedelta(months=6),
            'annual':       relativedelta(years=1),
        }
        offset = offsets.get(self.cadence, relativedelta(months=3))
        self.next_due_date = self.next_due_date + offset
        self.save(update_fields=['next_due_date', 'updated_at'])


class MaintenanceVisit(TimeStampedModel):
    KIND_CHOICES = (
        ('preventive',  'Preventive'),
        ('on_call',     'On-call / Reactive'),
    )

    STATUS_CHOICES = (
        ('scheduled',   'Scheduled'),
        ('dispatched',  'Technician Dispatched'),
        ('on_site',     'On Site'),
        ('completed',   'Completed'),
        ('verified',    'Verified by CS'),
        ('cancelled',   'Cancelled'),
        ('disputed',    'Disputed'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contract = models.ForeignKey(
        CONTRACT_MODEL,
        on_delete=models.CASCADE,
        related_name='maintenance_visits',
    )
    schedule = models.ForeignKey(
        MaintenanceSchedule,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='visits',
    )

    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default='preventive')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='scheduled')

    scheduled_for = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    technician = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='maintenance_visits',
    )

    task = models.OneToOneField(
        TASK_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='maintenance_visit',
    )

    ticket = models.ForeignKey(
        TICKET_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='maintenance_visits',
    )

    summary = models.CharField(max_length=220, blank=True)
    customer_visible_notes = models.TextField(blank=True)
    internal_notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-scheduled_for', '-created_at']
        indexes = [
            models.Index(fields=['contract', 'status']),
            models.Index(fields=['scheduled_for']),
        ]

    def __str__(self):
        return f'Visit {self.id} — {self.get_kind_display()} ({self.status})'


# ─────────────────────────────────────────────────────────────────────────────
# On-site events
# ─────────────────────────────────────────────────────────────────────────────

class OnSiteEvent(TimeStampedModel):
    EVENT_TYPES = (
        ('arrive',  'Arrived on site'),
        ('depart',  'Left site'),
        ('dispute', 'Customer disputed visit'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(
        TASK_MODEL,
        on_delete=models.CASCADE,
        related_name='on_site_events',
    )
    assignment = models.ForeignKey(
        ASSIGNMENT_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='on_site_events',
    )
    contract = models.ForeignKey(
        CONTRACT_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='on_site_events',
    )

    event_type = models.CharField(max_length=20, choices=EVENT_TYPES, default='arrive')

    actor_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='on_site_events_recorded',
    )
    actor_contact = models.ForeignKey(
        ContractContact,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='on_site_events_disputed',
    )

    gps_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
    )
    gps_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
    )
    note = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['task', 'event_type']),
            models.Index(fields=['contract', 'event_type']),
        ]

    def __str__(self):
        return f'{self.get_event_type_display()} for task {self.task_id}'


# ─────────────────────────────────────────────────────────────────────────────
# Customer-submitted support requests (now with full lifecycle)
# ─────────────────────────────────────────────────────────────────────────────

class PortalSupportRequest(TimeStampedModel):
    """
    Lifecycle (portal-side, distinct from the staff-side ServiceTicket so a
    customer cancellation doesn't disturb staff workflow without a signal):

        pending_review
          ↓ (CS picks it up — usually instant on creation)
        open
          ↓                      ↘
        resolved          cancelled (by customer, before resolution)
          ↓ (customer rates / requests reopen / falls into auto-close)
        closed
          ↓ (cannot be reopened from the portal — must contact CS)

    Disputes / reopens are tracked separately so we can show "this ticket
    was reopened twice before final close" in support history.
    """
    REQUEST_KINDS = (
        ('support',     'General Support'),
        ('on_call',     'On-call Maintenance'),
        ('followup',    'Follow-up on Previous Issue'),
        ('dispute',     'Dispute / Complaint'),
        ('question',    'Question about Service'),
    )

    PRIORITIES = (
        ('low',      'Low'),
        ('medium',   'Medium'),
        ('high',     'High'),
        ('critical', 'Critical / Urgent'),
    )

    STATUS_CHOICES = (
        ('pending_review', 'Pending Review'),
        ('open',           'Open'),
        ('in_progress',    'In Progress'),
        ('on_hold',        'On Hold'),
        ('resolved',       'Resolved — awaiting your confirmation'),
        ('closed',         'Closed'),
        ('cancelled',      'Cancelled by customer'),
        ('reopened',       'Reopened'),
    )

    OPEN_STATUSES = ('pending_review', 'open', 'in_progress', 'on_hold', 'reopened')
    RESOLVED_STATUSES = ('resolved',)
    TERMINAL_STATUSES = ('closed', 'cancelled')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contract = models.ForeignKey(
        CONTRACT_MODEL,
        on_delete=models.CASCADE,
        related_name='portal_requests',
    )
    contact = models.ForeignKey(
        ContractContact,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='requests',
    )
    token_used = models.ForeignKey(
        PortalAccessToken,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='requests_submitted',
    )

    kind = models.CharField(max_length=20, choices=REQUEST_KINDS, default='support')
    priority = models.CharField(max_length=20, choices=PRIORITIES, default='medium')
    subject = models.CharField(max_length=220)
    description = models.TextField()

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='pending_review',
        db_index=True,
    )

    # Lifecycle stamps
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(blank=True)

    # Reopen counters — kept here for fast filters even though there's a
    # ReopenRequest table for full audit trail.
    reopen_count = models.PositiveSmallIntegerField(default=0)
    last_reopened_at = models.DateTimeField(null=True, blank=True)

    # Mirror of the assigned agent / technician at time of last sync —
    # so the customer doesn't have to wait for a join across apps and so
    # we have a sensible string even if the staff user is later deactivated.
    assigned_agent_name = models.CharField(max_length=180, blank=True)
    assigned_technician_name = models.CharField(max_length=180, blank=True)

    service_ticket = models.ForeignKey(
        TICKET_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='portal_request_origin',
    )

    submitted_from_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['contract', 'status']),
            models.Index(fields=['contract', 'created_at']),
        ]

    def __str__(self):
        return f'PortalRequest "{self.subject}" ({self.status})'

    # ── State helpers ────────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES

    @property
    def is_resolved(self) -> bool:
        return self.status in self.RESOLVED_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in self.TERMINAL_STATUSES

    @property
    def can_cancel(self) -> bool:
        """Customer can cancel anything not yet resolved or terminal."""
        return self.status in self.OPEN_STATUSES

    @property
    def can_comment(self) -> bool:
        """Customer can chime in until the ticket is closed for good."""
        return self.status not in ('closed', 'cancelled')

    @property
    def can_request_reopen(self) -> bool:
        """Reopen is only meaningful AFTER resolution."""
        return self.status in self.RESOLVED_STATUSES + ('closed',)

    @property
    def can_rate(self) -> bool:
        """Rate once the work is done. Allow rating up to closed."""
        if self.status not in self.RESOLVED_STATUSES + ('closed',):
            return False
        return not hasattr(self, 'rating')

    @property
    def display_reference(self) -> str:
        """Short human handle shown to the customer."""
        if self.service_ticket and getattr(self.service_ticket, 'ticket_no', None):
            return self.service_ticket.ticket_no
        return f'PR-{str(self.id)[:8].upper()}'


# ─────────────────────────────────────────────────────────────────────────────
# Comments
# ─────────────────────────────────────────────────────────────────────────────

class TicketComment(TimeStampedModel):
    """
    A message on a portal request. Either side can post — `author_kind`
    distinguishes them. Internal notes by staff are NOT mirrored here; only
    customer-visible exchange.
    """
    AUTHOR_CHOICES = (
        ('customer', 'Customer'),
        ('staff',    'Support Agent'),
        ('system',   'System'),  # auto-posts on status changes etc.
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        PortalSupportRequest,
        on_delete=models.CASCADE,
        related_name='comments',
    )

    author_kind = models.CharField(max_length=12, choices=AUTHOR_CHOICES, default='customer')
    author_contact = models.ForeignKey(
        ContractContact,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='ticket_comments',
    )
    author_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='portal_ticket_comments',
    )
    author_display_name = models.CharField(max_length=180, blank=True)

    body = models.TextField()

    # System events get a structured tag so the UI can style them
    # ("✓ Ticket resolved", "↺ Reopen requested"). Not free text.
    event_tag = models.CharField(max_length=40, blank=True)

    submitted_from_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['request', 'created_at']),
        ]

    def __str__(self):
        return f'Comment on {self.request_id} by {self.author_kind}'

    @property
    def display_name(self) -> str:
        if self.author_display_name:
            return self.author_display_name
        if self.author_kind == 'customer' and self.author_contact:
            return self.author_contact.full_name
        if self.author_kind == 'staff' and self.author_user:
            return getattr(self.author_user, 'full_name', None) or self.author_user.get_username()
        if self.author_kind == 'system':
            return 'System'
        return 'Unknown'


# ─────────────────────────────────────────────────────────────────────────────
# Attachments
# ─────────────────────────────────────────────────────────────────────────────

def _attachment_path(instance, filename):
    """
    File layout:
        portal/tickets/<request-uuid>/<random>-<safe-filename>
    The random prefix prevents simple URL guessing; the request-uuid
    grouping means we can recursively delete files when a request is
    purged.
    """
    base = os.path.basename(filename)[:120]
    rand = secrets.token_hex(8)
    return f'portal/tickets/{instance.request_id}/{rand}-{base}'


class TicketAttachment(TimeStampedModel):
    """
    A file or photo attached to a portal request — either by the customer
    on the portal side, or by staff (mirrored from the ticket detail view
    if you wire that up).

    Validation: keep it permissive on type but strict on size. The view
    enforces the size limit and a basic content-sniff blacklist (no .exe /
    .bat / etc.).
    """
    UPLOADER_CHOICES = (
        ('customer', 'Customer'),
        ('staff',    'Support Agent'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        PortalSupportRequest,
        on_delete=models.CASCADE,
        related_name='attachments',
    )
    comment = models.ForeignKey(
        TicketComment,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='attachments',
        help_text='If the file was attached to a specific comment.',
    )

    file = models.FileField(upload_to=_attachment_path)
    original_name = models.CharField(max_length=200)
    content_type = models.CharField(max_length=100, blank=True)
    size_bytes = models.PositiveBigIntegerField(default=0)

    uploader_kind = models.CharField(
        max_length=12, choices=UPLOADER_CHOICES, default='customer',
    )
    uploader_contact = models.ForeignKey(
        ContractContact,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='ticket_attachments',
    )
    uploader_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='portal_ticket_attachments',
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['request', 'created_at']),
        ]

    def __str__(self):
        return f'{self.original_name} ({self.size_bytes} B)'

    @property
    def is_image(self) -> bool:
        return (self.content_type or '').lower().startswith('image/')

    @property
    def size_human(self) -> str:
        size = self.size_bytes
        for unit in ('B', 'KB', 'MB', 'GB'):
            if size < 1024 or unit == 'GB':
                return f'{size:.0f} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
            size /= 1024
        return f'{self.size_bytes} B'


# ─────────────────────────────────────────────────────────────────────────────
# Ratings
# ─────────────────────────────────────────────────────────────────────────────

class TicketRating(TimeStampedModel):
    """
    1–5 star CSAT plus optional written feedback. One rating per request
    (enforced by OneToOneField). If you ever want to allow re-rating after
    a reopen, store ratings on a separate "resolution episode" object.
    """
    SCORE_CHOICES = [(i, f'{i} star{"" if i == 1 else "s"}') for i in range(1, 6)]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.OneToOneField(
        PortalSupportRequest,
        on_delete=models.CASCADE,
        related_name='rating',
    )
    rated_by_contact = models.ForeignKey(
        ContractContact,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='ratings',
    )

    score = models.PositiveSmallIntegerField(
        choices=SCORE_CHOICES,
        validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    feedback = models.TextField(blank=True)
    would_recommend = models.BooleanField(null=True, blank=True)

    submitted_from_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['score']),
        ]

    def __str__(self):
        return f'{self.score}★ for {self.request_id}'


# ─────────────────────────────────────────────────────────────────────────────
# Reopen requests
# ─────────────────────────────────────────────────────────────────────────────

class ReopenRequest(TimeStampedModel):
    """
    The customer says "this isn't actually fixed". We don't auto-reopen —
    a staff agent reviews it. Tracked here so we have an audit trail of
    "customer pushed back twice, agent reopened on third try".
    """
    DECISION_CHOICES = (
        ('pending',  'Pending review'),
        ('approved', 'Approved — ticket reopened'),
        ('declined', 'Declined — ticket remains closed'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(
        PortalSupportRequest,
        on_delete=models.CASCADE,
        related_name='reopen_requests',
    )
    requested_by_contact = models.ForeignKey(
        ContractContact,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='reopen_requests',
    )

    reason = models.TextField()
    decision = models.CharField(
        max_length=12, choices=DECISION_CHOICES, default='pending', db_index=True,
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='portal_reopen_decisions',
    )
    decision_note = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['request', 'decision']),
        ]

    def __str__(self):
        return f'Reopen request for {self.request_id} ({self.decision})'