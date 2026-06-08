"""
apps/logistics/management/commands/issue_driver_link.py

    python manage.py issue_driver_link --driver "Modou Jeng" --base-url https://ops.example.com
    python manage.py issue_driver_link --driver-id <uuid> --revoke
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Issue or revoke a driver portal link.'

    def add_arguments(self, parser):
        parser.add_argument('--driver', help='Driver full name (exact match).')
        parser.add_argument('--driver-id', help='Driver UUID.')
        parser.add_argument('--base-url', default='', help='Base URL for the printed link.')
        parser.add_argument('--revoke', action='store_true')

    def handle(self, *args, **opts):
        from apps.logistics.models import Driver, DriverPortalToken

        driver = None
        if opts['driver_id']:
            driver = Driver.objects.filter(pk=opts['driver_id']).first()
        elif opts['driver']:
            driver = Driver.objects.filter(full_name=opts['driver']).first()
        if not driver:
            raise CommandError('Driver not found (use --driver "Name" or --driver-id UUID).')

        tok, created = DriverPortalToken.objects.get_or_create(driver=driver)
        if opts['revoke']:
            tok.revoke()
            self.stdout.write(self.style.WARNING(f'Revoked link for {driver.full_name}.'))
            return

        base = opts['base_url'].rstrip('/')
        link = f'{base}/drive/{tok.token}/' if base else f'/drive/{tok.token}/'
        state = 'created' if created else 'existing'
        self.stdout.write(self.style.SUCCESS(f'[{state}] {driver.full_name}: {link}'))
