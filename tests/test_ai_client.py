"""AI client: circuit breaker, timeout, fallback (spec.md 7.5; CLAUDE.md Testing).

Mocks httpx to avoid real API calls.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import httpx
import pytest

from app.services.ai_client import AiError, CircuitBreaker, generate


@pytest.fixture
def breaker() -> CircuitBreaker:
    """Fresh circuit breaker per test."""
    return CircuitBreaker(failure_threshold=2, timeout_seconds=10, open_timeout=5)


def test_circuit_breaker_initially_closed(breaker: CircuitBreaker) -> None:
    assert breaker.is_open() is False


def test_circuit_breaker_opens_after_threshold_failures(breaker: CircuitBreaker) -> None:
    breaker.record_failure()
    assert breaker.is_open() is False
    breaker.record_failure()
    assert breaker.is_open() is True


def test_circuit_breaker_resets_on_success(breaker: CircuitBreaker) -> None:
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open() is True
    breaker.record_success()
    assert breaker.failure_count == 0


def test_circuit_breaker_half_open_allows_trial_request(breaker: CircuitBreaker) -> None:
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open() is True
    # Simulate passage of open_timeout
    breaker.open_time = breaker.open_time - breaker.open_timeout - 1  # type: ignore
    assert breaker.is_open() is False  # Transitioned to HALF_OPEN, now allow
    assert breaker.failure_count == 0  # Reset


def test_generate_success(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_response = MagicMock()
    mock_response.json.return_value = {"text": "Dziekujemy za telefon"}
    mock_response.status_code = 200

    mock_client = MagicMock()
    mock_client.post.return_value = mock_response

    monkeypatch.setattr("app.services.ai_client._client", mock_client)

    response = generate(
        system_prompt="You are helpful.",
        caller_e164="+48501234567",
        client_id=str(uuid.uuid4()),
    )

    assert response.text == "Dziekujemy za telefon"


def test_generate_timeout_raises_ai_error(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = MagicMock()
    mock_client.post.side_effect = httpx.TimeoutException("timeout")
    monkeypatch.setattr("app.services.ai_client._client", mock_client)

    with pytest.raises(AiError):
        generate(
            system_prompt="You are helpful.",
            caller_e164="+48501234567",
            client_id=str(uuid.uuid4()),
        )


def test_generate_circuit_breaker_open_raises_ai_error(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_breaker = MagicMock()
    mock_breaker.is_open.return_value = True
    monkeypatch.setattr("app.services.ai_client._breaker", mock_breaker)

    with pytest.raises(AiError, match="circuit_breaker_open"):
        generate(
            system_prompt="You are helpful.",
            caller_e164="+48501234567",
            client_id=str(uuid.uuid4()),
        )


def test_generate_increments_failure_count_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        message="500", request=MagicMock(), response=MagicMock(status_code=500)
    )

    mock_client = MagicMock()
    mock_client.post.return_value = mock_response
    monkeypatch.setattr("app.services.ai_client._client", mock_client)

    # Set up a fresh breaker for this test
    mock_breaker = CircuitBreaker(failure_threshold=2)
    monkeypatch.setattr("app.services.ai_client._breaker", mock_breaker)

    with pytest.raises(AiError):
        generate(
            system_prompt="You are helpful.",
            caller_e164="+48501234567",
            client_id=str(uuid.uuid4()),
        )

    assert mock_breaker.failure_count == 1
