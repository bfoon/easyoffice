"""Celery tasks for maintenance deadlines and pickup reminders."""
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import MaintenanceReminder
from .maintenance_notifications import send_reminder


@shared_task
def process_maintenance_reminders(batch_size=200):
    now = timezone.now()
    processed = 0
    ids = list(
        MaintenanceReminder.objects.filter(
            is_active=True,
            next_send_at__lte=now,
        ).order_by('next_send_at').values_list('pk', flat=True)[:batch_size]
    )

    for reminder_id in ids:
        with transaction.atomic():
            reminder = (
                MaintenanceReminder.objects.select_for_update()
                .select_related('job', 'job__assigned_to', 'job__owner_user')
                .get(pk=reminder_id)
            )
            if not reminder.is_active or reminder.next_send_at > timezone.now():
                continue
            send_reminder(reminder)
            reminder.send_count += 1
            reminder.last_sent_at = timezone.now()
            if (
                reminder.repeat_every_hours > 0
                and reminder.send_count < reminder.max_sends
                and not reminder.job.is_terminal
            ):
                reminder.next_send_at = reminder.last_sent_at + timedelta(
                    hours=reminder.repeat_every_hours
                )
            else:
                reminder.is_active = False
            reminder.save(update_fields=[
                'send_count', 'last_sent_at', 'next_send_at',
                'is_active', 'updated_at'
            ])
            processed += 1
    return processed
