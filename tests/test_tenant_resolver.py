"""tenant_resolver: To -> client + active prompt resolution, 60s cache
(spec.md 2.2).
"""

from __future__ import annotations

import uuid
from collections.abc import Generator

import pytest
from sqlalchemy.orm import Session

from app.models import AiPrompt, Client, TwilioNumber
from app.services import tenant_resolver


@pytest.fixture(autouse=True)
def _clear_tenant_cache() -> Generator[None, None, None]:
    tenant_resolver.clear_cache()
    yield
    tenant_resolver.clear_cache()


def _make_tenant_rows(db_session: Session) -> tuple[Client, TwilioNumber, AiPrompt]:
    client = Client(
        company_name="Test Co",
        email=f"{uuid.uuid4()}@example.com",
        owner_phone_e164="+48501111111",
        status="active",
    )
    db_session.add(client)
    db_session.flush()

    number = TwilioNumber(
        client_id=client.id, phone_e164="+48732000333", twilio_sid="PNtestresolve0001"
    )
    db_session.add(number)
    db_session.flush()

    prompt = AiPrompt(
        client_id=client.id,
        system_prompt="You are a helpful assistant.",
        fallback_message="Dziekujemy za telefon.",
    )
    db_session.add(prompt)
    db_session.flush()

    return client, number, prompt


def test_resolve_tenant_returns_bundle(db_session: Session) -> None:
    client, number, prompt = _make_tenant_rows(db_session)

    tenant = tenant_resolver.resolve_tenant(db_session, number.id)

    assert tenant is not None
    assert tenant.client_id == client.id
    assert tenant.twilio_number_e164 == number.phone_e164
    assert tenant.twilio_number_is_active is True
    assert tenant.client_status == "active"
    assert tenant.fallback_message == prompt.fallback_message
    assert tenant.allow_diacritics is False
    assert tenant.max_sms_segments == 1


def test_resolve_tenant_returns_none_for_unknown_number(db_session: Session) -> None:
    assert tenant_resolver.resolve_tenant(db_session, uuid.uuid4()) is None


def test_resolve_tenant_fallback_message_none_without_active_prompt(
    db_session: Session,
) -> None:
    client = Client(
        company_name="No Prompt Co",
        email=f"{uuid.uuid4()}@example.com",
        owner_phone_e164="+48502222222",
    )
    db_session.add(client)
    db_session.flush()
    number = TwilioNumber(
        client_id=client.id, phone_e164="+48732000444", twilio_sid="PNtestnoprompt001"
    )
    db_session.add(number)
    db_session.flush()

    tenant = tenant_resolver.resolve_tenant(db_session, number.id)

    assert tenant is not None
    assert tenant.fallback_message is None
    assert tenant.allow_diacritics is False
    assert tenant.max_sms_segments == 1


def test_resolve_tenant_caches_within_ttl(db_session: Session) -> None:
    client, number, _ = _make_tenant_rows(db_session)

    first = tenant_resolver.resolve_tenant(db_session, number.id)
    assert first is not None
    assert first.client_status == "active"

    client.status = "suspended"
    db_session.flush()

    cached = tenant_resolver.resolve_tenant(db_session, number.id)
    assert cached is not None
    assert cached.client_status == "active"  # still cached — hasn't picked up the mutation

    tenant_resolver.clear_cache()
    fresh = tenant_resolver.resolve_tenant(db_session, number.id)
    assert fresh is not None
    assert fresh.client_status == "suspended"
