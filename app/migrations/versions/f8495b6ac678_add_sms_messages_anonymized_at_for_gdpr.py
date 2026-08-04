"""add sms_messages.anonymized_at for GDPR

Revision ID: f8495b6ac678
Revises: ec1b3baa9b4e
Create Date: 2026-08-04 11:30:21.454017

Adds anonymized_at column to sms_messages for GDPR retention/anonymization jobs
(spec.md 4.3, Week 3). Column defaults to NULL; set when log retention expires.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8495b6ac678"
down_revision: str | None = "ec1b3baa9b4e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sms_messages", sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("sms_messages", "anonymized_at")
    # ### end Alembic commands ###
