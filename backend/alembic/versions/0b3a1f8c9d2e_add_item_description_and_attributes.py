"""Add source description and variable technical attributes to parsed items.

Revision ID: 0b3a1f8c9d2e
Revises: b4e1d4d9a21c
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa


revision = "0b3a1f8c9d2e"
down_revision = "b4e1d4d9a21c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("supplier_price_items", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("supplier_price_items", sa.Column("attributes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("supplier_price_items", "attributes")
    op.drop_column("supplier_price_items", "description")
