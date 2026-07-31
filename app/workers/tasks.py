"""Celery entry point for the async pipeline (spec.md 3.4).

Guard order is significant (cheapest first, CLAUDE.md invariant 3): tenant
-> anonymous/loop/opted-out/non-mobile -> cooldown -> daily limit -> content
-> send. AI generation (Phase 1) is Week 3 work; until `ai_client.py`
exists, every send uses `ai_prompts.fallback_message` directly — which is
exactly the "AI failure never blocks the SMS" path invariant 4 describes,
just without an AI call to fail yet.
"""

from __future__ import annotations

import structlog
from celery import Task
from sqlalchemy import select
from sqlalchemy.orm import Session
from twilio.base.exceptions import TwilioRestException

from app.core.phone import is_mobile_number
from app.core.sms_encoding import compute_encoding_and_segments, prepare_sms_body
from app.db import SessionLocal
from app.models import CallEvent, CallEventStatus, SmsMessage
from app.redis_client import redis_client
from app.services.guards import (
    acquire_cooldown,
    check_daily_limit,
    is_known_non_mobile,
    is_loop_caller,
    is_opted_out,
)
from app.services.sms_sender import send_sms
from app.services.tenant_resolver import Tenant, resolve_tenant
from app.workers.celery_app import celery_app

logger = structlog.get_logger()


@celery_app.task(bind=True, max_retries=3, retry_backoff=True)  # type: ignore[untyped-decorator]
def process_missed_call(self: Task, call_sid: str) -> None:
    db = SessionLocal()
    try:
        _run(self, db, call_sid)
    finally:
        db.close()


def _run(task: Task, db: Session, call_sid: str) -> None:
    event = db.scalar(select(CallEvent).where(CallEvent.call_sid == call_sid))
    if event is None:
        logger.warning("call_event_not_found", call_sid=call_sid)
        return

    # --- GUARD 1: tenant routing ---
    twilio_number_id = event.twilio_number_id
    client_id = event.client_id
    if twilio_number_id is None or client_id is None:
        _mark(db, event, CallEventStatus.NO_TENANT)
        return
    tenant = resolve_tenant(db, twilio_number_id)
    if tenant is None or not tenant.twilio_number_is_active:
        _mark(db, event, CallEventStatus.NO_TENANT)
        return
    if tenant.client_status != "active":
        _mark(db, event, CallEventStatus.CLIENT_SUSPENDED)
        return

    # --- GUARD 2: caller filter ---
    # caller_e164 is already normalize_e164()'d by the webhook (invariant
    # 2) — anonymous markers and unparseable numbers both collapse to NULL
    # there, so this single check stands in for spec.md 3.4's separate
    # is_anonymous/invalid_number branches.
    caller_e164 = event.caller_e164
    if caller_e164 is None:
        _mark(db, event, CallEventStatus.ANONYMOUS)
        return
    if is_loop_caller(
        db,
        caller_e164=caller_e164,
        owner_phone_e164=tenant.client_owner_phone_e164,
        forwarded_from_e164=event.forwarded_from,
    ):
        _mark(db, event, CallEventStatus.LOOP_DETECTED)
        return
    if is_opted_out(db, client_id=tenant.client_id, phone_e164=caller_e164):
        _mark(db, event, CallEventStatus.OPTED_OUT)
        return
    if not is_mobile_number(caller_e164, tenant.client_country_code) or is_known_non_mobile(
        redis_client, caller_e164
    ):
        _mark(db, event, CallEventStatus.NON_MOBILE)
        return

    # --- GUARD 3: dedup + limits (Redis) ---
    if not acquire_cooldown(
        redis_client, client_id=tenant.client_id, caller_e164=caller_e164, call_sid=call_sid
    ):
        _mark(db, event, CallEventStatus.DEDUPLICATED)
        return
    if not check_daily_limit(
        redis_client, client_id=tenant.client_id, daily_limit=tenant.client_daily_sms_limit
    ):
        _mark(db, event, CallEventStatus.LIMIT_EXCEEDED)
        logger.warning("daily_limit_exceeded", call_sid=call_sid, client_id=str(tenant.client_id))
        return

    # --- Idempotency: a retry after a partial run must not double-send ---
    if db.scalar(select(SmsMessage.id).where(SmsMessage.call_event_id == event.id)) is not None:
        logger.info("sms_already_sent", call_sid=call_sid, client_id=str(tenant.client_id))
        return

    _send(task, db, event, tenant, caller_e164, call_sid)


def _send(
    task: Task,
    db: Session,
    event: CallEvent,
    tenant: Tenant,
    caller_e164: str,
    call_sid: str,
) -> None:
    # --- Content generation ---
    # Week 3 wraps this in a try/except around ai_client.generate()
    # (invariant 4: AI failure never blocks the SMS); until ai_client.py
    # exists, every send goes straight to the fallback template.
    fallback = tenant.fallback_message
    if fallback is None:
        logger.error("no_active_prompt", call_sid=call_sid, client_id=str(tenant.client_id))
        _mark(db, event, CallEventStatus.NO_TENANT)
        return

    body = prepare_sms_body(
        fallback,
        allow_diacritics=tenant.allow_diacritics,
        max_segments=tenant.max_sms_segments,
        country_code=tenant.client_country_code,
    )
    encoding, segments = compute_encoding_and_segments(body)

    # --- Sending ---
    try:
        result = send_sms(from_e164=tenant.twilio_number_e164, to_e164=caller_e164, body=body)
    except TwilioRestException as exc:
        if exc.status is not None and 400 <= exc.status < 500 and exc.status != 429:
            # spec.md 7.6: 4xx (e.g. 21211 invalid number) — no retry, log + sms_failed.
            logger.warning(
                "sms_send_failed",
                call_sid=call_sid,
                client_id=str(tenant.client_id),
                twilio_status=exc.status,
                twilio_code=exc.code,
            )
            db.add(
                SmsMessage(
                    call_event_id=event.id,
                    client_id=tenant.client_id,
                    to_e164=caller_e164,
                    body=body,
                    encoding=encoding,
                    segments=segments,
                    is_fallback=True,
                    status="failed",
                    error_code=str(exc.code) if exc.code is not None else None,
                )
            )
            _mark(db, event, CallEventStatus.SMS_FAILED)
            return
        # spec.md 7.6: 429/5xx are transient — retried via the Celery mechanism.
        raise task.retry(exc=exc) from exc

    db.add(
        SmsMessage(
            call_event_id=event.id,
            client_id=tenant.client_id,
            message_sid=result.message_sid,
            to_e164=caller_e164,
            body=body,
            encoding=encoding,
            segments=segments,
            is_fallback=True,
        )
    )
    _mark(db, event, CallEventStatus.SMS_QUEUED)


def _mark(db: Session, event: CallEvent, status: CallEventStatus) -> None:
    event.status = status.value
    db.commit()
