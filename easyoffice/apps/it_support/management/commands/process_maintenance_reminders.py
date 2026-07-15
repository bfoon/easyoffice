from django.core.management.base import BaseCommand

from apps.it_support.tasks import process_maintenance_reminders


class Command(BaseCommand):
    help = 'Send due equipment-maintenance and pickup reminders.'

    def handle(self, *args, **options):
        count = process_maintenance_reminders.run()
        self.stdout.write(self.style.SUCCESS(f'Processed {count} maintenance reminder(s).'))
