"""add per-section model rule-generation attempts

Revision ID: f3a7d9e2c4b1
Revises: e8b3c5a7d9f1
Create Date: 2026-07-14 12:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "f3a7d9e2c4b1"
down_revision = "e8b3c5a7d9f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rule_generation_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("price_version_id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.String(length=96), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("prompt_id", sa.String(length=64), nullable=False),
        sa.Column("source_file_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_context_hash", sa.String(length=64), nullable=False),
        sa.Column("input_context", sa.JSON(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("proposal", sa.JSON(), nullable=True),
        sa.Column("validation", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("parse_rule_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["parse_rule_id"], ["parse_rules.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["price_version_id"], ["price_versions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_rule_generation_attempts_price_version_id",
        "rule_generation_attempts",
        ["price_version_id"],
    )
    op.create_index(
        "ix_rule_generation_attempts_batch_id",
        "rule_generation_attempts",
        ["batch_id"],
    )
    op.create_index(
        "ix_rule_generation_attempts_parse_rule_id",
        "rule_generation_attempts",
        ["parse_rule_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_rule_generation_attempts_parse_rule_id", table_name="rule_generation_attempts")
    op.drop_index("ix_rule_generation_attempts_batch_id", table_name="rule_generation_attempts")
    op.drop_index("ix_rule_generation_attempts_price_version_id", table_name="rule_generation_attempts")
    op.drop_table("rule_generation_attempts")
