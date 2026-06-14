"""
Celery task for scheduled ZKTeco syncing.
Append to apps/hr/tasks.py (or create it).

Schedule it in your Celery beat config, e.g. every 15 minutes:

    CELERY_BEAT_SCHEDULE = {
        'sync-zk-attendance': {
            'task': 'apps.hr.tasks.sync_zk_attendance_task',
            'schedule': crontab(minute='*/15'),
        },
    }
"""
import logging

from celery import shared_task

from apps.hr import zk_service

logger = logging.getLogger(__name__)


@shared_task(name='apps.hr.tasks.sync_zk_attendance_task')
def sync_zk_attendance_task():
    """Pull all active terminals and derive attendance. Safe to run on a schedule."""
    summary = zk_service.sync_all_active_devices()
    logger.info('Scheduled ZK sync: %s', summary)
    return summary
