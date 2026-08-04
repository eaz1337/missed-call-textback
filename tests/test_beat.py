"""anonymize_expired_call_events / anonymize_expired_sms_messages (spec.md
4.3, CLAUDE.md invariant 8). Needs real Postgres for sha256()/interval
arithmetic, so these run against db_session like the other DB-touching
tests (see tests/test_tasks.py for the SessionLocal-monkeypatch pattern).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import CallEvent, Client, SmsMessage
from app.workers.beat import anonymize_expired_call_events, anonymize_expired_sms_messages


@pytest.fixture
def use_test_session(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> Session:
    monkeypatch.setattr("app.workers.beat.SessionLocal", lambda: db_session)
    # See tests/test_tasks.py's identically-named fixture: production code
    # closes a session it exclusively owns; here it's shared with the test.
    monkeypatch.setattr(Session, "close", lambda self: None)
    return db_session


def _make_client(db_session: Session, *, log_retention_days: int = 90) -> Client:
    client = Client(
        company_name="Test Co",
        email=f"{uuid.uuid4()}@example.com",
        owner_phone_e164="+48501111111",
        log_retention_days=log_retention_days,
    )
    db_session.add(client)
    db_session.flush()
    return client


def test_anonymize_expired_call_events_hashes_and_nulls_caller(
    use_test_session: Session,
) -> None:
    client = _make_client(use_test_session)
    event = CallEvent(
        call_sid="CAexpired00000000000000000001",
        client_id=client.id,
        caller_e164="+48501234567",
        forwarded_from="+48221234567",
        received_at=datetime.now(UTC) - timedelta(days=91),
    )
    use_test_session.add(event)
    use_test_session.flush()

    anonymize_expired_call_events.apply()

    use_test_session.refresh(event)
    assert event.caller_e164 is None
    assert event.forwarded_from is None
    assert event.anonymized_at is not None
    expected_hash = hashlib.sha256(f"+48501234567{client.anonymization_salt}".encode()).hexdigest()
    assert event.caller_hash == expected_hash


def test_anonymize_expired_call_events_leaves_recent_rows_untouched(
    use_test_session: Session,
) -> None:
    client = _make_client(use_test_session)
    event = CallEvent(
        call_sid="CArecent000000000000000000001",
        client_id=client.id,
        caller_e164="+48501234567",
    )
    use_test_session.add(event)
    use_test_session.flush()

    anonymize_expired_call_events.apply()

    use_test_session.refresh(event)
    assert event.caller_e164 == "+48501234567"
    assert event.anonymized_at is None


def test_anonymize_expired_sms_messages_nulls_number_and_replaces_body_with_length(
    use_test_session: Session,
) -> None:
    client = _make_client(use_test_session)
    event = CallEvent(
        call_sid="CAsmsexpired0000000000000001",
        client_id=client.id,
        caller_e164="+48501234567",
    )
    use_test_session.add(event)
    use_test_session.flush()

    body = "Dziekujemy za telefon, oddzwonimy wkrotce."
    sms = SmsMessage(
        call_event_id=event.id,
        client_id=client.id,
        to_e164="+48501234567",
        body=body,
        created_at=datetime.now(UTC) - timedelta(days=91),
    )
    use_test_session.add(sms)
    use_test_session.flush()

    anonymize_expired_sms_messages.apply()

    use_test_session.refresh(sms)
    assert sms.to_e164 is None
    assert sms.body == str(len(body))
    assert sms.anonymized_at is not None


def test_anonymize_expired_sms_messages_leaves_recent_rows_untouched(
    use_test_session: Session,
) -> None:
    client = _make_client(use_test_session)
    event = CallEvent(
        call_sid="CAsmsrecent00000000000000001",
        client_id=client.id,
        caller_e164="+48501234567",
    )
    use_test_session.add(event)
    use_test_session.flush()

    sms = SmsMessage(
        call_event_id=event.id,
        client_id=client.id,
        to_e164="+48501234567",
        body="still fresh",
    )
    use_test_session.add(sms)
    use_test_session.flush()

    anonymize_expired_sms_messages.apply()

    use_test_session.refresh(sms)
    assert sms.to_e164 == "+48501234567"
    assert sms.body == "still fresh"
    assert sms.anonymized_at is None
