"""Shared pytest fixtures.

DB-touching tests assume the schema already exists (docker compose up -d db
redis && alembic upgrade head), mirroring the two separate steps in
.github/workflows/ci.yml — this file does not create tables itself. Each
test runs inside a transaction that's rolled back afterward for isolation.
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from twilio.request_validator import RequestValidator

from app.config import settings
from app.db import engine, get_db
from app.main import app


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    connection = engine.connect()
    transaction = connection.begin()
    # create_savepoint: the webhook calls db.commit() as it would in
    # production; this makes that commit release a SAVEPOINT instead of the
    # outer `transaction`, so the rollback below still undoes everything.
    session_factory = sessionmaker(
        bind=connection,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def mock_enqueue(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replaces the Celery enqueue call so tests never touch a real broker."""
    mock = MagicMock()
    monkeypatch.setattr("app.api.webhooks.process_missed_call.delay", mock)
    return mock


def twilio_signature(url: str, form: dict[str, str]) -> str:
    validator = RequestValidator(settings.TWILIO_AUTH_TOKEN)
    return str(validator.compute_signature(url, form))
