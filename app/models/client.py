from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import CITEXT, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.countries import DEFAULT_COUNTRY
from app.models.base import Base

CLIENT_STATUSES = ("trial", "active", "suspended", "cancelled")


class Client(Base):
    __tablename__ = "clients"
    __table_args__ = (
        CheckConstraint(f"status IN {CLIENT_STATUSES}", name="ck_clients_status"),
        CheckConstraint(
            r"owner_phone_e164 ~ '^\+[1-9][0-9]{6,14}$'", name="ck_clients_owner_phone"
        ),
        CheckConstraint("country_code ~ '^[A-Z]{2}$'", name="ck_clients_country_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    company_name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    owner_phone_e164: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="trial")
    # Selects the CountryRules (app/core/countries.py) used for this client's
    # mobile-prefix heuristic, SMS transliteration, and opt-out keywords.
    # Only "PL" is populated today; a new market is a new registry entry,
    # not a schema change.
    country_code: Mapped[str] = mapped_column(
        String(2), nullable=False, server_default=DEFAULT_COUNTRY
    )
    daily_sms_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    log_retention_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default="90")
    anonymization_salt: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("encode(gen_random_bytes(16), 'hex')")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
