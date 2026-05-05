"""
apps/customer_portal/management/commands/dispatch_due_pm_visits.py
==================================================================

Run daily via cron to:
  1. Find every MaintenanceSchedule whose next_due_date <= today.
  2. Create a 'scheduled' MaintenanceVisit for each (if not already
     created for this cycle).
  3. Email the contract's contacts that PM is upcoming.
  4. Advance the schedule's next_due_date.

Usage:
    python manage.py dispatch_due_pm_visits
    python manage.py dispatch_due_pm_visits --dry-run
    python manage.py dispatch_due_pm_visits --date 2026-05-01

Cron suggestion (daily at 6:30 am, after the contract invoice run):
    30 6 * * *  /path/to/venv/bin/python manage.py dispatch_due_pm_visits
"""
from __future__ import annotations

import logging
from datetime import datetime, date

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.customer_portal.models import MaintenanceSchedule, MaintenanceVisit, ContractContact

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Create MaintenanceVisit rows for any PM schedules whose next_due_date is reached.'

    def add_arguments(self, parser):
        parser.add_argument('--date', type=str, default=None,
                            help='Override the run date (YYYY-MM-DD).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would happen without writing anything.')

    def handle(self, *args, **opts):
        if opts['date']:
            try:
                run_date = datetime.strptime(opts['date'], '%Y-%m-%d').date()
            except ValueError:
                raise CommandError('--date must be YYYY-MM-DD')
        else:
            run_date = timezone.localdate()

        due_schedules = list(
            MaintenanceSchedule.objects
            .filter(
                has_preventive=True,
                next_due_date__lte=run_date,
            )
            .select_related('contract')
        )

        self.stdout.write(self.style.NOTICE(
            f'Found {len(due_schedules)} schedule(s) due on or before {run_date}.'
        ))

        created = 0
        for schedule in due_schedules:
            existing = MaintenanceVisit.objects.filter(
                schedule=schedule,
                kind='preventive',
                scheduled_for__date=schedule.next_due_date,
            ).exists()
            if existing:
                self.stdout.write(f'  · skip {schedule.contract.title} — visit already exists.')
                continue

            if opts['dry_run']:
                self.stdout.write(
                    f'  + would create PM visit for {schedule.contract.title} '
                    f'on {schedule.next_due_date}'
                )
                continue

            visit = MaintenanceVisit.objects.create(
                contract=schedule.contract,
                schedule=schedule,
                kind='preventive',
                status='scheduled',
                scheduled_for=timezone.make_aware(
                    datetime.combine(schedule.next_due_date, datetime.min.time().replace(hour=9))
                ),
                summary=f'Scheduled {schedule.get_cadence_display()} preventive maintenance',
            )
            created += 1

            # Email contacts (best-effort)
            try:
                from apps.customer_portal import notifications as cp_notifications
                emails = list(
                    ContractContact.objects
                    .filter(contract=schedule.contract, is_active=True, receives_pm_notices=True)
                    .values_list('email', flat=True)
                )
                if emails:
                    cp_notifications.send_dispatch_notice(visit=visit, contact_emails=emails)
            except Exception:
                logger.exception('dispatch_due_pm_visits: notify failed for visit %s', visit.pk)

            # Advance the schedule
            schedule.advance()

            self.stdout.write(self.style.SUCCESS(
                f'  ✓ created PM visit for {schedule.contract.title} '
                f'(next due now: {schedule.next_due_date})'
            ))

        self.stdout.write(self.style.SUCCESS(f'Done. Created {created} new visit(s).'))
