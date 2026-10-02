"""discounts and calculations

Revision ID: b6d1f4a29c73
Revises: d4b8c1f60a92
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b6d1f4a29c73"
down_revision = "d4b8c1f60a92"
branch_labels = None
depends_on = None

ORIGINAL_PROJECT_ID = "10000000-0000-4000-8000-000000000001"


def upgrade() -> None:
    op.create_table(
        "discount_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            server_default=ORIGINAL_PROJECT_ID,
        ),
        sa.Column(
            "supplier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("suppliers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column(
            "category_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("global_categories.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("normalized_article", sa.Text(), nullable=True),
        sa.Column("min_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("percent", sa.Numeric(5, 2), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("percent >= 0 AND percent <= 100", name="ck_discount_percent"),
    )
    op.create_index("ix_discount_rules_project_id", "discount_rules", ["project_id"])
    op.create_index("ix_discount_rules_supplier_id", "discount_rules", ["supplier_id"])
    op.create_index("ix_discount_rules_category_id", "discount_rules", ["category_id"])
    op.create_index(
        "ix_discount_rules_normalized_article", "discount_rules", ["normalized_article"]
    )
    op.create_index(
        "ix_discount_rules_supplier_scope", "discount_rules", ["supplier_id", "scope"]
    )

    op.create_table(
        "calculations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            server_default=ORIGINAL_PROJECT_ID,
        ),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("price_requests.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("total_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_calculations_project_id", "calculations", ["project_id"])
    op.create_index("ix_calculations_request_id", "calculations", ["request_id"])

    op.create_table(
        "calculation_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "calculation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("calculations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_line", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "price_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("supplier_price_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "canonical_product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("canonical_products.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "supplier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("suppliers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.Text(), nullable=False, server_default=""),
        sa.Column("quantity", sa.Numeric(14, 3), nullable=False, server_default="1"),
        sa.Column("unit_code", sa.String(32), nullable=True),
        sa.Column("base_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("discount_percent", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column(
            "discount_rule_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("discount_rules.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("discount_source", sa.String(16), nullable=False, server_default="none"),
        sa.Column("price_after_discount", sa.Numeric(14, 2), nullable=True),
        sa.Column("manual_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("manual_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("manual_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("manual_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_calculation_items_calculation_id", "calculation_items", ["calculation_id"])
    op.create_index("ix_calculation_items_supplier_id", "calculation_items", ["supplier_id"])

    op.create_table(
        "price_override_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "calculation_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("calculation_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("previous_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("new_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_price_override_log_item", "price_override_log", ["calculation_item_id"]
    )


def downgrade() -> None:
    op.drop_table("price_override_log")
    op.drop_table("calculation_items")
    op.drop_table("calculations")
    op.drop_table("discount_rules")
