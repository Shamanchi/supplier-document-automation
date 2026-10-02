"""add vat rate percent to price imports

Revision ID: 33dd2e300030
Revises: 86af2f8ea9dd
Create Date: 2026-08-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "33dd2e300030"
down_revision: Union[str, None] = "86af2f8ea9dd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "price_imports",
        sa.Column("vat_rate_percent", sa.Numeric(5, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("price_imports", "vat_rate_percent")