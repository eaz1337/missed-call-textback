"""Twilio Messages API integration (spec.md 3.4, 3.5).

The only place holding a Twilio REST client for sending — `process_missed_call`
(app/workers/tasks.py) must call into `send_sms` here, never
`twilio.messages.create` directly (CLAUDE.md Structure): the inline call in
spec.md 3.4 is illustrative pseudocode, not a file placement.
"""

from __future__ import annotations

from dataclasses import dataclass

from twilio.rest import Client

from app.config import settings

SMS_STATUS_CALLBACK_PATH = "/webhooks/twilio/sms-status"

_client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


@dataclass(frozen=True)
class SmsSendResult:
    message_sid: str
    status: str


def send_sms(*, from_e164: str, to_e164: str, body: str) -> SmsSendResult:
    """Sends the text-back SMS, wiring `status_callback` to our
    `/webhooks/twilio/sms-status` endpoint (spec.md 3.4, 3.5) so message
    lifecycle events come back to us instead of Twilio Smart Encoding
    guessing at content we already normalized ourselves (spec.md 4.2 rule
    3). Raises `twilio.base.exceptions.TwilioRestException` on a non-2xx
    Twilio response — the caller (tasks.py) decides retry vs. terminal
    failure per spec.md 7.6.
    """
    status_callback = f"{settings.PUBLIC_BASE_URL}{SMS_STATUS_CALLBACK_PATH}"
    message = _client.messages.create(
        from_=from_e164,
        to=to_e164,
        body=body,
        status_callback=status_callback,
    )
    return SmsSendResult(message_sid=message.sid, status=message.status)
