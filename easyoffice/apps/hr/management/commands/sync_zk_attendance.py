"""
Sync ZKTeco devices from the command line or cron.

    python manage.py sync_zk_attendance
    python manage.py sync_zk_attendance --device <uuid>
    python manage.py sync_zk_attendance --derive-only

Place at: apps/hr/management/commands/sync_zk_attendance.py
(ensure apps/hr/management/ and apps/hr/management/commands/ each have __init__.py)
"""
from django.core.management.base import BaseCommand, CommandError

from apps.hr.models import ZKDevice
from apps.hr import zk_service
from apps.hr.zk_client import ZKConnectionError


class Command(BaseCommand):
    help = 'Pull punch logs from ZKTeco terminals and derive attendance records.'

    def add_arguments(self, parser):
        parser.add_argument('--device', help='Sync a single device by UUID.')
        parser.add_argument(
            '--derive-only',
            action='store_true',
            help='Skip hardware; just fold existing raw punches into attendance.',
        )

    def handle(self, *args, **options):
        if options['derive_only']:
            summary = zk_service.derive_attendance()
            self.stdout.write(self.style.SUCCESS(f'Derive complete: {summary}'))
            return

        if options['device']:
            device = ZKDevice.objects.filter(pk=options['device']).first()
            if not device:
                raise CommandError(f'No device with id {options["device"]}')
            try:
                summary = zk_service.sync_device(device)
                self.stdout.write(self.style.SUCCESS(f'{device.name}: {summary}'))
            except ZKConnectionError as exc:
                raise CommandError(str(exc))
            return

        summary = zk_service.sync_all_active_devices()
        self.stdout.write(self.style.SUCCESS(f'All devices: {summary}'))
