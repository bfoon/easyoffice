from django.core.management.base import BaseCommand

from apps.investments.services.distribution import run_due_distributions


class Command(BaseCommand):
    help = 'Calculate (and auto-post where enabled) every completed, unpaid profit period.'

    def handle(self, *args, **opts):
        report = run_due_distributions()
        self.stdout.write(self.style.SUCCESS(str(report)))
