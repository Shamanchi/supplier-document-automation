"""Bind extraction runs to immutable document versions.

Revision ID: 0005_certificate_extraction
Revises: 0004_certificates
Create Date: 2026-08-19
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_certificate_extraction"
down_revision = "0004_certificates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "extraction_runs",
        sa.Column("document_version_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_foreign_key(
        "fk_extraction_run_document_version",
        "extraction_runs",
        "document_versions",
        ["document_version_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.alter_column("extraction_runs", "check_item_id", nullable=True)
    op.add_column(
        "extraction_runs",
        sa.Column("status", sa.Text(), nullable=False, server_default="QUEUED"),
    )
    op.add_column(
        "extraction_runs",
        sa.Column(
            "pipeline_version", sa.Text(), nullable=False, server_default="native-v1"
        ),
    )
    op.add_column("extraction_runs", sa.Column("idempotency_key", sa.Text()))
    op.add_column("extraction_runs", sa.Column("worker_token", sa.Text()))
    op.add_column("extraction_runs", sa.Column("error_code", sa.Text()))
    op.add_column("extraction_runs", sa.Column("error_message", sa.Text()))
    op.add_column(
        "extraction_runs", sa.Column("started_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "extraction_runs", sa.Column("finished_at", sa.DateTime(timezone=True))
    )
    op.create_check_constraint(
        "ck_extraction_run_owner",
        "extraction_runs",
        "document_version_id IS NOT NULL OR check_item_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_extraction_run_status",
        "extraction_runs",
        "status IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')",
    )
    op.create_unique_constraint(
        "uq_extraction_run_idempotency", "extraction_runs", ["idempotency_key"]
    )
    op.create_index(
        "ix_extraction_document_version",
        "extraction_runs",
        ["document_version_id", "created_at"],
    )

    op.alter_column("raw_parameters", "check_item_id", nullable=True)
    op.create_check_constraint(
        "ck_raw_parameter_owner",
        "raw_parameters",
        "check_item_id IS NOT NULL OR extraction_run_id IS NOT NULL",
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM source_verifications sv USING extraction_evidence ee, "
        "raw_parameters rp WHERE sv.extraction_evidence_id = ee.id "
        "AND ee.raw_parameter_id = rp.id AND rp.check_item_id IS NULL"
    )
    op.execute("ALTER TABLE extraction_evidence DISABLE TRIGGER prevent_modify")
    op.execute(
        "DELETE FROM extraction_evidence ee USING raw_parameters rp "
        "WHERE ee.raw_parameter_id = rp.id AND rp.check_item_id IS NULL"
    )
    op.execute("ALTER TABLE extraction_evidence ENABLE TRIGGER prevent_modify")
    op.execute("DELETE FROM raw_parameters WHERE check_item_id IS NULL")
    op.execute("DELETE FROM extraction_runs WHERE check_item_id IS NULL")

    op.drop_constraint("ck_raw_parameter_owner", "raw_parameters", type_="check")
    op.alter_column("raw_parameters", "check_item_id", nullable=False)
    op.drop_index("ix_extraction_document_version", table_name="extraction_runs")
    op.drop_constraint(
        "uq_extraction_run_idempotency", "extraction_runs", type_="unique"
    )
    op.drop_constraint("ck_extraction_run_status", "extraction_runs", type_="check")
    op.drop_constraint("ck_extraction_run_owner", "extraction_runs", type_="check")
    for column in (
        "finished_at",
        "started_at",
        "error_message",
        "error_code",
        "worker_token",
        "idempotency_key",
        "pipeline_version",
        "status",
    ):
        op.drop_column("extraction_runs", column)
    op.alter_column("extraction_runs", "check_item_id", nullable=False)
    op.drop_constraint(
        "fk_extraction_run_document_version", "extraction_runs", type_="foreignkey"
    )
    op.drop_column("extraction_runs", "document_version_id")
