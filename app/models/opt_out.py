from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

OPT_OUT_SOURCES = ("sms_stop", "manual", "twilio_21610")


class OptOut(Base):
    __tablename__ = "opt_outs"
    __table_args__ = (
        CheckConstraint(f"source IN {OPT_OUT_SOURCES}", name="ck_opt_outs_source"),
        UniqueConstraint("client_id", "phone_e164", name="uq_opt_outs_client_phone"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    phone_e164: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False, server_default="sms_stop")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
