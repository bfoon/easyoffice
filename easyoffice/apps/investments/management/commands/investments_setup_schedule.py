"""Create/refresh the nightly Celery Beat job (00:20 server time)."""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Register the nightly investments distribution job with django_celery_beat.'

    def handle(self, *args, **opts):
        from django_celery_beat.models import CrontabSchedule, PeriodicTask
        from django.conf import settings
        cron, _ = CrontabSchedule.objects.get_or_create(
            minute='20', hour='0', day_of_week='*', day_of_month='*', month_of_year='*',
            timezone=getattr(settings, 'TIME_ZONE', 'UTC'))
        task, created = PeriodicTask.objects.update_or_create(
            name='Investments: run due distributions',
            defaults={'task': 'investments.run_due_distributions', 'crontab': cron, 'enabled': True})
        self.stdout.write(self.style.SUCCESS(('Created' if created else 'Updated') + f' "{task.name}".'))
