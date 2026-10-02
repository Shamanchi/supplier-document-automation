"""store review-only prompts for unique price rule generation

Revision ID: e4f2c1b7a9d0
Revises: c28503281c1a
Create Date: 2026-07-12 23:30:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "e4f2c1b7a9d0"
down_revision = "c28503281c1a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("price_versions", sa.Column("rule_generation", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("price_versions", "rule_generation")
