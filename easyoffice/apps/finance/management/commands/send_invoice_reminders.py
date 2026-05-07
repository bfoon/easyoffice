# management/commands/send_invoice_reminders.py
from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from apps.finance.models import IncomingPaymentRequest
from apps.finance.views import _send_invoice_reminder_email

class Command(BaseCommand):
    def handle(self, *args, **options):
        overdue = IncomingPaymentRequest.objects.filter(
            status__in=['sent', 'overdue'],
            due_date__lt=timezone.now().date(),
            customer_email__isnull=False,
        ).exclude(customer_email='')
        # Only send if no reminder in past 3 days
        cutoff = timezone.now() - timedelta(days=3)
        to_remind = overdue.filter(
            last_reminder_sent__lt=cutoff
        ) | overdue.filter(last_reminder_sent__isnull=True)
        for ipr in to_remind:
            _send_invoice_reminder_email(ipr)
            ipr.reminder_count += 1
            ipr.last_reminder_sent = timezone.now()
            ipr.status = 'overdue'
            ipr.save(update_fields=['reminder_count', 'last_reminder_sent', 'status'])
        self.stdout.write(f'Sent {to_remind.count()} reminders.')