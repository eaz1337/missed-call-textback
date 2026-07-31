"""Guard-chain primitives: loop detection, opt-out, cooldown, daily limit,
non-mobile cache (spec.md 3.4, 7.2, 7.3, 3.5; CLAUDE.md Testing section).

Loop/opt-out need the real Postgres schema (db_session fixture, per
tests/conftest.py); cooldown/daily-limit/non-mobile are pure Redis
operations, tested against fakeredis instead of the real broker.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator

import fakeredis
import pytest
from sqlalchemy.orm import Session

from app.models import Client, TwilioNumber
from app.services.guards import (
    acquire_cooldown,
    check_daily_limit,
    is_known_non_mobile,
    is_loop_caller,
    is_opted_out,
    mark_non_mobile,
    record_opt_out,
)


@pytest.fixture
def redis() -> Generator[fakeredis.FakeRedis, None, None]:
    server = fakeredis.FakeRedis(decode_responses=True)
    try:
        yield server
    finally:
        server.close()


def _make_client(db_session: Session) -> Client:
    client = Client(
        company_name="Test Co",
        email=f"{uuid.uuid4()}@example.com",
        owner_phone_e164="+48501111111",
    )
    db_session.add(client)
    db_session.flush()
    return client


def test_is_loop_caller_true_for_owner_phone(db_session: Session) -> None:
    client = _make_client(db_session)
    assert (
        is_loop_caller(
            db_session,
            caller_e164=client.owner_phone_e164,
            owner_phone_e164=client.owner_phone_e164,
            forwarded_from_e164=None,
        )
        is True
    )


def test_is_loop_caller_true_for_forwarded_from_match(db_session: Session) -> None:
    client = _make_client(db_session)
    assert (
        is_loop_caller(
            db_session,
            caller_e164="+48509999999",
            owner_phone_e164=client.owner_phone_e164,
            forwarded_from_e164="+48509999999",
        )
        is True
    )


def test_is_loop_caller_true_for_known_twilio_number(db_session: Session) -> None:
    client = _make_client(db_session)
    number = TwilioNumber(
        client_id=client.id, phone_e164="+48732000222", twilio_sid="PNtestloop00000001"
    )
    db_session.add(number)
    db_session.flush()

    assert (
        is_loop_caller(
            db_session,
            caller_e164="+48732000222",
            owner_phone_e164=client.owner_phone_e164,
            forwarded_from_e164=None,
        )
        is True
    )


def test_is_loop_caller_false_for_ordinary_caller(db_session: Session) -> None:
    client = _make_client(db_session)
    assert (
        is_loop_caller(
            db_session,
            caller_e164="+48501234567",
            owner_phone_e164=client.owner_phone_e164,
            forwarded_from_e164=None,
        )
        is False
    )


def test_opt_out_round_trip(db_session: Session) -> None:
    client = _make_client(db_session)
    assert is_opted_out(db_session, client_id=client.id, phone_e164="+48501234567") is False

    record_opt_out(db_session, client_id=client.id, phone_e164="+48501234567", source="sms_stop")

    assert is_opted_out(db_session, client_id=client.id, phone_e164="+48501234567") is True


def test_record_opt_out_is_idempotent_on_conflict(db_session: Session) -> None:
    client = _make_client(db_session)
    record_opt_out(db_session, client_id=client.id, phone_e164="+48501234567", source="sms_stop")
    record_opt_out(db_session, client_id=client.id, phone_e164="+48501234567", source="manual")

    assert is_opted_out(db_session, client_id=client.id, phone_e164="+48501234567") is True


def test_acquire_cooldown_blocks_second_call_within_window(
    redis: fakeredis.FakeRedis,
) -> None:
    client_id = uuid.uuid4()
    assert (
        acquire_cooldown(redis, client_id=client_id, caller_e164="+48501234567", call_sid="CA1")
        is True
    )
    assert (
        acquire_cooldown(redis, client_id=client_id, caller_e164="+48501234567", call_sid="CA2")
        is False
    )


def test_acquire_cooldown_allows_retry_of_same_call_sid(redis: fakeredis.FakeRedis) -> None:
    client_id = uuid.uuid4()
    assert (
        acquire_cooldown(redis, client_id=client_id, caller_e164="+48501234567", call_sid="CA1")
        is True
    )
    assert (
        acquire_cooldown(redis, client_id=client_id, caller_e164="+48501234567", call_sid="CA1")
        is True
    )


def test_acquire_cooldown_different_callers_are_independent(
    redis: fakeredis.FakeRedis,
) -> None:
    client_id = uuid.uuid4()
    assert (
        acquire_cooldown(redis, client_id=client_id, caller_e164="+48501234567", call_sid="CA1")
        is True
    )
    assert (
        acquire_cooldown(redis, client_id=client_id, caller_e164="+48509999999", call_sid="CA2")
        is True
    )


def test_check_daily_limit_respects_limit(redis: fakeredis.FakeRedis) -> None:
    client_id = uuid.uuid4()
    assert check_daily_limit(redis, client_id=client_id, daily_limit=2) is True
    assert check_daily_limit(redis, client_id=client_id, daily_limit=2) is True
    assert check_daily_limit(redis, client_id=client_id, daily_limit=2) is False


def test_check_daily_limit_sets_a_ttl(redis: fakeredis.FakeRedis) -> None:
    client_id = uuid.uuid4()
    check_daily_limit(redis, client_id=client_id, daily_limit=100)
    ttl = redis.ttl(f"daily_limit:{client_id}")
    assert 0 < ttl <= 86400


def test_non_mobile_cache_round_trip(redis: fakeredis.FakeRedis) -> None:
    assert is_known_non_mobile(redis, "+48221234567") is False
    mark_non_mobile(redis, "+48221234567")
    assert is_known_non_mobile(redis, "+48221234567") is True
