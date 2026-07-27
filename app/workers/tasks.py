"""Celery entry point for the async pipeline (spec.md 3.4).

Week 1 scope: enqueue + pick up the persisted call_event only. The guard
chain (tenant/anonymous/loop/opt-out/non_mobile/cooldown/limit), AI call,
and SMS send are Week 2/3 work — see spec.md section 8 (roadmap) and
app/services/guards.py once it exists.
"""

from __future__ import annotations

import structlog
from sqlalchemy import select

from app.db import SessionLocal
from app.models import CallEvent
from app.workers.celery_app import celery_app

logger = structlog.get_logger()


@celery_app.task(bind=True, max_retries=3, retry_backoff=True)  # type: ignore[untyped-decorator]
def process_missed_call(self: object, call_sid: str) -> None:
    db = SessionLocal()
    try:
        event = db.scalar(select(CallEvent).where(CallEvent.call_sid == call_sid))
        if event is None:
            logger.warning("call_event_not_found", call_sid=call_sid)
            return
        logger.info("call_event_received", call_sid=call_sid, client_id=str(event.client_id))
    finally:
        db.close()
