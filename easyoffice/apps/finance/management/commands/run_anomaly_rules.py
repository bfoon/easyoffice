"""Run batch anomaly rules. Schedule nightly via cron or Celery beat:

    0 2 * * *   manage.py run_anomaly_rules

Output is a JSON dict on stdout: {rule_name: count_created, ...}
"""
import json

from django.core.management.base import BaseCommand

from apps.finance.anomalies import run_batch_rules


class Command(BaseCommand):
    help = 'Run all batch anomaly rules and emit a JSON summary.'

    def handle(self, *args, **options):
        result = run_batch_rules()
        self.stdout.write(json.dumps(result, indent=2))
        total = sum(v for v in result.values() if v > 0)
        self.stdout.write(self.style.SUCCESS(
            f'\nCreated {total} new anomalies.'
        ))
