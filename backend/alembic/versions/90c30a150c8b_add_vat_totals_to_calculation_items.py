"""add vat totals to calculation items

Revision ID: 90c30a150c8b
Revises: 33dd2e300030
Create Date: 2026-08-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "90c30a150c8b"
down_revision: Union[str, None] = "33dd2e300030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "calculation_items",
        sa.Column("discount_amount", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "calculation_items",
        sa.Column("vat_rate_percent", sa.Numeric(5, 2), nullable=True),
    )
    op.add_column(
        "calculation_items",
        sa.Column("price_without_vat", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "calculation_items",
        sa.Column("vat_amount", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "calculation_items",
        sa.Column("price_with_vat", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "calculation_items",
        sa.Column("line_total_without_vat", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "calculation_items",
        sa.Column("line_vat_amount", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "calculation_items",
        sa.Column("line_total_with_vat", sa.Numeric(14, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("calculation_items", "line_total_with_vat")
    op.drop_column("calculation_items", "line_vat_amount")
    op.drop_column("calculation_items", "line_total_without_vat")
    op.drop_column("calculation_items", "price_with_vat")
    op.drop_column("calculation_items", "vat_amount")
    op.drop_column("calculation_items", "price_without_vat")
    op.drop_column("calculation_items", "vat_rate_percent")
    op.drop_column("calculation_items", "discount_amount")