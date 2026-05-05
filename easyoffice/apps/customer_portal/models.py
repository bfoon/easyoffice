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
"""
from __future__ import annotations

import uuid
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


# ─────────────────────────────────────────────────────────────────────────────
# Configurable model labels
# ─────────────────────────────────────────────────────────────────────────────
#
# We don't know whether your Contract model lives in apps.contracts,
# apps.finance, or somewhere else, and we shouldn't import it directly here
# (avoids circular imports + lets you point this at the right app via
# settings without editing code). All FKs use string refs.

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

    Note: we DON'T blindly upsert a contact on every contract save — the
    signal in signals.py is careful to (a) keep the contract's primary
    counterparty as one ContractContact, (b) not duplicate by email.
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

    # Convenience: the live token for this contact (if any)
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
    """
    A token has to:
      * be URL-safe (we put it in /portal/<token>/),
      * have at least ~128 bits of entropy,
      * not be guessable from any other field.

    secrets.token_urlsafe(48) gives ~64 chars of url-safe base64 with 384
    bits of entropy. Overkill, but cheap.
    """
    return secrets.token_urlsafe(48)


class PortalAccessToken(TimeStampedModel):
    """
    A token grants ONE contact ONE-link access to the portal for the
    duration of ONE contract period.

    Lifecycle:
      * Issued when a contract is saved (signal in signals.py).
      * `valid_until` matches contract.end_date (with a small buffer so a
        same-day expiry doesn't lock the customer out at noon).
      * On contract renewal, the old token is REVOKED — not deleted — and a
        new one is issued. This preserves audit history.
      * If the customer clicks an old/revoked link, the portal view tells
        them so plainly and offers a "request a new link" action that emails
        the head of customer service. (Implemented in views.py.)
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

    # Diagnostics — we want to know how the customer is using the portal
    issued_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    use_count = models.PositiveIntegerField(default=0)

    # Revocation: not deleted, so we can answer "what happened?" later.
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
        """Record portal hit. Cheap — no concurrency safeguard, just a counter."""
        PortalAccessToken.objects.filter(pk=self.pk).update(
            last_used_at=timezone.now(),
            use_count=models.F('use_count') + 1,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Maintenance schedule (only for maintenance contracts)
# ─────────────────────────────────────────────────────────────────────────────

class MaintenanceSchedule(TimeStampedModel):
    """
    Created automatically when a maintenance contract is saved IF it has
    preventive maintenance. The signal looks at:

        contract.contract_type == 'maintenance'

    and the optional contract.maintenance_features field (JSON or text)
    containing 'preventive' or 'on_call'. If your Contract model doesn't
    have a maintenance_features field, the signal degrades gracefully:
    every maintenance contract gets a quarterly PM schedule as a default,
    which is the most common cadence for the GMD market.
    """
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
        """Push next_due_date forward by one cadence period."""
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
    """
    A planned or completed maintenance visit. Created either by:
      (a) the recurring PM cron when next_due_date is reached, or
      (b) the customer requesting on-call support via the portal.

    Each visit links to a Task — that's where the technician actually works,
    taps "on site", logs time, and ultimately marks "done". The portal
    timeline reads from MaintenanceVisit + OnSiteEvent.
    """
    KIND_CHOICES = (
        ('preventive',  'Preventive'),
        ('on_call',     'On-call / Reactive'),
    )

    STATUS_CHOICES = (
        ('scheduled',   'Scheduled'),
        ('dispatched',  'Technician Dispatched'),
        ('on_site',     'On Site'),
        ('completed',   'Completed'),
        ('verified',    'Verified by CS'),   # CS confirmed with customer
        ('cancelled',   'Cancelled'),
        ('disputed',    'Disputed'),         # customer says tech didn't show
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

    # The Task the technician actually works against.
    task = models.OneToOneField(
        TASK_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='maintenance_visit',
    )

    # Optional link to a service ticket (when the visit came from a
    # customer-initiated support request).
    ticket = models.ForeignKey(
        TICKET_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='maintenance_visits',
    )

    summary = models.CharField(max_length=220, blank=True)
    customer_visible_notes = models.TextField(
        blank=True,
        help_text='What the customer sees in the portal. Keep it human.',
    )
    internal_notes = models.TextField(
        blank=True,
        help_text='Hidden from the customer.',
    )

    class Meta:
        ordering = ['-scheduled_for', '-created_at']
        indexes = [
            models.Index(fields=['contract', 'status']),
            models.Index(fields=['scheduled_for']),
        ]

    def __str__(self):
        return f'Visit {self.id} — {self.get_kind_display()} ({self.status})'


# ─────────────────────────────────────────────────────────────────────────────
# On-site events (for both portal display + audit)
# ─────────────────────────────────────────────────────────────────────────────

class OnSiteEvent(TimeStampedModel):
    """
    Created every time a technician taps "I'm on site" or "I'm leaving site"
    on a Task. We deliberately keep ARRIVE and DEPART as separate rows
    rather than start/end columns on one row, because:

      * Some sites get multiple visits in one day.
      * If the tech forgets to tap depart, we still have a clean arrive
        record (and a flag we can set after a timeout).

    Notifications are NOT sent here — they're sent by the view that creates
    the event, after the DB write commits. Keep model code free of side
    effects.
    """
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
        help_text='Mirrored from task.maintenance_visit / linked ticket assignment.',
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
        help_text='The technician (for arrive/depart) or null for customer disputes.',
    )
    actor_contact = models.ForeignKey(
        ContractContact,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='on_site_events_disputed',
        help_text='The customer-side contact who logged a dispute.',
    )

    # Optional GPS — the front-end MAY supply navigator.geolocation. If it
    # doesn't, that's fine; we still log the event.
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
# Customer-submitted support requests
# ─────────────────────────────────────────────────────────────────────────────

class PortalSupportRequest(TimeStampedModel):
    """
    What a customer submits via /portal/<token>/new-request/.

    We don't reuse customer_service.ServiceTicket directly here for two
    reasons:

      1. We need to capture portal-only fields (originating contact, token
         used, dispute flags) without polluting ServiceTicket.
      2. The portal sometimes accepts requests that we want a CS agent to
         vet before they become an actual ticket — e.g. the customer says
         "is this covered?" — so we need a "pending review" stage that
         doesn't yet exist on the ticket.

    On submission, an actual ServiceTicket IS created (status='new') and
    linked back via `service_ticket`. The CS team works on that ticket
    normally; the portal just shows the customer their view.
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

    # Set once the CS team has spun up an actual ticket from this request.
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
            models.Index(fields=['contract', 'created_at']),
        ]

    def __str__(self):
        return f'PortalRequest "{self.subject}" ({self.kind})'
