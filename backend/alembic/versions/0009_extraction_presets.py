"""Managed extraction presets for certificate/document mapping.

Revision ID: 0009_extraction_presets
Revises: 0008_batch_verdict
Create Date: 2026-09-02
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.modules.extraction.builtin_presets import BUILTIN_PRESETS

revision = "0009_extraction_presets"
down_revision = "0008_batch_verdict"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "extraction_presets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("doc_type", sa.Text(), nullable=False, server_default="passport"),
        sa.Column(
            "columns",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "labels",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "section_keywords",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "header_keywords",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "units",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "include_all",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "keyvalue",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("fingerprint", sa.Text(), nullable=True),
        sa.Column(
            "builtin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
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
        sa.UniqueConstraint("code", name="uq_extraction_presets_code"),
    )
    op.create_index(
        "ix_extraction_presets_builtin_name",
        "extraction_presets",
        ["builtin", "name"],
    )

    connection = op.get_bind()
    for preset in BUILTIN_PRESETS:
        connection.execute(
            sa.text(
                "INSERT INTO extraction_presets "
                "(code, name, description, doc_type, columns, labels, "
                "section_keywords, header_keywords, units, include_all, keyvalue, "
                "fingerprint, builtin) "
                "VALUES (:code, :name, :description, :doc_type, "
                "CAST(:columns AS jsonb), CAST(:labels AS jsonb), "
                "CAST(:section_keywords AS jsonb), CAST(:header_keywords AS jsonb), "
                "CAST(:units AS jsonb), :include_all, :keyvalue, "
                ":fingerprint, true)"
            ),
            {
                "code": preset["code"],
                "name": preset["name"],
                "description": preset["description"],
                "doc_type": preset["doc_type"],
                "columns": json.dumps(preset["columns"], ensure_ascii=False),
                "labels": json.dumps(preset["labels"], ensure_ascii=False),
                "section_keywords": json.dumps(
                    preset["section_keywords"], ensure_ascii=False
                ),
                "header_keywords": json.dumps(
                    preset["header_keywords"], ensure_ascii=False
                ),
                "units": json.dumps(preset["units"], ensure_ascii=False),
                "include_all": preset["include_all"],
                "keyvalue": preset["keyvalue"],
                "fingerprint": preset.get("fingerprint"),
            },
        )


def downgrade() -> None:
    op.drop_index("ix_extraction_presets_builtin_name", table_name="extraction_presets")
    op.drop_table("extraction_presets")
