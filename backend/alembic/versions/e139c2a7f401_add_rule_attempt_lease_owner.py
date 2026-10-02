"""add rule attempt lease owner

Revision ID: e139c2a7f401
Revises: da139e1f4b20
Create Date: 2026-08-26 09:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "e139c2a7f401"
down_revision = "da139e1f4b20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rule_generation_attempts",
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("rule_generation_attempts", "lease_owner")
