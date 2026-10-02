"""add vat mode to calculation items

Revision ID: b857bef6c213
Revises: 90c30a150c8b
Create Date: 2026-08-22
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b857bef6c213"
down_revision: Union[str, None] = "90c30a150c8b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "calculation_items",
        sa.Column("vat_mode", sa.String(16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("calculation_items", "vat_mode")