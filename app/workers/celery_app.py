from __future__ import annotations

from celery import Celery

from app.config import settings

celery_app = Celery("mctb", broker=settings.REDIS_URL, backend=settings.REDIS_URL)
celery_app.autodiscover_tasks(["app.workers"])
