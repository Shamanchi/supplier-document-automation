"""Record source rows glued into a parsed item by tier-glue.

Revision ID: d9e6c2b1a4f8
Revises: dae6dbe8b3e4
Create Date: 2026-09-04
"""

from alembic import op
import sqlalchemy as sa


revision = "d9e6c2b1a4f8"
down_revision = "dae6dbe8b3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "supplier_price_items", sa.Column("glued_source_rows", sa.JSON(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("supplier_price_items", "glued_source_rows")