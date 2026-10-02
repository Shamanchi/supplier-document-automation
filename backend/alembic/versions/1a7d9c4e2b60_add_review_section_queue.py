"""Add durable price-import review section queue.

Revision ID: 1a7d9c4e2b60
Revises: 6b7c9d2e4f10
"""

from alembic import op
import sqlalchemy as sa


revision = "1a7d9c4e2b60"
down_revision = "6b7c9d2e4f10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "price_import_review_sections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("price_import_review_id", sa.Uuid(), nullable=False),
        sa.Column("price_import_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.String(length=96), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("source_context_hash", sa.String(length=64), nullable=False),
        sa.Column("sheet_name", sa.String(length=255), nullable=False),
        sa.Column("section_kind", sa.String(length=64), nullable=False),
        sa.Column("start_row_index", sa.Integer(), nullable=True),
        sa.Column("end_row_index", sa.Integer(), nullable=True),
        sa.Column("attempt_status", sa.String(length=32), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("decision_summary", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("updated_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','in_progress','validated','accepted','rejected')",
            name="ck_price_import_review_sections_status",
        ),
        sa.ForeignKeyConstraint(
            ["price_import_review_id"], ["price_import_reviews.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["price_import_id"], ["price_imports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "price_import_review_id", "section_id", name="uq_price_import_review_sections_identity"
        ),
    )
    op.create_index(
        "ix_price_import_review_sections_review_id",
        "price_import_review_sections",
        ["price_import_review_id"],
    )
    op.create_index(
        "ix_price_import_review_sections_import_id",
        "price_import_review_sections",
        ["price_import_id"],
    )
    op.create_index(
        "ix_price_import_review_sections_status",
        "price_import_review_sections",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_price_import_review_sections_status", table_name="price_import_review_sections")
    op.drop_index("ix_price_import_review_sections_import_id", table_name="price_import_review_sections")
    op.drop_index("ix_price_import_review_sections_review_id", table_name="price_import_review_sections")
    op.drop_table("price_import_review_sections")
