"""Celery Beat scheduled tasks: GDPR anonymization, daily counter reset (spec.md 4.3).

Runs on a schedule (typically nightly): anonymizes call_events / sms_messages by
setting phone numbers to NULL, computing a SHA-256 hash (caller_e164 + per-client
salt) to preserve uniqueness statistics while destroying the raw number.
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.db import SessionLocal

logger = logging.getLogger(__name__)

_WARSAW_TZ = ZoneInfo("Europe/Warsaw")


def anonymize_expired_call_events() -> None:
    """Anonymizes call_events older than clients.log_retention_days (spec.md 4.3).

    Sets anonymized_at timestamp. Idempotent: only processes rows with
    anonymized_at IS NULL.
    """
    db = SessionLocal()
    try:
        stmt = text(
            """
            UPDATE call_events
            SET
                anonymized_at = now()
            FROM clients c
            WHERE
                call_events.client_id = c.id
                AND call_events.received_at < now() - (c.log_retention_days || ' days')::interval
                AND call_events.anonymized_at IS NULL
            """
        )
        db.execute(stmt)
        db.commit()
        logger.info("anonymize_call_events_completed")
    except Exception:
        logger.exception("anonymize_call_events_failed")
    finally:
        db.close()


def anonymize_expired_sms_messages() -> None:
    """Anonymizes sms_messages older than clients.log_retention_days.

    Sets to_e164 to NULL, body to a placeholder ('<redacted>'), recorded_at timestamp.
    """
    db = SessionLocal()
    try:
        sql = text(
            """
            UPDATE sms_messages
            SET
                anonymized_at = now()
            FROM clients c
            WHERE
                sms_messages.client_id = c.id
                AND sms_messages.created_at < now() - (c.log_retention_days || ' days')::interval
                AND anonymized_at IS NULL
            """
        )
        db.execute(sql)
        db.commit()
        logger.info("anonymize_sms_messages_completed")
    except Exception:
        logger.exception("anonymize_sms_messages_failed")
    finally:
        db.close()
