"""Add revisioned price-import reviews.

Revision ID: 4f8b2d1c7a90
Revises: 2c7a9e4d1b63
"""

from alembic import op
import sqlalchemy as sa


revision = "4f8b2d1c7a90"
down_revision = "2c7a9e4d1b63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "price_import_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("price_import_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("mapping", sa.JSON(), nullable=True),
        sa.Column("rule_recipe", sa.JSON(), nullable=True),
        sa.Column("rule_name", sa.String(length=512), nullable=True),
        sa.Column("parse_rule_id", sa.Uuid(), nullable=True),
        sa.Column("section_decisions", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("currency_override", sa.String(length=8), nullable=True),
        sa.Column("validation", sa.JSON(), nullable=True),
        sa.Column("last_applied_revision", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("updated_by", sa.String(length=255), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_price_import_reviews_revision"),
        sa.CheckConstraint(
            "last_applied_revision IS NULL OR last_applied_revision <= revision",
            name="ck_price_import_reviews_applied_revision",
        ),
        sa.CheckConstraint(
            "status IN ('draft','validated','applying','applied','superseded')",
            name="ck_price_import_reviews_status",
        ),
        sa.ForeignKeyConstraint(["parse_rule_id"], ["parse_rules.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["price_import_id"], ["price_imports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("price_import_id"),
    )
    op.create_index(
        "ix_price_import_reviews_price_import_id",
        "price_import_reviews",
        ["price_import_id"],
        unique=False,
    )
    op.create_index(
        "ix_price_import_reviews_parse_rule_id",
        "price_import_reviews",
        ["parse_rule_id"],
        unique=False,
    )
    op.create_table(
        "price_import_review_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("price_import_review_id", sa.Uuid(), nullable=False),
        sa.Column("price_import_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["price_import_review_id"], ["price_import_reviews.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["price_import_id"], ["price_imports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_price_import_review_events_price_import_review_id",
        "price_import_review_events",
        ["price_import_review_id"],
        unique=False,
    )
    op.create_index(
        "ix_price_import_review_events_price_import_id",
        "price_import_review_events",
        ["price_import_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_price_import_review_events_price_import_id",
        table_name="price_import_review_events",
    )
    op.drop_index(
        "ix_price_import_review_events_price_import_review_id",
        table_name="price_import_review_events",
    )
    op.drop_table("price_import_review_events")
    op.drop_index("ix_price_import_reviews_parse_rule_id", table_name="price_import_reviews")
    op.drop_index("ix_price_import_reviews_price_import_id", table_name="price_import_reviews")
    op.drop_table("price_import_reviews")
