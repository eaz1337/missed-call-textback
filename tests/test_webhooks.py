"""Twilio webhooks: voice (spec.md 3.2-3.3, 7.1, 7.6), sms-status (3.5),
inbound-sms (3.6; CLAUDE.md Testing section).

Requires the schema to already exist against DATABASE_URL — see
tests/conftest.py.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Client, TwilioNumber
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


INBOUND_SMS_URL = f"{settings.PUBLIC_BASE_URL}/webhooks/twilio/inbound-sms"


def _inbound_sms_form(from_number: str, to_number: str, body: str) -> dict[str, str]:
    return {
        "From": from_number,
        "To": to_number,
        "Body": body,
        "MessageSid": f"SM{uuid.uuid4().hex[:29]}",
        "AccountSid": settings.TWILIO_ACCOUNT_SID,
    }


def test_inbound_sms_stop_records_opt_out(client: TestClient, db_session: Session) -> None:
    # Set up a client + Twilio number
    cli = Client(
        company_name="Test Co",
        email=f"{uuid.uuid4()}@example.com",
        owner_phone_e164="+48501111111",
    )
    db_session.add(cli)
    db_session.flush()

    number = TwilioNumber(
        client_id=cli.id, phone_e164="+48732000111", twilio_sid="PNtestinbound0001"
    )
    db_session.add(number)
    db_session.flush()

    form = _inbound_sms_form(from_number="+48501234567", to_number="+48732000111", body="STOP")
    signature = twilio_signature(INBOUND_SMS_URL, form)

    response = client.post(
        "/webhooks/twilio/inbound-sms",
        data=form,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200
    assert "<Response" in response.text

    # Verify opt-out was recorded
    from app.models import OptOut

    opt_out = (
        db_session.query(OptOut).filter_by(client_id=cli.id, phone_e164="+48501234567").first()
    )
    assert opt_out is not None
    assert opt_out.source == "sms_stop"


def test_inbound_sms_koniec_records_opt_out(client: TestClient, db_session: Session) -> None:
    # Test Polish opt-out keyword
    cli = Client(
        company_name="Test Co",
        email=f"{uuid.uuid4()}@example.com",
        owner_phone_e164="+48501111111",
    )
    db_session.add(cli)
    db_session.flush()

    number = TwilioNumber(
        client_id=cli.id, phone_e164="+48732000112", twilio_sid="PNtestinbound0002"
    )
    db_session.add(number)
    db_session.flush()

    form = _inbound_sms_form(from_number="+48501234568", to_number="+48732000112", body="KONIEC")
    signature = twilio_signature(INBOUND_SMS_URL, form)

    response = client.post(
        "/webhooks/twilio/inbound-sms",
        data=form,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200

    from app.models import OptOut

    opt_out = (
        db_session.query(OptOut).filter_by(client_id=cli.id, phone_e164="+48501234568").first()
    )
    assert opt_out is not None


def test_inbound_sms_non_opt_out_does_not_record(client: TestClient, db_session: Session) -> None:
    # Non-opt-out message should be logged but not recorded as opt-out
    cli = Client(
        company_name="Test Co",
        email=f"{uuid.uuid4()}@example.com",
        owner_phone_e164="+48501111111",
    )
    db_session.add(cli)
    db_session.flush()

    number = TwilioNumber(
        client_id=cli.id, phone_e164="+48732000113", twilio_sid="PNtestinbound0003"
    )
    db_session.add(number)
    db_session.flush()

    form = _inbound_sms_form(
        from_number="+48501234569", to_number="+48732000113", body="Hallo, when are you open?"
    )
    signature = twilio_signature(INBOUND_SMS_URL, form)

    response = client.post(
        "/webhooks/twilio/inbound-sms",
        data=form,
        headers={"X-Twilio-Signature": signature},
    )

    assert response.status_code == 200

    from app.models import OptOut

    opt_out = (
        db_session.query(OptOut).filter_by(client_id=cli.id, phone_e164="+48501234569").first()
    )
    assert opt_out is None


def test_inbound_sms_invalid_signature_returns_403(client: TestClient) -> None:
    form = _inbound_sms_form(from_number="+48501234567", to_number="+48732000111", body="STOP")

    response = client.post(
        "/webhooks/twilio/inbound-sms",
        data=form,
        headers={"X-Twilio-Signature": "invalid"},
    )

    assert response.status_code == 403
