"""Phase 1 AI adapter with circuit breaker and fallback (spec.md 3.4, 7.5).

httpx client with a hard 8s timeout + circuit breaker: ≥5 consecutive errors
in 60s opens the circuit for 2 minutes (all events bypass AI and go straight to
fallback); after 2 minutes, a half-open state allows one trial request. An AI
failure never blocks the SMS — timeout/error → fallback_message, is_fallback=true,
zero retries within a single event (spec.md invariant 4).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

import httpx
import structlog

from app.config import settings

logger = structlog.get_logger()


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class AiResponse:
    text: str


class CircuitBreaker:
    """Simple circuit breaker: closes after 5 consecutive errors within 60s."""

    def __init__(
        self, failure_threshold: int = 5, timeout_seconds: int = 60, open_timeout: int = 120
    ):
        self.failure_threshold = failure_threshold
        self.timeout_seconds = timeout_seconds
        self.open_timeout = open_timeout
        self.failure_count = 0
        self.last_failure_time: float | None = None
        self.open_time: float | None = None

    def is_open(self) -> bool:
        """Returns True if circuit is OPEN or HALF_OPEN."""
        if self.open_time is None:
            return False
        elapsed = time.monotonic() - self.open_time
        if elapsed >= self.open_timeout:
            # Transition to HALF_OPEN: allow one trial request
            self._reset()
            return False
        return True

    def record_success(self) -> None:
        """Reset failure count on success."""
        self.failure_count = 0
        self.last_failure_time = None

    def record_failure(self) -> None:
        """Track failure; open circuit if threshold reached within window."""
        now = time.monotonic()
        if self.last_failure_time is None or (now - self.last_failure_time) > self.timeout_seconds:
            self.failure_count = 1
        else:
            self.failure_count += 1
        self.last_failure_time = now
        if self.failure_count >= self.failure_threshold:
            self.open_time = now
            logger.warning("circuit_breaker_opened", failure_count=self.failure_count)

    def _reset(self) -> None:
        """Reset to CLOSED state."""
        self.failure_count = 0
        self.last_failure_time = None
        self.open_time = None


_client = httpx.Client(timeout=float(settings.AI_TIMEOUT_SECONDS))
_breaker = CircuitBreaker()


class AiError(Exception):
    """Raised on non-transient AI client errors."""

    pass


def generate(system_prompt: str, caller_e164: str, client_id: str) -> AiResponse:
    """Calls Phase 1 AI service with a hard timeout and circuit breaker.

    spec.md 3.4: content generation with 8s timeout; on timeout/error →
    fallback_message, is_fallback=true. No retry within a single event
    (spec.md invariant 4).

    Raises AiError on circuit-breaker open or network failure; the caller
    (tasks.py) catches it and uses fallback_message instead.
    """
    if _breaker.is_open():
        raise AiError("circuit_breaker_open")

    try:
        response = _client.post(
            settings.AI_SERVICE_URL,
            json={
                "system_prompt": system_prompt,
                "caller_e164": caller_e164,
                "client_id": str(client_id),
            },
        )
        response.raise_for_status()
        data = response.json()
        _breaker.record_success()
        return AiResponse(text=data.get("text", ""))
    except (httpx.TimeoutException, httpx.HTTPError, httpx.RequestError) as exc:
        _breaker.record_failure()
        logger.warning(
            "ai_request_failed",
            client_id=str(client_id),
            error_type=type(exc).__name__,
            breaker_failures=_breaker.failure_count,
        )
        raise AiError(str(exc)) from exc
