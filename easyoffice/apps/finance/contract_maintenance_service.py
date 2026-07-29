"""
apps/finance/contract_maintenance_service.py
============================================

Maintenance-roster automation for contracts. Mirrors the structure of
contract_invoice_service.py so the ops model stays familiar:

* schedule_visit_for_roster()   – materialize ONE visit (manual or scheduled):
      - creates the MaintenanceVisit
      - creates ONE shared Task, assignee = lead member, everyone else added
        as collaborators, and notifies them
      - emails every responsible party a calendar invite (.ics REQUEST)
        containing the public completion link

* run_scheduled_maintenance()   – called by Celery Beat / cron daily. Walks
      every active roster whose next_visit_date falls within its notice
      window, schedules the visit, and advances next_visit_date.

* complete_visit()              – called by the public completion view.
      Records the report, marks the visit completed, closes the Task, and
      notifies the finance/contract managers.

* cancel_visit()                – cancels a visit and emails an .ics CANCEL
      so the event disappears from attendees' calendars.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Optional

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.mail import EmailMultiAlternatives, send_mail
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


class MaintenanceError(Exception):
    """Raised when a visit cannot be scheduled or completed."""


# ── Frequency math (same idiom as contract_invoice_service._advance_date) ────

def _advance_date(start: date, frequency: str) -> Optional[date]:
    if not start:
        return None
    freq = (frequency or '').lower()
    if freq == 'weekly':
        return start + timedelta(weeks=1)
    if freq == 'biweekly':
        return start + timedelta(weeks=2)
    if freq == 'monthly':
        return start + relativedelta(months=1)
    if freq == 'quarterly':
        return start + relativedelta(months=3)
    if freq == 'semi_annual':
        return start + relativedelta(months=6)
    if freq == 'annual':
        return start + relativedelta(years=1)
    return None


# ── URL / branding helpers ───────────────────────────────────────────────────

def _absolute_url(path: str, request=None) -> str:
    """Build an absolute URL for emails. Works from views and Celery alike."""
    if request is not None:
        return request.build_absolute_uri(path)
    base = getattr(settings, 'SITE_URL', '') or getattr(settings, 'BASE_URL', '')
    return f"{base.rstrip('/')}{path}" if base else path


def _org_name() -> str:
    return getattr(settings, 'ORG_NAME', None) or getattr(settings, 'SITE_NAME', 'Finance System')


def _from_email() -> str:
    return getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@example.com')


# ── ICS (RFC 5545) generation ────────────────────────────────────────────────

def _ics_escape(text: str) -> str:
    return (
        (text or '')
        .replace('\\', '\\\\')
        .replace(';', '\\;')
        .replace(',', '\\,')
        .replace('\r\n', '\\n')
        .replace('\n', '\\n')
    )


def _ics_fold(line: str) -> str:
    """Fold lines longer than 75 octets per RFC 5545 §3.1."""
    out, raw = [], line.encode('utf-8')
    while len(raw) > 75:
        cut = 75
        # don't split inside a multi-byte char
        while cut > 0 and (raw[cut] & 0xC0) == 0x80:
            cut -= 1
        out.append(raw[:cut].decode('utf-8'))
        raw = b' ' + raw[cut:]
    out.append(raw.decode('utf-8'))
    return '\r\n'.join(out)


def _ics_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def build_visit_ics(visit, *, method: str = 'REQUEST') -> str:
    """
    Build a calendar invite for a visit. METHOD:REQUEST makes mail clients
    render Accept/Decline buttons; METHOD:CANCEL (with the same UID and a
    bumped SEQUENCE) removes the event again.
    """
    roster = visit.roster
    tz = timezone.get_current_timezone()
    start_time = roster.visit_time or time(9, 0)
    dt_start = timezone.make_aware(datetime.combine(visit.scheduled_date, start_time), tz)
    dt_end = dt_start + timedelta(minutes=roster.duration_minutes or 60)

    complete_url = _absolute_url(visit.public_path())
    description = (
        f"{roster.description or 'Routine maintenance visit.'}\n\n"
        f"Contract: {roster.contract.title}\n"
        f"When done, submit the completion form: {complete_url}"
    )

    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        f'PRODID:-//{_ics_escape(_org_name())}//Maintenance Roster//EN',
        f'METHOD:{method}',
        'BEGIN:VEVENT',
        f'UID:{visit.ics_uid}',
        f'SEQUENCE:{visit.ics_sequence}',
        f'DTSTAMP:{_ics_dt(timezone.now())}',
        f'DTSTART:{_ics_dt(dt_start)}',
        f'DTEND:{_ics_dt(dt_end)}',
        f'SUMMARY:{_ics_escape(f"{roster.title} — {roster.contract.title}")}',
        f'DESCRIPTION:{_ics_escape(description)}',
        f'ORGANIZER;CN={_ics_escape(_org_name())}:mailto:{_from_email()}',
        'STATUS:' + ('CANCELLED' if method == 'CANCEL' else 'CONFIRMED'),
    ]
    if roster.location:
        lines.append(f'LOCATION:{_ics_escape(roster.location)}')
    for m in roster.members.all():
        if m.email:
            lines.append(
                f'ATTENDEE;CN={_ics_escape(m.display_name)};ROLE=REQ-PARTICIPANT;'
                f'PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{m.email}'
            )
    lines += [
        'BEGIN:VALARM',
        'TRIGGER:-P1D',
        'ACTION:DISPLAY',
        f'DESCRIPTION:{_ics_escape(roster.title)} tomorrow',
        'END:VALARM',
        'END:VEVENT',
        'END:VCALENDAR',
    ]
    return '\r\n'.join(_ics_fold(l) for l in lines) + '\r\n'


# ── The shared Task ─────────────────────────────────────────────────────────

def _attach_collaborators(task, users) -> Optional[str]:
    """
    Add users to the task's collaborator relation, whatever it is called in
    apps.tasks (collaborators / watchers / participants / assignees).
    Returns the field name used, or None if no M2M was found.
    """
    for field in ('collaborators', 'watchers', 'participants', 'assignees'):
        rel = getattr(task, field, None)
        if rel is not None and hasattr(rel, 'add'):
            try:
                rel.add(*users)
                return field
            except Exception:
                logger.exception('Could not add collaborators via Task.%s', field)
    return None


def _create_visit_task(visit, actor=None):
    """Create ONE Task for the visit; lead = assignee, others = collaborators."""
    from apps.tasks.models import Task

    roster = visit.roster
    members = list(roster.members.select_related('user'))
    internal = [m.user for m in members if m.user_id]
    lead = next((m.user for m in members if m.is_lead and m.user_id),
                internal[0] if internal else None)

    complete_url = _absolute_url(visit.public_path())
    description = (
        f"Routine maintenance visit for contract \"{roster.contract.title}\".\n\n"
        f"{roster.description or ''}\n\n"
        f"Scheduled: {visit.scheduled_date.strftime('%A, %B %-d, %Y')}\n"
        f"Location: {roster.location or '—'}\n"
        f"Responsible: {', '.join(m.display_name for m in members) or '—'}\n\n"
        f"Completion form (share with whoever performs the visit):\n{complete_url}"
    )

    kwargs = dict(
        title=f'{roster.title} — {visit.scheduled_date.strftime("%b %-d, %Y")}',
        description=description,
        due_date=visit.scheduled_date,
        status='todo',
    )
    if lead is not None:
        kwargs['assigned_to'] = lead
    # Optional metadata fields, applied only if the Task model has them
    for opt_field, opt_value in (('created_by', actor), ('priority', 'medium')):
        if opt_value is not None and hasattr(Task, opt_field):
            kwargs[opt_field] = opt_value

    task = Task.objects.create(**kwargs)

    collaborators = [u for u in internal if u != lead]
    if collaborators:
        used = _attach_collaborators(task, collaborators)
        if used is None:
            logger.warning(
                'Task model has no collaborator M2M; %s users noted only in '
                'the description for visit %s', len(collaborators), visit.pk,
            )
    return task


# ── Notifications ───────────────────────────────────────────────────────────

def send_visit_invites(visit, request=None) -> dict:
    """
    Email every roster member: calendar invite (.ics) + completion link.
    Returns {'sent': n, 'errors': [(email, err), ...]}.
    """
    roster = visit.roster
    contract = roster.contract
    complete_url = _absolute_url(visit.public_path(), request)
    ics = build_visit_ics(visit, method='REQUEST')
    org = _org_name()

    sent, errors = 0, []
    for m in roster.members.all():
        email = m.email
        if not email:
            errors.append((m.display_name, 'no email address'))
            continue

        subject = f'Maintenance visit: {roster.title} — {visit.scheduled_date.strftime("%b %-d, %Y")} | {org}'
        body = (
            f'Hello {m.display_name},\n\n'
            f'You are a responsible party for the routine maintenance below. '
            f'A calendar invite is attached — please accept it.\n\n'
            f'  Routine:   {roster.title}\n'
            f'  Contract:  {contract.title}\n'
            f'  Date:      {visit.scheduled_date.strftime("%A, %B %-d, %Y")}'
            f'{" at " + roster.visit_time.strftime("%H:%M") if roster.visit_time else ""}\n'
            f'  Location:  {roster.location or "—"}\n\n'
            f'{roster.description or ""}\n\n'
            f'When the visit/exercise is complete, fill in the completion form:\n'
            f'{complete_url}\n\n'
            f'— {org}'
        )
        try:
            msg = EmailMultiAlternatives(subject, body, _from_email(), [email])
            # method=REQUEST in the content type is what makes clients show
            # the Accept / Decline bar instead of a plain attachment.
            msg.attach('invite.ics', ics, 'text/calendar; charset=utf-8; method=REQUEST')
            msg.send(fail_silently=False)
            sent += 1
        except Exception as exc:
            logger.exception('Failed to send maintenance invite to %s', email)
            errors.append((email, str(exc)))

    if sent:
        visit.invites_sent_at = timezone.now()
        visit.save(update_fields=['invites_sent_at', 'updated_at'])
    return {'sent': sent, 'errors': errors}


def _notify_managers_of_completion(visit):
    """Tell Finance/CEO/Admin/HR that a visit report came in (best-effort)."""
    from apps.core.models import User
    from django.db.models import Q

    recipients = list(
        User.objects.filter(is_active=True)
        .filter(Q(groups__name__in=['Finance', 'CEO', 'Admin', 'HR']) | Q(is_superuser=True))
        .exclude(email='')
        .values_list('email', flat=True)
        .distinct()
    )
    if not recipients:
        return
    roster = visit.roster
    outcome = visit.get_outcome_display() or '—'
    send_mail(
        subject=f'Maintenance completed: {roster.title} ({visit.scheduled_date}) — {outcome}',
        message=(
            f'"{roster.title}" for contract "{roster.contract.title}" was marked '
            f'complete by {visit.completed_by_name or "a responsible party"}.\n\n'
            f'Outcome: {outcome}\n'
            f'Work done: {visit.work_done or "—"}\n'
            f'Issues found: {visit.issues_found or "—"}\n'
            f'Follow-up required: {"Yes — " + visit.follow_up_notes if visit.follow_up_required else "No"}\n'
        ),
        from_email=_from_email(),
        recipient_list=recipients,
        fail_silently=True,
    )


# ── Scheduling ──────────────────────────────────────────────────────────────

@transaction.atomic
def schedule_visit_for_roster(roster, *, visit_date: Optional[date] = None,
                              actor=None, request=None):
    """
    Materialize ONE visit for a roster: create the MaintenanceVisit + shared
    Task, then send calendar invites. Idempotent per (roster, date).
    """
    from apps.finance.models import MaintenanceVisit

    visit_date = visit_date or roster.next_visit_date
    if not visit_date:
        raise MaintenanceError('This roster has no next visit date set.')
    if not roster.members.exists():
        raise MaintenanceError('Add at least one responsible party before scheduling visits.')

    visit, created = MaintenanceVisit.objects.get_or_create(
        roster=roster, scheduled_date=visit_date,
    )
    if not created:
        return visit, False

    if roster.create_task:
        try:
            visit.task = _create_visit_task(visit, actor=actor)
            visit.save(update_fields=['task', 'updated_at'])
        except Exception:
            # A missing/incompatible Task field must not block the roster.
            logger.exception('Could not create shared Task for visit %s', visit.pk)

    if roster.send_calendar_invites:
        send_visit_invites(visit, request=request)

    return visit, True


def find_rosters_due(today: Optional[date] = None):
    """Active rosters whose next visit falls inside their notice window."""
    from django.db.models import F, ExpressionWrapper, DateField
    from apps.finance.models import MaintenanceRoster

    today = today or timezone.localdate()
    return (
        MaintenanceRoster.objects
        .filter(active=True, next_visit_date__isnull=False, contract__status='active')
        .annotate(
            trigger=ExpressionWrapper(
                F('next_visit_date') - timedelta(days=1) * F('notice_days_before'),
                output_field=DateField(),
            )
        )
        .filter(trigger__lte=today)
        .select_related('contract')
    )


def run_scheduled_maintenance(today: Optional[date] = None) -> dict:
    """
    Daily entry point (Celery / cron). For each due roster: schedule the
    visit, then advance next_visit_date (stopping past the roster/contract
    end date). Returns a summary dict like run_scheduled_generation().
    """
    today = today or timezone.localdate()
    summary = {'date': str(today), 'attempted': 0, 'succeeded': 0,
               'failed': 0, 'visits': [], 'errors': []}

    for roster in find_rosters_due(today):
        summary['attempted'] += 1
        try:
            visit, created = schedule_visit_for_roster(roster)

            nxt = _advance_date(roster.next_visit_date, roster.frequency)
            end = roster.effective_end_date
            if nxt and end and nxt > end:
                nxt = None
            roster.next_visit_date = nxt
            if nxt is None:
                roster.active = False  # schedule exhausted
            roster.save(update_fields=['next_visit_date', 'active', 'updated_at'])

            summary['succeeded'] += 1
            summary['visits'].append((str(roster.pk), str(visit.scheduled_date)))
        except Exception as exc:
            logger.exception('Maintenance scheduling failed for roster %s', roster.pk)
            summary['failed'] += 1
            summary['errors'].append((str(roster.pk), str(exc)))

    return summary


# ── Completion & cancellation ───────────────────────────────────────────────

@transaction.atomic
def complete_visit(visit, *, name: str, outcome: str, work_done: str = '',
                   issues_found: str = '', follow_up_required: bool = False,
                   follow_up_notes: str = '', user=None):
    """Record the completion report, close the visit, and finish the Task."""
    from apps.finance.models import MaintenanceVisit

    if visit.status == MaintenanceVisit.Status.COMPLETED:
        raise MaintenanceError('This visit has already been marked complete.')
    if visit.status == MaintenanceVisit.Status.CANCELLED:
        raise MaintenanceError('This visit was cancelled.')

    visit.status = MaintenanceVisit.Status.COMPLETED
    visit.completed_at = timezone.now()
    visit.completed_by_user = user if (user and user.is_authenticated) else None
    visit.completed_by_name = (name or '').strip()[:200]
    visit.outcome = outcome
    visit.work_done = (work_done or '').strip()
    visit.issues_found = (issues_found or '').strip()
    visit.follow_up_required = bool(follow_up_required)
    visit.follow_up_notes = (follow_up_notes or '').strip()
    visit.save()

    if visit.task_id:
        try:
            visit.task.status = 'done'
            fields = ['status']
            if hasattr(visit.task, 'completed_at'):
                visit.task.completed_at = timezone.now()
                fields.append('completed_at')
            visit.task.save(update_fields=fields)
        except Exception:
            logger.exception('Could not close Task for visit %s', visit.pk)

    transaction.on_commit(lambda: _notify_managers_of_completion(visit))
    return visit


def cancel_visit(visit, *, actor=None):
    """Cancel a scheduled visit and retract the calendar event."""
    from apps.finance.models import MaintenanceVisit

    if visit.status != MaintenanceVisit.Status.SCHEDULED:
        raise MaintenanceError('Only scheduled visits can be cancelled.')

    visit.status = MaintenanceVisit.Status.CANCELLED
    visit.ics_sequence += 1
    visit.save(update_fields=['status', 'ics_sequence', 'updated_at'])

    if visit.task_id:
        try:
            visit.task.status = 'cancelled'
            visit.task.save(update_fields=['status'])
        except Exception:
            logger.exception('Could not cancel Task for visit %s', visit.pk)

    if visit.invites_sent_at:
        ics = build_visit_ics(visit, method='CANCEL')
        for m in visit.roster.members.all():
            if not m.email:
                continue
            try:
                msg = EmailMultiAlternatives(
                    f'Cancelled: {visit.roster.title} — {visit.scheduled_date}',
                    'This maintenance visit has been cancelled.',
                    _from_email(), [m.email],
                )
                msg.attach('cancel.ics', ics, 'text/calendar; charset=utf-8; method=CANCEL')
                msg.send(fail_silently=True)
            except Exception:
                logger.exception('Failed to send cancellation to %s', m.email)
    return visit
