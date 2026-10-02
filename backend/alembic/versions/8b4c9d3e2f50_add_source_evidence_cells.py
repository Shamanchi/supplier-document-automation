"""Store raw source-evidence cells on supplier price items.

Adds the JSON snapshots of the header, data and category rows the parser read
from the original document, so a reviewer can verify a persisted position
against its source without re-opening the file.

Revision ID: 8b4c9d3e2f50
Revises: 7a3f8b2c1d40
"""

from alembic import op
import sqlalchemy as sa


revision = "8b4c9d3e2f50"
down_revision = "7a3f8b2c1d40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "supplier_price_items", sa.Column("source_header_cells", sa.JSON(), nullable=True)
    )
    op.add_column(
        "supplier_price_items", sa.Column("source_data_cells", sa.JSON(), nullable=True)
    )
    op.add_column(
        "supplier_price_items", sa.Column("source_category_cells", sa.JSON(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("supplier_price_items", "source_category_cells")
    op.drop_column("supplier_price_items", "source_data_cells")
    op.drop_column("supplier_price_items", "source_header_cells")
