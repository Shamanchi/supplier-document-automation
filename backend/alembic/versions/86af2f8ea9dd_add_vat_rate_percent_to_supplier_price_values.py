"""add vat rate percent to supplier price values

Revision ID: 86af2f8ea9dd
Revises: c9f2a7e31d04
Create Date: 2026-08-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "86af2f8ea9dd"
down_revision: Union[str, None] = "c9f2a7e31d04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "supplier_price_values",
        sa.Column("vat_rate_percent", sa.Numeric(5, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("supplier_price_values", "vat_rate_percent")
