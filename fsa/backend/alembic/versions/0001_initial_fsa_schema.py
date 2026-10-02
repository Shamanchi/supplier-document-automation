"""Начальная схема сервиса FSA.

Revision ID: 0001_fsa
Revises:
Create Date: 2026-07-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_fsa"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "registry_certificate",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("registration_number_key", sa.String(length=128), nullable=False),
        sa.Column("registration_number", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("status_raw", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("registered_at", sa.Date()),
        sa.Column("valid_until", sa.Date()),
        sa.Column("suspended_at", sa.Date()),
        sa.Column("suspension_until", sa.Date()),
        sa.Column("terminated_at", sa.Date()),
        sa.Column("resumed_at", sa.Date()),
        sa.Column("extended_at", sa.Date()),
        sa.Column("object_type", sa.String(length=128), server_default=""),
        sa.Column("blank_number", sa.String(length=64), server_default=""),
        sa.Column("annex_blank_number", sa.String(length=64), server_default=""),
        sa.Column("technical_regulations", sa.Text(), server_default=""),
        sa.Column("product_group", sa.Text(), server_default=""),
        sa.Column("applicant_name", sa.Text(), server_default=""),
        sa.Column("applicant_inn", sa.String(length=32), server_default=""),
        sa.Column("manufacturer_name", sa.Text(), server_default=""),
        sa.Column("manufacturer_inn", sa.String(length=32), server_default=""),
        sa.Column("manufacturer_ogrn", sa.String(length=32), server_default=""),
        sa.Column("product_full_name", sa.Text(), server_default=""),
        sa.Column("product_designation", sa.Text(), server_default=""),
        sa.Column("product_type", sa.Text(), server_default=""),
        sa.Column("product_brand", sa.Text(), server_default=""),
        sa.Column("product_model", sa.Text(), server_default=""),
        sa.Column("product_article", sa.Text(), server_default=""),
        sa.Column("okpd2_tnved", sa.Text(), server_default=""),
        sa.Column("product_standards", sa.Text(), server_default=""),
        sa.Column("source_slice", sa.String(length=128), server_default=""),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("registration_number_key", name="uq_registry_certificate_key"),
    )
    op.create_index(
        "ix_registry_certificate_manufacturer", "registry_certificate", ["manufacturer_inn"]
    )
    op.create_index(
        "ix_registry_certificate_valid_until", "registry_certificate", ["valid_until"]
    )
    op.create_index("ix_registry_certificate_status", "registry_certificate", ["status"])

    op.create_table(
        "registry_number",
        sa.Column("registration_number_key", sa.String(length=128), primary_key=True),
    )

    op.create_table(
        "registry_import",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("certificates_loaded", sa.Integer(), server_default="0"),
        sa.Column("numbers_loaded", sa.Integer(), server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.Text(), server_default=""),
    )

    op.create_table(
        "document",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_path", sa.Text(), server_default=""),
        sa.Column("supplier", sa.Text(), server_default=""),
        sa.Column("media_type", sa.String(length=64), server_default=""),
        sa.Column("size_bytes", sa.Integer(), server_default="0"),
        sa.Column("storage_key", sa.Text(), server_default=""),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("doc_class", sa.String(length=32), server_default="other"),
        sa.Column("text_source", sa.String(length=16), server_default="ocr"),
        sa.Column("engine", sa.String(length=32), server_default=""),
        sa.Column("page_count", sa.Integer(), server_default="0"),
        sa.Column("mean_confidence", sa.Float()),
        sa.Column("duration_ms", sa.Integer(), server_default="0"),
        sa.Column("ocr_text", sa.Text(), server_default=""),
        sa.Column("extracted", sa.JSON(), nullable=False),
        sa.Column("registration_number_key", sa.String(length=128), server_default=""),
        sa.Column("error", sa.Text(), server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recognized_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("sha256", name="uq_document_sha256"),
    )
    op.create_index("ix_document_status", "document", ["status"])
    op.create_index("ix_document_number", "document", ["registration_number_key"])
    op.create_index("ix_document_supplier", "document", ["supplier"])

    op.create_table(
        "document_check",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("document.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "certificate_id",
            sa.Uuid(),
            sa.ForeignKey("registry_certificate.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "successor_id",
            sa.Uuid(),
            sa.ForeignKey("registry_certificate.id", ondelete="SET NULL"),
        ),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("matched_by", sa.String(length=32), nullable=False, server_default="none"),
        sa.Column("registration_number_key", sa.String(length=128), server_default=""),
        sa.Column("checked_on", sa.Date(), nullable=False),
        sa.Column("valid_until", sa.Date()),
        sa.Column("needs_manual_review", sa.Boolean(), server_default=sa.true()),
        sa.Column("review_reasons", sa.JSON(), nullable=False),
        sa.Column("successor_score", sa.Float()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("document_id", "checked_on", name="uq_document_check_day"),
    )
    op.create_index("ix_document_check_verdict", "document_check", ["verdict"])


def downgrade() -> None:
    op.drop_table("document_check")
    op.drop_table("document")
    op.drop_table("registry_import")
    op.drop_table("registry_number")
    op.drop_table("registry_certificate")
