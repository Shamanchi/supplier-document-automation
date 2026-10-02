"""Remove artificial length limits from source-derived item fields.

Revision ID: 54d7f4c8b92a
Revises: 7c9e2a6f4d81
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa


revision = "54d7f4c8b92a"
down_revision = "7c9e2a6f4d81"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "supplier_price_items",
        "source_article",
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        "supplier_price_items",
        "normalized_article",
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        "supplier_price_items",
        "unit_code",
        existing_type=sa.String(length=32),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.alter_column(
        "supplier_price_items",
        "source_sheet",
        existing_type=sa.String(length=255),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "supplier_price_items",
        "source_sheet",
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
    op.alter_column(
        "supplier_price_items",
        "unit_code",
        existing_type=sa.Text(),
        type_=sa.String(length=32),
        existing_nullable=True,
    )
    op.alter_column(
        "supplier_price_items",
        "normalized_article",
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    op.alter_column(
        "supplier_price_items",
        "source_article",
        existing_type=sa.Text(),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
