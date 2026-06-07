"""
apps/customer_portal/models_onsite_report.py
=============================================

NEW MODULE — on-site visit reporting + monthly customer report rollup.

Drop this file into apps/customer_portal/ and import its models from
apps/customer_portal/models.py so Django picks them up:

    # at the bottom of apps/customer_portal/models.py
    from apps.customer_portal.models_onsite_report import (   # noqa: F401,E402
        OnSiteVisitReport, VisitActivity, MonthlyServiceReport,
    )

Then:  python manage.py makemigrations customer_portal && migrate

What this owns
--------------
  * OnSiteVisitReport  — one technician visit. Linked to EITHER a
      service-ticket assignment (customer opened a ticket) OR a contract
      (routine/preventive maintenance). The logged-in technician opens
      their per-visit link and logs what they did.

  * VisitActivity      — one row in the visit's activity log. Mirrors the
      "Detailed Activity Log" table in the monthly maintenance report:
      date, device/asset, task, serial/user, status.

  * MonthlyServiceReport — the month-end rollup. Aggregates every visit's
      activities for a contract (or a whole customer) into a single PDF,
      which the CEO signs via the EXISTING ContractSignatureRequest flow,
      after which it is emailed to the customer's contract contacts.

Design notes
------------
  * Access is staff-auth only (logged-in technician / supervisor / admin).
    No public token link — the visit link is an internal /tasks-style URL.
  * A visit may be opened straight from a Task (the on-site flow in
    apps/tasks/views_on_site.py). We keep a nullable FK to Task so the
    two features dovetail without a hard dependency.
"""
from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import User


# ─────────────────────────────────────────────────────────────────────────────
# On-site visit (one technician site visit)
# ─────────────────────────────────────────────────────────────────────────────

class OnSiteVisitReport(models.Model):
    """
    A single on-site visit by a technician. The visit groups together the
    individual activity rows the technician logs while/after on site.

    Exactly one of (ticket_assignment, contract) is the *origin*:
      * ticket_assignment set  → customer-opened ticket visit (ad-hoc).
      * contract set           → routine/preventive maintenance visit.
    Both may resolve to a contract for the monthly rollup, but `origin`
    records how the visit came to be.
    """

    class Origin(models.TextChoices):
        TICKET  = 'ticket',  _('Customer Ticket')
        ROUTINE = 'routine', _('Routine Maintenance')

    class Status(models.TextChoices):
        DRAFT      = 'draft',      _('Draft')        # tech still logging
        SUBMITTED  = 'submitted',  _('Submitted')    # tech finished
        VERIFIED   = 'verified',   _('Verified')     # CS/supervisor checked
        REPORTED   = 'reported',   _('In Monthly Report')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    origin = models.CharField(
        max_length=12, choices=Origin.choices, default=Origin.ROUTINE,
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DRAFT,
        db_index=True,
    )

    # ── Linkage ──────────────────────────────────────────────────────────
    contract = models.ForeignKey(
        'finance.Contract',
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='onsite_visits',
        help_text='The contract this visit belongs to (routine maintenance, '
                  'or resolved from the ticket).',
    )
    ticket_assignment = models.ForeignKey(
        'customer_service.ServiceTicketAssignment',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='onsite_visits',
        help_text='Set when this visit originated from a customer ticket.',
    )
    task = models.ForeignKey(
        'tasks.Task',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='onsite_visit_reports',
        help_text='The task the technician worked from, if any.',
    )

    # ── Who / when ─────────────────────────────────────────────────────────
    technician = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='onsite_visits_performed',
    )
    visit_date = models.DateField(default=timezone.localdate, db_index=True)
    arrived_at = models.DateTimeField(null=True, blank=True)
    departed_at = models.DateTimeField(null=True, blank=True)

    # Free-text fields that surface on the report
    site_location = models.CharField(max_length=255, blank=True)
    summary = models.TextField(
        blank=True,
        help_text='Short narrative of the visit (optional).',
    )
    gps_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
    )
    gps_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True,
    )

    submitted_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-visit_date', '-created_at']
        indexes = [
            models.Index(fields=['contract', 'visit_date']),
            models.Index(fields=['status', 'visit_date']),
        ]

    def __str__(self):
        who = self.technician.full_name if self.technician else 'Unassigned'
        return f'Visit {self.visit_date} — {who}'

    # ── Convenience ────────────────────────────────────────────────────────

    @property
    def activity_count(self) -> int:
        return self.activities.count()

    @property
    def resolved_contract(self):
        """Contract for this visit, resolving through the ticket if needed."""
        if self.contract_id:
            return self.contract
        if self.ticket_assignment_id:
            ticket = getattr(self.ticket_assignment, 'ticket', None)
            return getattr(ticket, 'contract', None)
        return None

    def submit(self, *, by_user=None):
        """Tech marks the visit complete. Idempotent."""
        if self.status == self.Status.DRAFT:
            self.status = self.Status.SUBMITTED
            self.submitted_at = timezone.now()
            self.save(update_fields=['status', 'submitted_at', 'updated_at'])
            return True
        return False

    def reopen(self):
        """Move back to DRAFT so the tech can add/correct rows."""
        if self.status in (self.Status.SUBMITTED, self.Status.VERIFIED):
            self.status = self.Status.DRAFT
            self.submitted_at = None
            self.save(update_fields=['status', 'submitted_at', 'updated_at'])
            return True
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Activity row (one line in the visit's activity log)
# ─────────────────────────────────────────────────────────────────────────────

class VisitActivity(models.Model):
    """
    One detailed row of work done during a visit. Mirrors the columns of the
    monthly maintenance report's "Detailed Activity Log":

        Date | Device | Issue / Task | Tag / Serial / User | Status
    """

    class Status(models.TextChoices):
        DONE        = 'done',        _('Resolved / Done')
        IN_PROGRESS = 'in_progress', _('In Progress')
        PENDING     = 'pending',     _('Pending / Follow-up')
        ESCALATED   = 'escalated',   _('Escalated')
        INFO        = 'info',        _('Informational')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    visit = models.ForeignKey(
        OnSiteVisitReport,
        on_delete=models.CASCADE,
        related_name='activities',
    )

    position = models.PositiveIntegerField(default=0)

    # The five report columns
    activity_date = models.DateField(default=timezone.localdate)
    device = models.CharField(
        max_length=200, blank=True,
        help_text='Device / asset, e.g. "Lenovo X1", "HP Printer".',
    )
    task_description = models.CharField(
        max_length=500,
        help_text='Issue worked on / task performed.',
    )
    serial_or_user = models.CharField(
        max_length=200, blank=True,
        help_text='Asset tag / serial number / end-user name.',
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DONE,
    )
    notes = models.TextField(blank=True)

    logged_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='visit_activities_logged',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['position', 'activity_date', 'created_at']

    def __str__(self):
        return f'{self.activity_date} — {self.task_description[:40]}'


# ─────────────────────────────────────────────────────────────────────────────
# Monthly rollup report
# ─────────────────────────────────────────────────────────────────────────────

class MonthlyServiceReport(models.Model):
    """
    Month-end aggregation of all visit activities, scoped EITHER to a single
    contract or to an entire customer (all their contracts).

    The generated PDF is signed by the CEO using the existing
    finance.ContractSignatureRequest flow, then emailed to the customer.

    The `signature_request` FK is nullable and points at the SAME signature
    model the contract-signing flow already uses, so we reuse
    stamp_signature_onto_pdf / the public-sign machinery unchanged.
    """

    class Scope(models.TextChoices):
        CONTRACT = 'contract', _('Single Contract')
        CUSTOMER = 'customer', _('Whole Customer')

    class Status(models.TextChoices):
        DRAFT          = 'draft',          _('Draft')
        AWAITING_SIGN  = 'awaiting_sign',  _('Awaiting CEO Signature')
        SIGNED         = 'signed',         _('Signed')
        SENT           = 'sent',           _('Sent to Customer')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    scope = models.CharField(
        max_length=12, choices=Scope.choices, default=Scope.CONTRACT,
    )
    status = models.CharField(
        max_length=14, choices=Status.choices, default=Status.DRAFT,
        db_index=True,
    )

    # One of these is set depending on scope.
    contract = models.ForeignKey(
        'finance.Contract',
        on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='monthly_reports',
    )
    # For CUSTOMER scope we store the customer key the contract uses.
    # finance.Contract identifies the counterparty by name; if you have a
    # dedicated Customer model, swap this for an FK.
    customer_name = models.CharField(max_length=255, blank=True)

    # Reporting period — first and last day of the month being reported.
    period_start = models.DateField()
    period_end = models.DateField()

    title = models.CharField(max_length=300, blank=True)

    # Snapshot of the visits that fed this report (list of visit UUIDs)
    visit_ids = models.JSONField(default=list, blank=True)

    # Aggregate counters captured at generation time (for the exec summary)
    total_activities = models.PositiveIntegerField(default=0)
    total_visits = models.PositiveIntegerField(default=0)

    pdf_file = models.FileField(
        upload_to='monthly_service_reports/%Y/%m/',
        null=True, blank=True,
    )
    pdf_hash = models.CharField(max_length=64, blank=True)

    signature_request = models.ForeignKey(
        'finance.ContractSignatureRequest',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='monthly_service_reports',
    )

    generated_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='monthly_reports_generated',
    )
    sent_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-period_start', '-created_at']
        indexes = [
            models.Index(fields=['contract', 'period_start']),
            models.Index(fields=['status', 'period_start']),
        ]

    def __str__(self):
        label = self.contract.title if self.contract_id else self.customer_name
        return f'Monthly Report {self.period_start:%b %Y} — {label}'

    @property
    def period_label(self) -> str:
        return f'{self.period_start:%B %Y}'
