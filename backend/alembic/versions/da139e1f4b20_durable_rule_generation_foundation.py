"""add durable rule generation foundation

Revision ID: da139e1f4b20
Revises: c9f2a7e31d04
Create Date: 2026-08-25 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "da139e1f4b20"
down_revision = "c9f2a7e31d04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rule_generation_artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("work_key", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("request_settings_hash", sa.String(length=64), nullable=False),
        sa.Column("source_file_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_context_hash", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("proposal", sa.JSON(), nullable=False),
        sa.Column("validation", sa.JSON(), nullable=False),
        sa.Column("compiler_dry_run_summary", sa.JSON(), nullable=False),
        sa.Column("provenance", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "work_key", name="uq_rule_generation_artifacts_work_key"
        ),
    )

    op.add_column(
        "rule_generation_attempts",
        sa.Column("work_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "rule_generation_attempts",
        sa.Column("artifact_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "rule_generation_attempts",
        sa.Column("failure_class", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "rule_generation_attempts",
        sa.Column(
            "retry_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "rule_generation_attempts",
        sa.Column(
            "semantic_retry_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "rule_generation_attempts",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "rule_generation_attempts",
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "rule_generation_attempts",
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "rule_generation_attempts",
        sa.Column("reused_from_attempt_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "rule_generation_attempts",
        sa.Column("terminal_reason", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_rule_generation_attempts_artifact_id",
        "rule_generation_attempts",
        "rule_generation_artifacts",
        ["artifact_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_rule_generation_attempts_reused_from_attempt_id",
        "rule_generation_attempts",
        "rule_generation_attempts",
        ["reused_from_attempt_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_rule_generation_attempts_work_key",
        "rule_generation_attempts",
        ["work_key"],
    )
    op.create_index(
        "ix_rule_generation_attempts_next_retry_at",
        "rule_generation_attempts",
        ["next_retry_at"],
    )
    op.create_index(
        "ix_rule_generation_attempts_lease_until",
        "rule_generation_attempts",
        ["lease_until"],
    )

    op.create_table(
        "rule_generation_operations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("operation_no", sa.Integer(), nullable=False),
        sa.Column("operation_kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("provider_operation_id", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("http_attempts", sa.Integer(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("error_type", sa.String(length=64), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["rule_generation_attempts.id"],
            name="fk_rule_generation_operations_attempt_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "attempt_id",
            "operation_no",
            name="uq_rule_generation_operations_attempt_operation",
        ),
    )
    op.create_index(
        "ix_rule_generation_operations_attempt_id",
        "rule_generation_operations",
        ["attempt_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_rule_generation_operations_attempt_id",
        table_name="rule_generation_operations",
    )
    op.drop_table("rule_generation_operations")

    op.drop_index(
        "ix_rule_generation_attempts_lease_until",
        table_name="rule_generation_attempts",
    )
    op.drop_index(
        "ix_rule_generation_attempts_next_retry_at",
        table_name="rule_generation_attempts",
    )
    op.drop_index(
        "ix_rule_generation_attempts_work_key",
        table_name="rule_generation_attempts",
    )
    op.drop_constraint(
        "fk_rule_generation_attempts_reused_from_attempt_id",
        "rule_generation_attempts",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_rule_generation_attempts_artifact_id",
        "rule_generation_attempts",
        type_="foreignkey",
    )
    op.drop_column("rule_generation_attempts", "terminal_reason")
    op.drop_column("rule_generation_attempts", "reused_from_attempt_id")
    op.drop_column("rule_generation_attempts", "last_heartbeat_at")
    op.drop_column("rule_generation_attempts", "lease_until")
    op.drop_column("rule_generation_attempts", "next_retry_at")
    op.drop_column("rule_generation_attempts", "semantic_retry_count")
    op.drop_column("rule_generation_attempts", "retry_count")
    op.drop_column("rule_generation_attempts", "failure_class")
    op.drop_column("rule_generation_attempts", "artifact_id")
    op.drop_column("rule_generation_attempts", "work_key")

    op.drop_table("rule_generation_artifacts")
