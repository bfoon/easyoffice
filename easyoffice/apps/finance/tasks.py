"""
apps/finance/tasks.py
=====================

Celery tasks for the finance app. Currently:
    - generate_due_contract_invoices: scheduled daily by Celery Beat to
      generate invoices for contracts whose next_invoice_date is due.

Beat schedule (add to your celery config / settings.py):

    from celery.schedules import crontab

    CELERY_BEAT_SCHEDULE = {
        'generate-contract-invoices-daily': {
            'task': 'apps.finance.tasks.generate_due_contract_invoices',
            'schedule': crontab(hour=6, minute=0),  # 6am daily
        },
    }
"""
import logging
from celery import shared_task

from apps.finance.contract_invoice_service import run_scheduled_generation

logger = logging.getLogger(__name__)


@shared_task(
    name='apps.finance.tasks.generate_due_contract_invoices',
    bind=True,
    max_retries=2,
    default_retry_delay=60 * 10,  # 10 min between retries
)
def generate_due_contract_invoices(self):
    """
    Generate invoices for every contract whose next_invoice_date is due.
    Runs once a day; safe to run multiple times (idempotent per period
    because we advance next_invoice_date after each successful generation).
    """
    try:
        summary = run_scheduled_generation()
    except Exception as exc:
        logger.exception('Scheduled contract invoice generation crashed')
        # retry the whole batch — individual contract errors are caught inside
        # run_scheduled_generation so this only fires for setup-level problems
        # (e.g. no system user available).
        raise self.retry(exc=exc)

    logger.info(
        'Contract invoices: %s attempted, %s succeeded, %s failed',
        summary['attempted'], summary['succeeded'], summary['failed'],
    )
    if summary['errors']:
        for cid, err in summary['errors']:
            logger.warning('Contract %s: %s', cid, err)

    return summary
