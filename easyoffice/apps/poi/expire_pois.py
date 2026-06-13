"""
apps/poi/management/commands/expire_pois.py
Run daily. Expires every POI whose 30-day trust window has lapsed:
untrusts their device and disables the account.

    python manage.py expire_pois
"""

from django.core.management.base import BaseCommand

from apps.poi.models import POIProfile


class Command(BaseCommand):
    help = "Expire POIs whose 30-day trusted-device window has lapsed."

    def handle(self, *args, **options):
        n = POIProfile.expire_due()
        self.stdout.write(self.style.SUCCESS(f"Expired {n} POI account(s)."))
