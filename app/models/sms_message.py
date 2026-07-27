from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

SMS_ENCODINGS = ("gsm7", "ucs2")
SMS_STATUSES = ("queued", "sent", "delivered", "undelivered", "failed")


class SmsMessage(Base):
    __tablename__ = "sms_messages"
    __table_args__ = (
        CheckConstraint(f"encoding IN {SMS_ENCODINGS}", name="ck_sms_messages_encoding"),
        CheckConstraint(f"status IN {SMS_STATUSES}", name="ck_sms_messages_status"),
        Index("idx_sms_messages_client_time", "client_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    call_event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("call_events.id"), nullable=False
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), nullable=False
    )
    message_sid: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    to_e164: Mapped[str | None] = mapped_column(String(16), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    encoding: Mapped[str] = mapped_column(String, nullable=False, server_default="gsm7")
    segments: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    is_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="queued")
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    price_usd: Mapped[float | None] = mapped_column(Numeric(8, 5), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
