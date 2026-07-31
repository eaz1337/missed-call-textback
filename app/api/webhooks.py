"""Twilio webhook endpoints.

Per CLAUDE.md: this module does ONLY signature validation, persisting the
call event, enqueueing the worker job, and returning TwiML. No AI or Twilio
Messages API calls happen here (spec.md 3.3, invariant 1) — deciding the
final call_events.status (guards) is entirely the worker's job
(app/workers/tasks.py), even though the tenant lookup below (a single
indexed read) happens here to populate the FK columns at insert time.

The SMS status callback (spec.md 3.5) also lives here: it only updates
`sms_messages` by `MessageSid` and feeds the opt-out / non-mobile guards —
no AI or outbound Twilio Messages API calls either.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.phone import normalize_e164
from app.core.security import verify_twilio_signature
from app.db import get_db
from app.models import CallEvent, SmsMessage, TwilioNumber
from app.redis_client import redis_client
from app.services.guards import mark_non_mobile, record_opt_out
from app.workers.tasks import process_missed_call

router = APIRouter(prefix="/webhooks/twilio")
logger = structlog.get_logger()

# Twilio's MessageStatus values collapsed onto our restricted
# sms_messages.status CHECK constraint (app.models.sms_message.SMS_STATUSES).
_TWILIO_STATUS_MAP = {
    "accepted": "queued",
    "scheduled": "queued",
    "queued": "queued",
    "sending": "queued",
    "sent": "sent",
    "delivered": "delivered",
    "undelivered": "undelivered",
    "failed": "failed",
    "canceled": "failed",
}

_REJECT_TWIML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<Response><Reject reason="busy"/></Response>'
)


def _twiml_reject() -> Response:
    return Response(content=_REJECT_TWIML, media_type="application/xml")


@router.post("/voice")
async def voice(
    form: Annotated[dict[str, str], Depends(verify_twilio_signature)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    call_sid = form.get("CallSid", "")
    to_e164 = normalize_e164(form.get("To"))
    caller_e164 = normalize_e164(form.get("From"))
    forwarded_from_e164 = normalize_e164(form.get("ForwardedFrom"))

    twilio_number = None
    if to_e164 is not None:
        twilio_number = db.scalar(
            select(TwilioNumber).where(
                TwilioNumber.phone_e164 == to_e164, TwilioNumber.is_active.is_(True)
            )
        )

    stmt = (
        pg_insert(CallEvent)
        .values(
            call_sid=call_sid,
            client_id=twilio_number.client_id if twilio_number else None,
            twilio_number_id=twilio_number.id if twilio_number else None,
            caller_e164=caller_e164,
            forwarded_from=forwarded_from_e164,
        )
        .on_conflict_do_nothing(index_elements=["call_sid"])
        .returning(CallEvent.id)
    )
    inserted_id = db.scalar(stmt)
    db.commit()

    if inserted_id is not None:
        process_missed_call.delay(call_sid)

    return _twiml_reject()


@router.post("/sms-status")
async def sms_status(
    form: Annotated[dict[str, str], Depends(verify_twilio_signature)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    message_sid = form.get("MessageSid", "")
    sms = db.scalar(select(SmsMessage).where(SmsMessage.message_sid == message_sid))
    if sms is None:
        logger.warning("sms_status_unknown_message_sid", message_sid=message_sid)
        return Response(status_code=204)

    mapped_status = _TWILIO_STATUS_MAP.get(form.get("MessageStatus", ""))
    if mapped_status is not None:
        sms.status = mapped_status
    else:
        logger.warning("sms_status_unmapped", twilio_status=form.get("MessageStatus"))

    error_code = form.get("ErrorCode") or None
    sms.error_code = error_code
    db.commit()

    # spec.md 3.5: error codes that need follow-up action beyond the status update.
    if error_code == "21610" and sms.to_e164 is not None:
        record_opt_out(db, client_id=sms.client_id, phone_e164=sms.to_e164, source="twilio_21610")
    elif error_code in ("21614", "30006") and sms.to_e164 is not None:
        mark_non_mobile(redis_client, sms.to_e164)
    elif error_code == "30003":
        logger.info("sms_undeliverable", message_sid=message_sid)

    return Response(status_code=204)
