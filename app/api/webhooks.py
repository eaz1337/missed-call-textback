"""Twilio webhook endpoints.

Per CLAUDE.md: this module does ONLY signature validation, persisting the
call event, enqueueing the worker job, and returning TwiML. No AI or Twilio
Messages API calls happen here (spec.md 3.3, invariant 1) — deciding the
final call_events.status (guards) is entirely the worker's job
(app/workers/tasks.py), even though the tenant lookup below (a single
indexed read) happens here to populate the FK columns at insert time.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.phone import normalize_e164
from app.core.security import verify_twilio_signature
from app.db import get_db
from app.models import CallEvent, TwilioNumber
from app.workers.tasks import process_missed_call

router = APIRouter(prefix="/webhooks/twilio")

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
