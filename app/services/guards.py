"""Guard-chain primitives (spec.md 3.4, 7.2, 7.3, 3.5): loop detection,
opt-out tracking, cooldown/daily-limit anti-spam, and the non-mobile cache
fed by Twilio status-callback error codes. `app/workers/tasks.py` wires
these into the guard order — this module holds no orchestration itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from redis import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import OptOut, TwilioNumber

_WARSAW_TZ = ZoneInfo("Europe/Warsaw")
_NON_MOBILE_CACHE_KEY = "non_mobile_numbers"


def is_loop_caller(
    db: Session,
    *,
    caller_e164: str,
    owner_phone_e164: str,
    forwarded_from_e164: str | None,
) -> bool:
    """spec.md 7.2: the client calling their own number, a carrier echoing
    ForwardedFrom back as From, or a caller that's itself one of our
    Twilio numbers (two MCTB-RAG clients calling each other).
    """
    if caller_e164 == owner_phone_e164:
        return True
    if forwarded_from_e164 is not None and caller_e164 == forwarded_from_e164:
        return True
    return (
        db.scalar(select(TwilioNumber.id).where(TwilioNumber.phone_e164 == caller_e164)) is not None
    )


def is_opted_out(db: Session, *, client_id: uuid.UUID, phone_e164: str) -> bool:
    return (
        db.scalar(
            select(OptOut.id).where(OptOut.client_id == client_id, OptOut.phone_e164 == phone_e164)
        )
        is not None
    )


def record_opt_out(db: Session, *, client_id: uuid.UUID, phone_e164: str, source: str) -> None:
    """Appends to opt_outs (spec.md 3.6, 3.5) — idempotent on the
    (client_id, phone_e164) unique constraint. Commits: called from
    request-scoped webhook handlers with no other pending work.
    """
    stmt = (
        pg_insert(OptOut)
        .values(client_id=client_id, phone_e164=phone_e164, source=source)
        .on_conflict_do_nothing(index_elements=["client_id", "phone_e164"])
    )
    db.execute(stmt)
    db.commit()


def acquire_cooldown(
    redis_client: Redis,
    *,
    client_id: uuid.UUID,
    caller_e164: str,
    call_sid: str,
    ttl: int = 14400,
) -> bool:
    """SET NX (spec.md 7.3): one SMS per (client, caller) pair per `ttl`
    seconds, atomic against two calls landing in the same second.

    The key's value is the call_sid that acquired it, not spec's literal
    `1` — so a Celery retry of the *same* call_sid (e.g. after a Twilio 5xx
    on the send step) passes through instead of being wrongly deduplicated
    against its own earlier, failed attempt (CLAUDE.md invariant 6;
    spec.md 7.6).
    """
    key = f"cooldown:{client_id}:{caller_e164}"
    if redis_client.set(key, call_sid, nx=True, ex=ttl):
        return True
    return bool(redis_client.get(key) == call_sid)


def check_daily_limit(redis_client: Redis, *, client_id: uuid.UUID, daily_limit: int) -> bool:
    """INCR with a TTL to midnight **Europe/Warsaw** (spec.md 7.3;
    CLAUDE.md known pitfall — not UTC). Returns False once the count for
    today exceeds `daily_limit`.
    """
    key = f"daily_limit:{client_id}"
    count = redis_client.incr(key)
    if count == 1:
        redis_client.expire(key, _seconds_until_warsaw_midnight())
    return count <= daily_limit


def mark_non_mobile(redis_client: Redis, phone_e164: str) -> None:
    """Fed by Twilio status-callback error codes 21614/30006 (spec.md
    3.5) — a number that bounced with "can't receive SMS" skips the
    is_mobile_number prefix heuristic on future calls.
    """
    redis_client.sadd(_NON_MOBILE_CACHE_KEY, phone_e164)


def is_known_non_mobile(redis_client: Redis, phone_e164: str) -> bool:
    return bool(redis_client.sismember(_NON_MOBILE_CACHE_KEY, phone_e164))


def _seconds_until_warsaw_midnight() -> int:
    now = datetime.now(_WARSAW_TZ)
    next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((next_midnight - now).total_seconds()))
