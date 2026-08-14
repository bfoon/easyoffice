"""
apps/inventory/management/commands/check_license_expiry.py
──────────────────────────────────────────────────────────
The nightly licence job: warn before expiry, flag what has expired.

Run it once a day — early enough that the mail is waiting when people
start work:

    # crontab -e
    30 6 * * *  cd /srv/easyoffice && /srv/easyoffice/venv/bin/python \\
                manage.py check_license_expiry >> /var/log/easyoffice/licences.log 2>&1

Or, with Celery beat in settings.py:

    CELERY_BEAT_SCHEDULE = {
        'license-expiry': {
            'task': 'django.core.management.call_command',
            'schedule': crontab(hour=6, minute=30),
            'args': ('check_license_expiry',),
        },
    }

Safe to run more than once a day: each reminder threshold fires exactly
once per term, recorded on the licence itself.

Options
───────
  --dry-run   Show what would be sent. Nothing is emailed or written.
  --force     Re-send the current threshold even if it already fired.
              For testing mail settings — use it on one licence with
              --reference.
  --reference Limit the run to one licence reference (LIC-202608-A1B2C).
  --quiet     Only print the summary line.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.inventory import license_services as services
from apps.inventory.license_models import License


class Command(BaseCommand):
    help = 'Send licence expiry reminders and mark expired licences.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report without sending or saving anything.')
        parser.add_argument('--force', action='store_true',
                            help='Re-send the current threshold.')
        parser.add_argument('--reference', type=str, default='',
                            help='Only check this licence reference.')
        parser.add_argument('--quiet', action='store_true',
                            help='Summary line only.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        force = options['force']
        reference = (options['reference'] or '').strip()
        quiet = options['quiet']

        if reference:
            lic = License.objects.filter(reference__iexact=reference).first()
            if not lic:
                raise CommandError(f'No licence with reference {reference}.')
            # Scoped run: temporarily narrow the scan to this one record by
            # deactivating alerts elsewhere is too invasive — instead we call
            # the same code path on a single-item basis.
            result = self._single(lic, dry_run=dry_run, force=force)
        else:
            result = services.run_expiry_scan(dry_run=dry_run, force=force)

        if not quiet:
            for row in result['details']:
                marker = '↻' if row['action'] == 'reminder' else '⛔'
                self.stdout.write(
                    f" {marker} {row['reference']:<22} {row['name'][:40]:<42}"
                    f" {row['days_left']:>5} day(s)"
                )

        prefix = 'DRY RUN — ' if dry_run else ''
        line = (f"{prefix}checked {result['checked']} licence(s): "
                f"{result['reminders']} reminder(s), "
                f"{result['expired']} expiry notice(s), "
                f"{result['skipped']} skipped.")
        self.stdout.write(self.style.SUCCESS(line))

    # ── One-licence run, same rules as the full scan ────────────────────────
    def _single(self, lic, *, dry_run, force):
        from django.utils import timezone
        from apps.inventory import license_notifications as notify

        summary = {'checked': 1, 'reminders': 0, 'expired': 0, 'skipped': 0,
                   'dry_run': dry_run, 'ran_at': timezone.now(), 'details': []}

        if lic.is_perpetual or not lic.end_date:
            summary['skipped'] = 1
            self.stdout.write('Perpetual licence — nothing to check.')
            return summary

        days = lic.days_remaining
        if days >= 0:
            crossed = lic.crossed_reminders
            if force and not crossed:
                ladder = lic.get_reminder_days()
                crossed = [min((t for t in ladder if days <= t), default=days)]
            if not crossed:
                summary['skipped'] = 1
                self.stdout.write(
                    f'{lic.reference}: {days} day(s) left, no threshold due.')
                return summary
            summary['reminders'] = 1
            summary['details'].append({
                'reference': lic.reference, 'name': lic.name,
                'action': 'reminder', 'days_left': days, 'threshold': crossed[0],
            })
            if not dry_run:
                lic.reminders_sent = sorted(lic.fired_reminders | set(crossed),
                                            reverse=True)
                lic.last_reminder_at = timezone.now()
                lic.save(update_fields=['reminders_sent', 'last_reminder_at',
                                        'updated_at'])
                notify.notify_license_expiring(lic, days_left=days)
        else:
            summary['expired'] = 1
            summary['details'].append({
                'reference': lic.reference, 'name': lic.name,
                'action': 'expired', 'days_left': days, 'threshold': None,
            })
            if not dry_run:
                lic.expired_notified_at = timezone.now()
                lic.save(update_fields=['expired_notified_at', 'updated_at'])
                notify.notify_license_expired(lic)
        return summary
