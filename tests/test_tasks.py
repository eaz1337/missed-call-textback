"""process_missed_call: the full guard chain + send (spec.md 3.4, 7.2-7.6;
CLAUDE.md invariants 3, 4, 6). Redis is fakeredis; Twilio's send is always
mocked — no network calls, per CLAUDE.md.

`process_missed_call` opens its own SQLAlchemy session via `SessionLocal()`
rather than taking one as a dependency, so — unlike the webhook tests, which
override FastAPI's `get_db` — these tests monkeypatch
`app.workers.tasks.SessionLocal` to hand back the test's transactional
`db_session`, keeping every DB write inside the same rolled-back
transaction as the fixtures that set up the scenario.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from unittest.mock import MagicMock

import fakeredis
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from twilio.base.exceptions import TwilioRestException

from app.models import AiPrompt, CallEvent, Client, SmsMessage, TwilioNumber
from app.services import tenant_resolver
from app.services.sms_sender import SmsSendResult
from app.workers.tasks import process_missed_call


@pytest.fixture(autouse=True)
def _clear_tenant_cache() -> Generator[None, None, None]:
    tenant_resolver.clear_cache()
    yield
    tenant_resolver.clear_cache()


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> fakeredis.FakeRedis:
    server = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("app.workers.tasks.redis_client", server)
    return server


@pytest.fixture
def use_test_session(monkeypatch: pytest.MonkeyPatch, db_session: Session) -> Session:
    monkeypatch.setattr("app.workers.tasks.SessionLocal", lambda: db_session)
    # process_missed_call's `finally: db.close()` would otherwise roll back
    # this session's uncommitted SAVEPOINT (Session.close() on pending work
    # implies a rollback) — wiping out whatever the test's own fixtures just
    # flushed. Production code closes a session it exclusively owns; here
    # the session is shared with the test, so close() is a no-op for the
    # duration of the test instead.
    monkeypatch.setattr(Session, "close", lambda self: None)
    return db_session


@pytest.fixture
def mock_send_sms(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock(return_value=SmsSendResult(message_sid="SMtest0001", status="queued"))
    monkeypatch.setattr("app.workers.tasks.send_sms", mock)
    return mock


def _make_tenant(
    db_session: Session,
    *,
    phone_e164: str,
    twilio_sid: str,
    client_status: str = "active",
    daily_sms_limit: int = 100,
    twilio_number_active: bool = True,
) -> tuple[Client, TwilioNumber, AiPrompt]:
    client = Client(
        company_name="Test Co",
        email=f"{uuid.uuid4()}@example.com",
        owner_phone_e164="+48501111111",
        status=client_status,
        daily_sms_limit=daily_sms_limit,
    )
    db_session.add(client)
    db_session.flush()

    number = TwilioNumber(
        client_id=client.id,
        phone_e164=phone_e164,
        twilio_sid=twilio_sid,
        is_active=twilio_number_active,
    )
    db_session.add(number)
    db_session.flush()

    prompt = AiPrompt(
        client_id=client.id,
        system_prompt="You are a helpful assistant.",
        fallback_message="Dziekujemy za telefon, oddzwonimy wkrotce.",
    )
    db_session.add(prompt)
    db_session.flush()

    return client, number, prompt


def _make_event(
    db_session: Session,
    *,
    call_sid: str,
    client: Client | None = None,
    number: TwilioNumber | None = None,
    caller_e164: str | None = "+48501234567",
    forwarded_from: str | None = None,
) -> CallEvent:
    event = CallEvent(
        call_sid=call_sid,
        client_id=client.id if client else None,
        twilio_number_id=number.id if number else None,
        caller_e164=caller_e164,
        forwarded_from=forwarded_from,
    )
    db_session.add(event)
    db_session.flush()
    return event


def _run_task(call_sid: str) -> None:
    process_missed_call.apply(args=(call_sid,))


def test_no_tenant_when_event_never_resolved_a_twilio_number(
    use_test_session: Session, fake_redis: fakeredis.FakeRedis
) -> None:
    event = _make_event(
        use_test_session, call_sid="CAnotenant0000000000000000000001", client=None, number=None
    )

    _run_task(event.call_sid)

    use_test_session.refresh(event)
    assert event.status == "no_tenant"


def test_client_suspended(
    use_test_session: Session, fake_redis: fakeredis.FakeRedis, mock_send_sms: MagicMock
) -> None:
    client, number, _ = _make_tenant(
        use_test_session,
        phone_e164="+48732100001",
        twilio_sid="PNtestsuspended0001",
        client_status="suspended",
    )
    event = _make_event(
        use_test_session, call_sid="CAsuspended000000000000000001", client=client, number=number
    )

    _run_task(event.call_sid)

    use_test_session.refresh(event)
    assert event.status == "client_suspended"
    mock_send_sms.assert_not_called()


def test_anonymous_caller(
    use_test_session: Session, fake_redis: fakeredis.FakeRedis, mock_send_sms: MagicMock
) -> None:
    client, number, _ = _make_tenant(
        use_test_session, phone_e164="+48732100002", twilio_sid="PNtestanon0000001"
    )
    event = _make_event(
        use_test_session,
        call_sid="CAanonymous00000000000000000001",
        client=client,
        number=number,
        caller_e164=None,
    )

    _run_task(event.call_sid)

    use_test_session.refresh(event)
    assert event.status == "anonymous"
    mock_send_sms.assert_not_called()


def test_loop_detected_when_caller_is_owner_phone(
    use_test_session: Session, fake_redis: fakeredis.FakeRedis, mock_send_sms: MagicMock
) -> None:
    client, number, _ = _make_tenant(
        use_test_session, phone_e164="+48732100003", twilio_sid="PNtestloop0000001"
    )
    event = _make_event(
        use_test_session,
        call_sid="CAloop00000000000000000000000001",
        client=client,
        number=number,
        caller_e164=client.owner_phone_e164,
    )

    _run_task(event.call_sid)

    use_test_session.refresh(event)
    assert event.status == "loop_detected"
    mock_send_sms.assert_not_called()


def test_loop_detected_when_caller_is_a_twilio_number(
    use_test_session: Session, fake_redis: fakeredis.FakeRedis, mock_send_sms: MagicMock
) -> None:
    client, number, _ = _make_tenant(
        use_test_session, phone_e164="+48732100004", twilio_sid="PNtestloop0000002"
    )
    other_number = TwilioNumber(
        client_id=client.id, phone_e164="+48732100005", twilio_sid="PNtestloop0000003"
    )
    use_test_session.add(other_number)
    use_test_session.flush()

    event = _make_event(
        use_test_session,
        call_sid="CAloop00000000000000000000000002",
        client=client,
        number=number,
        caller_e164=other_number.phone_e164,
    )

    _run_task(event.call_sid)

    use_test_session.refresh(event)
    assert event.status == "loop_detected"
    mock_send_sms.assert_not_called()


def test_opted_out_caller(
    use_test_session: Session, fake_redis: fakeredis.FakeRedis, mock_send_sms: MagicMock
) -> None:
    from app.models import OptOut

    client, number, _ = _make_tenant(
        use_test_session, phone_e164="+48732100006", twilio_sid="PNtestoptout0001"
    )
    use_test_session.add(OptOut(client_id=client.id, phone_e164="+48501234567", source="sms_stop"))
    use_test_session.flush()

    event = _make_event(
        use_test_session, call_sid="CAoptout0000000000000000000001", client=client, number=number
    )

    _run_task(event.call_sid)

    use_test_session.refresh(event)
    assert event.status == "opted_out"
    mock_send_sms.assert_not_called()


def test_non_mobile_caller(
    use_test_session: Session, fake_redis: fakeredis.FakeRedis, mock_send_sms: MagicMock
) -> None:
    client, number, _ = _make_tenant(
        use_test_session, phone_e164="+48732100007", twilio_sid="PNtestnonmobile01"
    )
    event = _make_event(
        use_test_session,
        call_sid="CAnonmobile00000000000000000001",
        client=client,
        number=number,
        caller_e164="+48221234567",  # landline prefix
    )

    _run_task(event.call_sid)

    use_test_session.refresh(event)
    assert event.status == "non_mobile"
    mock_send_sms.assert_not_called()


def test_deduplicated_within_cooldown_window(
    use_test_session: Session, fake_redis: fakeredis.FakeRedis, mock_send_sms: MagicMock
) -> None:
    client, number, _ = _make_tenant(
        use_test_session, phone_e164="+48732100008", twilio_sid="PNtestdedup0000001"
    )
    fake_redis.set(f"cooldown:{client.id}:+48501234567", "CAsomeoneelse", nx=True, ex=14400)

    event = _make_event(
        use_test_session, call_sid="CAdedup00000000000000000000001", client=client, number=number
    )

    _run_task(event.call_sid)

    use_test_session.refresh(event)
    assert event.status == "deduplicated"
    mock_send_sms.assert_not_called()


def test_limit_exceeded(
    use_test_session: Session, fake_redis: fakeredis.FakeRedis, mock_send_sms: MagicMock
) -> None:
    client, number, _ = _make_tenant(
        use_test_session,
        phone_e164="+48732100009",
        twilio_sid="PNtestlimit0000001",
        daily_sms_limit=0,
    )
    event = _make_event(
        use_test_session, call_sid="CAlimit00000000000000000000001", client=client, number=number
    )

    _run_task(event.call_sid)

    use_test_session.refresh(event)
    assert event.status == "limit_exceeded"
    mock_send_sms.assert_not_called()


def test_happy_path_sends_fallback_sms_and_marks_sms_queued(
    use_test_session: Session, fake_redis: fakeredis.FakeRedis, mock_send_sms: MagicMock
) -> None:
    client, number, prompt = _make_tenant(
        use_test_session, phone_e164="+48732100010", twilio_sid="PNtesthappy0000001"
    )
    event = _make_event(
        use_test_session, call_sid="CAhappy00000000000000000000001", client=client, number=number
    )

    _run_task(event.call_sid)

    use_test_session.refresh(event)
    assert event.status == "sms_queued"
    mock_send_sms.assert_called_once()
    _, kwargs = mock_send_sms.call_args
    assert kwargs["from_e164"] == number.phone_e164
    assert kwargs["to_e164"] == "+48501234567"
    assert "?" not in kwargs["body"]  # transliterated Polish fallback, no lossy chars

    sms = use_test_session.scalar(select(SmsMessage).where(SmsMessage.call_event_id == event.id))
    assert sms is not None
    assert sms.message_sid == "SMtest0001"
    assert sms.is_fallback is True
    assert sms.encoding == "gsm7"
    assert sms.segments == 1
    assert sms.body == prompt.fallback_message  # short enough to need no truncation


def test_sms_send_4xx_marks_sms_failed(
    use_test_session: Session, fake_redis: fakeredis.FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, number, _ = _make_tenant(
        use_test_session, phone_e164="+48732100011", twilio_sid="PNtest4xx00000001"
    )
    event = _make_event(
        use_test_session, call_sid="CA4xx000000000000000000000001", client=client, number=number
    )

    error = TwilioRestException(400, "https://api.twilio.com/x", msg="Invalid number", code=21211)
    monkeypatch.setattr("app.workers.tasks.send_sms", MagicMock(side_effect=error))

    _run_task(event.call_sid)

    use_test_session.refresh(event)
    assert event.status == "sms_failed"

    sms = use_test_session.scalar(select(SmsMessage).where(SmsMessage.call_event_id == event.id))
    assert sms is not None
    assert sms.status == "failed"
    assert sms.error_code == "21211"
    assert sms.message_sid is None


def test_sms_send_5xx_retries_instead_of_marking_a_terminal_status(
    use_test_session: Session, fake_redis: fakeredis.FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, number, _ = _make_tenant(
        use_test_session, phone_e164="+48732100012", twilio_sid="PNtest5xx00000001"
    )
    event = _make_event(
        use_test_session, call_sid="CA5xx000000000000000000000001", client=client, number=number
    )

    error = TwilioRestException(500, "https://api.twilio.com/x", msg="Server error")
    mock_send_sms = MagicMock(side_effect=error)
    monkeypatch.setattr("app.workers.tasks.send_sms", mock_send_sms)

    result = process_missed_call.apply(args=(event.call_sid,))

    # `.apply()` simulates the broker's retry loop locally (celery.app.task
    # .Task.apply re-invokes the task via `retval.sig.apply(...)` on every
    # Retry), so max_retries=3 means 4 real attempts before the original
    # exception surfaces as the final (failed) result — no silent success,
    # no terminal status assigned along the way (spec.md 7.6).
    assert mock_send_sms.call_count == 4
    assert result.state == "FAILURE"
    assert isinstance(result.result, TwilioRestException)

    refreshed = use_test_session.get(CallEvent, event.id)
    assert refreshed is not None
    assert refreshed.status == "received"  # unchanged: no terminal status was assigned

    sms = use_test_session.scalar(select(SmsMessage).where(SmsMessage.call_event_id == event.id))
    assert sms is None


def test_retry_of_same_call_sid_does_not_get_deduplicated_by_its_own_cooldown(
    use_test_session: Session, fake_redis: fakeredis.FakeRedis, mock_send_sms: MagicMock
) -> None:
    client, number, _ = _make_tenant(
        use_test_session, phone_e164="+48732100013", twilio_sid="PNtestretry0000001"
    )
    event = _make_event(
        use_test_session, call_sid="CAretry0000000000000000000001", client=client, number=number
    )

    # First attempt acquires the cooldown key under this call_sid.
    _run_task(event.call_sid)
    use_test_session.refresh(event)
    assert event.status == "sms_queued"

    # A hypothetical Celery retry re-runs the whole task with the same
    # call_sid; the idempotency check (existing sms_messages row) short-
    # circuits it before a second send, and the cooldown key wouldn't have
    # blocked it either way.
    mock_send_sms.reset_mock()
    _run_task(event.call_sid)
    mock_send_sms.assert_not_called()


def test_idempotent_retry_skips_send_when_sms_already_recorded(
    use_test_session: Session, fake_redis: fakeredis.FakeRedis, mock_send_sms: MagicMock
) -> None:
    client, number, _ = _make_tenant(
        use_test_session, phone_e164="+48732100014", twilio_sid="PNtestidem0000001"
    )
    event = _make_event(
        use_test_session, call_sid="CAidem0000000000000000000001", client=client, number=number
    )
    use_test_session.add(
        SmsMessage(
            call_event_id=event.id,
            client_id=client.id,
            message_sid="SMalreadysent0001",
            to_e164="+48501234567",
            body="already sent",
        )
    )
    use_test_session.flush()

    _run_task(event.call_sid)

    mock_send_sms.assert_not_called()
