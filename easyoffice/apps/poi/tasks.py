"""
apps/poi/tasks.py
─────────────────
Celery task that drives the daily POI expiry. Calls the same management command
logic so there's a single implementation.

Wire into your beat schedule (settings.py), e.g.:

    from celery.schedules import crontab
    CELERY_BEAT_SCHEDULE = {
        "expire-pois-daily": {
            "task": "apps.poi.tasks.expire_pois",
            "schedule": crontab(hour=2, minute=0),   # 02:00 every day
        },
    }
"""

import logging

try:
    from celery import shared_task
except Exception:  # Celery not installed in some environments
    def shared_task(*a, **k):
        def deco(fn):
            return fn
        return deco if not (a and callable(a[0])) else a[0]

log = logging.getLogger(__name__)


@shared_task(name="apps.poi.tasks.expire_pois")
def expire_pois():
    from apps.poi.models import POIProfile
    n = POIProfile.expire_due()
    log.info("expire_pois task: expired %s POI(s)", n)
    return n
