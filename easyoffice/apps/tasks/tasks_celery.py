"""
apps/tasks/tasks_celery.py
===========================

Celery periodic tasks for the tasks app.

Wire-up — in your project's celery beat schedule (settings.py):

    CELERY_BEAT_SCHEDULE = {
        'tasks-due-reminders': {
            'task': 'apps.tasks.tasks_celery.send_due_reminders',
            'schedule': crontab(minute=0),  # every hour, on the hour
        },
    }

The hourly run is fine even though we want 24h-before and 12h-overdue
cadence — the task itself decides whether each Task is due for a nudge
this run by looking at `last_reminder_at` and `pre_due_reminder_sent`.

Pacing:
  * 24h BEFORE due_date  → one "due tomorrow" warning
  * Once OVERDUE         → a reminder every 12 hours, until status moves
                            to done or cancelled

Recipients of every reminder:
  * the assignee
  * the creator (assigned_by)
  * the assignee's supervisor (if any)
"""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


PRE_DUE_WINDOW = timedelta(hours=24)
OVERDUE_INTERVAL = timedelta(hours=12)
CLOSED_STATUSES = ('done', 'cancelled')


def _recipients_for(task):
    """Return a deduped set of users to notify for this task."""
    recipients = set()
    if task.assigned_to_id:
        recipients.add(task.assigned_to)
    if task.assigned_by_id:
        recipients.add(task.assigned_by)
    profile = getattr(task.assigned_to, 'staffprofile', None)
    if profile and profile.supervisor_id:
        recipients.add(profile.supervisor)
    return recipients


def _send_notification(task, *, kind: str):
    """
    `kind` is 'pre_due' or 'overdue'. Creates one CoreNotification per
    recipient. Best-effort: any single failure is logged, not raised.
    """
    from apps.core.models import CoreNotification

    if kind == 'pre_due':
        title = '⏰ Task due tomorrow'
        body = (
            f'"{task.title}" is due {timezone.localtime(task.due_date).strftime("%a %d %b at %H:%M")}. '
            f'Please wrap it up or update progress.'
        )
    else:  # overdue
        hours_late = int((timezone.now() - task.due_date).total_seconds() // 3600)
        title = '🔴 Overdue task — please update'
        body = (
            f'"{task.title}" is {hours_late}h past its due date. '
            f'Mark it done, request an extension, or update progress now.'
        )

    link = f'/tasks/{task.id}/'

    for user in _recipients_for(task):
        try:
            CoreNotification.objects.create(
                recipient=user,
                sender=None,
                notification_type='task',
                title=title,
                message=body,
                link=link,
            )
        except Exception:
            logger.exception(
                'reminder: could not create notification for user %s on task %s',
                getattr(user, 'id', '?'), task.pk,
            )


@shared_task(name='apps.tasks.tasks_celery.send_due_reminders')
def send_due_reminders():
    """
    Hourly sweep:
      1. Tasks due within the next 24h that haven't had a pre-due ping yet
         → send pre-due reminder.
      2. Tasks already past due_date and not closed, where the last
         reminder was >= 12h ago (or never) → send overdue reminder.
    """
    from apps.tasks.models import Task

    now = timezone.now()
    pre_due_until = now + PRE_DUE_WINDOW
    overdue_cutoff = now - OVERDUE_INTERVAL

    sent_pre_due = 0
    sent_overdue = 0

    # ── 1) Pre-due (24h ahead) ──────────────────────────────────────────
    pre_due_qs = (
        Task.objects
        .filter(
            due_date__gt=now,
            due_date__lte=pre_due_until,
            pre_due_reminder_sent=False,
        )
        .exclude(status__in=CLOSED_STATUSES)
    )
    for task in pre_due_qs.select_related('assigned_to', 'assigned_by'):
        try:
            _send_notification(task, kind='pre_due')
            task.pre_due_reminder_sent = True
            task.last_reminder_at = now
            task.save(update_fields=[
                'pre_due_reminder_sent', 'last_reminder_at', 'updated_at',
            ])
            sent_pre_due += 1
        except Exception:
            logger.exception('pre-due reminder failed for task %s', task.pk)

    # ── 2) Overdue (every 12h) ──────────────────────────────────────────
    from django.db.models import Q

    overdue_qs = (
        Task.objects
        .filter(due_date__lt=now)
        .exclude(status__in=CLOSED_STATUSES)
        .filter(Q(last_reminder_at__isnull=True) | Q(last_reminder_at__lte=overdue_cutoff))
    )
    for task in overdue_qs.select_related('assigned_to', 'assigned_by'):
        try:
            _send_notification(task, kind='overdue')
            task.last_reminder_at = now
            task.save(update_fields=['last_reminder_at', 'updated_at'])
            sent_overdue += 1
        except Exception:
            logger.exception('overdue reminder failed for task %s', task.pk)

    logger.info(
        'tasks.send_due_reminders: pre_due=%d overdue=%d',
        sent_pre_due, sent_overdue,
    )
    return {'pre_due': sent_pre_due, 'overdue': sent_overdue}
