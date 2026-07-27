from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TwilioNumber(Base):
    __tablename__ = "twilio_numbers"
    __table_args__ = (
        # Generic E.164 shape, not PL-specific (+48...) — a client's Twilio
        # number pool comes from whichever country their CountryRules names;
        # per-country format is validated in app/core/countries.py, not here.
        CheckConstraint(r"phone_e164 ~ '^\+[1-9][0-9]{6,14}$'", name="ck_twilio_numbers_phone"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False
    )
    phone_e164: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    twilio_sid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
