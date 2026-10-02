"""Persist provider and immutable request settings per LLM attempt.

Revision ID: a6c2e7f9b1d4
Revises: f3a7d9e2c4b1
"""
from alembic import op
import sqlalchemy as sa


revision = "a6c2e7f9b1d4"
down_revision = "f3a7d9e2c4b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "rule_generation_attempts",
        sa.Column("provider", sa.String(length=32), server_default="openai", nullable=False),
    )
    op.add_column(
        "rule_generation_attempts",
        sa.Column("request_settings", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    )
    # Existing Yandex attempts store the fully qualified gpt:// URI, so retain
    # their provider identity across the migration (including queued rows).
    op.execute(
        sa.text(
            "UPDATE rule_generation_attempts SET provider = 'yandexgpt' "
            "WHERE model LIKE 'gpt://%'"
        )
    )
    op.alter_column("rule_generation_attempts", "provider", server_default=None)
    op.alter_column("rule_generation_attempts", "request_settings", server_default=None)


def downgrade() -> None:
    op.drop_column("rule_generation_attempts", "request_settings")
    op.drop_column("rule_generation_attempts", "provider")
