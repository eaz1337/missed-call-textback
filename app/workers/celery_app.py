from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.config import settings
from app.core.logging import configure_logging

configure_logging()

celery_app = Celery("mctb", broker=settings.REDIS_URL, backend=settings.REDIS_URL)
celery_app.autodiscover_tasks(["app.workers"])
celery_app.autodiscover_tasks(["app.workers"], related_name="beat")

celery_app.conf.beat_schedule = {
    "anonymize-call-events-nightly": {
        "task": "app.workers.beat.anonymize_expired_call_events",
        "schedule": crontab(hour=3, minute=0),
    },
    "anonymize-sms-messages-nightly": {
        "task": "app.workers.beat.anonymize_expired_sms_messages",
        "schedule": crontab(hour=3, minute=15),
    },
}
