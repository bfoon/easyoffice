"""
Celery tasks. django_celery_beat uses the DatabaseScheduler, so create the
schedule once (management command `investments_setup_schedule` does it) or in
Django admin → Periodic tasks.
"""
import logging

from celery import shared_task

from apps.features.decorators import skip_if_disabled

logger = logging.getLogger(__name__)


@shared_task(name='investments.run_due_distributions')
@skip_if_disabled('investments', default={'skipped': 'feature off'})
def run_due_distributions_task():
    from apps.investments.services.distribution import run_due_distributions
    report = run_due_distributions()
    logger.info('investments distributions: %s', report)
    return report
