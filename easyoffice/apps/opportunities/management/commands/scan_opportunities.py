from django.core.management.base import BaseCommand
from apps.opportunities.services import scan_all_sources


class Command(BaseCommand):
    help = 'Scan all active opportunity sources.'

    def handle(self, *args, **options):
        created = scan_all_sources()
        self.stdout.write(self.style.SUCCESS(f'Scan completed. New matches found: {created}'))