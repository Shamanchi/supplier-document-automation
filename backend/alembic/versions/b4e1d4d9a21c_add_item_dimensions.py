"""store parsed item dimensions

Revision ID: b4e1d4d9a21c
Revises: e4f2c1b7a9d0
Create Date: 2026-07-13 14:20:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "b4e1d4d9a21c"
down_revision = "e4f2c1b7a9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("supplier_price_items", sa.Column("dimensions", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("supplier_price_items", "dimensions")
