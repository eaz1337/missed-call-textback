"""Voice webhook: signature validation, TwiML shape, call_sid idempotency
(spec.md 3.2-3.3, 7.1, 7.6; CLAUDE.md Testing section).

Requires the schema to already exist against DATABASE_URL — see
tests/conftest.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.config import settings
from tests.conftest import twilio_signature

VOICE_URL = f"{settings.PUBLIC_BASE_URL}/webhooks/twilio/voice"


def _voice_form(call_sid: str) -> dict[str, str]:
    return {
        "CallSid": call_sid,
        "AccountSid": settings.TWILIO_ACCOUNT_SID,
        "From": "+48501234567",
        "To": "+48732000111",
        "CallStatus": "ringing",
        "Direction": "inbound",
        "ForwardedFrom": "+48601998877",
        "CallerName": "",
        "FromCountry": "PL",
        "ToCountry": "PL",
        "ApiVersion": "2010-04-01",
    }


def test_voice_webhook_valid_signature_returns_reject_twiml(
    client: TestClient, mock_enqueue: MagicMock
) -> None:
    form = _voice_form("CAvalidsig0000000000000000000001")
    signature = twilio_signature(VOICE_URL, form)

    response = client.post(
        "/webhooks/twilio/voice",
        data=form,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    assert "<Reject" in response.text
    assert 'reason="busy"' in response.text
    mock_enqueue.assert_called_once_with("CAvalidsig0000000000000000000001")


def test_voice_webhook_invalid_signature_returns_403(
    client: TestClient, mock_enqueue: MagicMock
) -> None:
    form = _voice_form("CAbadsig00000000000000000000001")

    response = client.post(
        "/webhooks/twilio/voice",
        data=form,
        headers={"X-Twilio-Signature": "not-a-valid-signature"},
    )

    assert response.status_code == 403
    mock_enqueue.assert_not_called()


def test_voice_webhook_duplicate_call_sid_enqueues_once(
    client: TestClient, mock_enqueue: MagicMock
) -> None:
    form = _voice_form("CAduplicate000000000000000000001")
    signature = twilio_signature(VOICE_URL, form)
    headers = {"X-Twilio-Signature": signature}

    first = client.post("/webhooks/twilio/voice", data=form, headers=headers)
    second = client.post("/webhooks/twilio/voice", data=form, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert "<Reject" in second.text
    mock_enqueue.assert_called_once_with("CAduplicate000000000000000000001")
