"""Add manual_category_id to supplier price items.

Positions from "flat" price lists (no category sections in the source document)
have no supplier-derived category.  ``manual_category_id`` lets an operator — or
the auto-classifier — assign a global category directly; when set it wins over
the supplier-derived category.

Revision ID: a1b2c3d4e5f6
Revises: 9c5d0e4f3a60
"""

from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "9c5d0e4f3a60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "supplier_price_items",
        sa.Column("manual_category_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_supplier_price_items_manual_category_id",
        "supplier_price_items",
        "global_categories",
        ["manual_category_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_supplier_price_items_manual_category_id",
        "supplier_price_items",
        ["manual_category_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_supplier_price_items_manual_category_id", table_name="supplier_price_items")
    op.drop_constraint("fk_supplier_price_items_manual_category_id", "supplier_price_items", type_="foreignkey")
    op.drop_column("supplier_price_items", "manual_category_id")
