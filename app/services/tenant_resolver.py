"""To -> client + active prompt resolution (spec.md 2.2), cached 60s.

The webhook already resolves `To` -> twilio_numbers -> client_id at insert
time (app/api/webhooks.py, Week 1), so the worker keys off
`call_events.twilio_number_id` rather than re-parsing a raw To string that
isn't persisted separately. The cache holds a plain snapshot (not the
SQLAlchemy row objects): each Celery task opens and closes its own Session
(app/db.py), so caching detached ORM instances across sessions would be a
foot-gun the moment a relationship or lazy-loaded attribute is added later.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AiPrompt, Client, TwilioNumber

_CACHE_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class Tenant:
    twilio_number_e164: str
    twilio_number_is_active: bool
    client_id: uuid.UUID
    client_status: str
    client_country_code: str
    client_owner_phone_e164: str
    client_daily_sms_limit: int
    system_prompt: str | None
    fallback_message: str | None
    allow_diacritics: bool
    max_sms_segments: int


_cache: dict[uuid.UUID, tuple[Tenant | None, float]] = {}


def resolve_tenant(db: Session, twilio_number_id: uuid.UUID) -> Tenant | None:
    """Returns the (twilio_number, client, active ai_prompt) bundle for a
    call event's twilio_number_id, or None if the number no longer resolves
    (spec.md 3.4 guard 1: no_tenant). Cached for 60s per twilio_number_id —
    a bounded staleness window, accepted per spec.md 2.2's "(60s cache)"
    note, in exchange for not hitting Postgres on every call to the same
    client's number.
    """
    cached = _cache.get(twilio_number_id)
    now = time.monotonic()
    if cached is not None and cached[1] > now:
        return cached[0]

    tenant = _load_tenant(db, twilio_number_id)
    _cache[twilio_number_id] = (tenant, now + _CACHE_TTL_SECONDS)
    return tenant


def clear_cache() -> None:
    """Test-only escape hatch — production code never needs to invalidate
    early within the 60s window.
    """
    _cache.clear()


def _load_tenant(db: Session, twilio_number_id: uuid.UUID) -> Tenant | None:
    number = db.get(TwilioNumber, twilio_number_id)
    if number is None:
        return None
    client = db.get(Client, number.client_id)
    if client is None:
        return None
    prompt = db.scalar(
        select(AiPrompt).where(AiPrompt.client_id == client.id, AiPrompt.is_active.is_(True))
    )
    return Tenant(
        twilio_number_e164=number.phone_e164,
        twilio_number_is_active=number.is_active,
        client_id=client.id,
        client_status=client.status,
        client_country_code=client.country_code,
        client_owner_phone_e164=client.owner_phone_e164,
        client_daily_sms_limit=client.daily_sms_limit,
        system_prompt=prompt.system_prompt if prompt else None,
        fallback_message=prompt.fallback_message if prompt else None,
        allow_diacritics=prompt.allow_diacritics if prompt else False,
        max_sms_segments=prompt.max_sms_segments if prompt else 1,
    )
