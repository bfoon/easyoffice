"""
Remind HR to complete attendance entry before the payroll day.

Run daily via cron / Celery beat / Windows Task Scheduler, e.g.:

    0 8 * * *  cd /path/to/project && python manage.py remind_attendance_deadline

Behaviour:
- Uses HRSetting.payroll_day (default 25, configurable by CEO / Office Administrator)
  and HRSetting.attendance_reminder_lead_days (default 5).
- From (payroll_day - lead_days) until the payroll day, if attendance for the current
  month is incomplete, every HR user gets an in-app notification and an email telling
  them how many records are missing and that unrecorded days are not paid.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.hr.models import HRSetting
from apps.hr.views import (
    attendance_completeness_for_period,
    notify_hr_attendance_deadline,
)


class Command(BaseCommand):
    help = 'Remind HR to finish attendance entry before the payroll day (default: 25th of the month).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Send the reminder even outside the reminder window.',
        )

    def handle(self, *args, **options):
        setting = HRSetting.get_solo()
        today = timezone.now().date()
        deadline = setting.payroll_deadline_for(today.year, today.month)
        window_start = setting.reminder_window_start_for(today.year, today.month)

        if not options['force'] and not (window_start <= today <= deadline):
            self.stdout.write(
                f'Outside reminder window ({window_start} → {deadline}); nothing sent. '
                'Use --force to send anyway.'
            )
            return

        completeness = attendance_completeness_for_period(today.year, today.month, setting=setting)
        if completeness['is_complete']:
            self.stdout.write('Attendance entry is complete for this month; no reminder needed.')
            return

        notify_hr_attendance_deadline(None, today.year, today.month, completeness, setting=setting)
        self.stdout.write(self.style.SUCCESS(
            f'Reminder sent to HR: {completeness["missing"]} record(s) missing for '
            f'{completeness["staff_missing_count"]} staff member(s); deadline {deadline}.'
        ))
