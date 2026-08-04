"""Celery Beat scheduled tasks: GDPR anonymization (spec.md 4.3).

Runs nightly (schedule in app.workers.celery_app.celery_app.conf.beat_schedule):
anonymizes call_events / sms_messages older than each client's
`log_retention_days`, replacing the caller's number with a per-client-salted
SHA-256 hash (preserves unique-caller/repeat-rate statistics without storing
the raw number) rather than just stamping a timestamp.
"""

from __future__ import annotations

import structlog
from sqlalchemy import text

from app.db import SessionLocal
from app.workers.celery_app import celery_app

logger = structlog.get_logger()


@celery_app.task  # type: ignore[untyped-decorator]
def anonymize_expired_call_events() -> None:
    """Anonymizes call_events older than clients.log_retention_days (spec.md 4.3).

    Nulls caller_e164 and forwarded_from, replaces caller_hash with
    sha256(caller_e164 || client's anonymization_salt), and stamps
    anonymized_at. Idempotent: only processes rows with anonymized_at IS NULL.
    """
    db = SessionLocal()
    try:
        db.execute(
            text(
                """
                UPDATE call_events
                SET caller_hash = encode(
                        sha256((call_events.caller_e164 || c.anonymization_salt)::bytea), 'hex'
                    ),
                    caller_e164 = NULL,
                    forwarded_from = NULL,
                    anonymized_at = now()
                FROM clients c
                WHERE call_events.client_id = c.id
                  AND call_events.received_at < now() - (c.log_retention_days || ' days')::interval
                  AND call_events.anonymized_at IS NULL
                """
            )
        )
        db.commit()
        logger.info("anonymize_call_events_completed")
    except Exception:
        db.rollback()
        logger.exception("anonymize_call_events_failed")
        raise
    finally:
        db.close()


@celery_app.task  # type: ignore[untyped-decorator]
def anonymize_expired_sms_messages() -> None:
    """Anonymizes sms_messages older than clients.log_retention_days (spec.md
    4.3). Nulls to_e164, replaces body with its character length (preserves
    length/segment statistics without the message content), and stamps
    anonymized_at. Idempotent: only processes rows with anonymized_at IS NULL.
    """
    db = SessionLocal()
    try:
        db.execute(
            text(
                """
                UPDATE sms_messages
                SET body = length(sms_messages.body)::text,
                    to_e164 = NULL,
                    anonymized_at = now()
                FROM clients c
                WHERE sms_messages.client_id = c.id
                  AND sms_messages.created_at < now() - (c.log_retention_days || ' days')::interval
                  AND sms_messages.anonymized_at IS NULL
                """
            )
        )
        db.commit()
        logger.info("anonymize_sms_messages_completed")
    except Exception:
        db.rollback()
        logger.exception("anonymize_sms_messages_failed")
        raise
    finally:
        db.close()
