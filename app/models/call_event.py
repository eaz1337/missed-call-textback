from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CallEventStatus(enum.StrEnum):
    """Must match the `call_events.status` CHECK constraint 1:1 (CLAUDE.md
    Code conventions) — adding a status here requires a migration adding it
    to the constraint too.
    """

    RECEIVED = "received"
    NO_TENANT = "no_tenant"
    CLIENT_SUSPENDED = "client_suspended"
    ANONYMOUS = "anonymous"
    INVALID_NUMBER = "invalid_number"
    LOOP_DETECTED = "loop_detected"
    OPTED_OUT = "opted_out"
    NON_MOBILE = "non_mobile"
    DEDUPLICATED = "deduplicated"
    LIMIT_EXCEEDED = "limit_exceeded"
    SMS_QUEUED = "sms_queued"
    SMS_SENT = "sms_sent"
    SMS_FAILED = "sms_failed"


CALL_EVENT_STATUSES = tuple(status.value for status in CallEventStatus)


class CallEvent(Base):
    __tablename__ = "call_events"
    __table_args__ = (
        CheckConstraint(f"status IN {CALL_EVENT_STATUSES}", name="ck_call_events_status"),
        UniqueConstraint("call_sid", name="uq_call_events_call_sid"),
        Index("idx_call_events_client_time", "client_id", text("received_at DESC")),
        Index(
            "idx_call_events_dedup",
            "client_id",
            "caller_e164",
            text("received_at DESC"),
            postgresql_where=text("caller_e164 IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    call_sid: Mapped[str] = mapped_column(String(64), nullable=False)
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=True
    )
    twilio_number_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("twilio_numbers.id"), nullable=True
    )
    caller_e164: Mapped[str | None] = mapped_column(String(16), nullable=True)
    caller_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    forwarded_from: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=CallEventStatus.RECEIVED.value
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    anonymized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
