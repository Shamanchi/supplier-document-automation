"""Add max_quantity to supplier price values.

A quantity price tier (price break) is bounded inclusively on both sides:
``min_quantity`` is the lower bound ("свыше 50 шт." -> 51) and ``max_quantity``
the upper bound ("до 50 шт." -> 50).  Together they let a position store its
full tier ladder and answer "what is the price for N pieces?".

Revision ID: 9c5d0e4f3a60
Revises: 8b4c9d3e2f50
"""

from alembic import op
import sqlalchemy as sa


revision = "9c5d0e4f3a60"
down_revision = "8b4c9d3e2f50"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "supplier_price_values",
        sa.Column("max_quantity", sa.Numeric(14, 3), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("supplier_price_values", "max_quantity")
