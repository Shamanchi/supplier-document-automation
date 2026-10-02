"""Add built-in pump matrix extraction preset.

Revision ID: 0010_pump_main_extraction_preset
Revises: 0009_extraction_presets
Create Date: 2026-09-04
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

from app.modules.extraction.builtin_presets import BUILTIN_PRESETS

revision = "0010_pump_main_extraction_preset"
down_revision = "0009_extraction_presets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    preset = next(item for item in BUILTIN_PRESETS if item["code"] == "pump_main")
    op.get_bind().execute(
        sa.text(
            "INSERT INTO extraction_presets "
            "(code, name, description, doc_type, columns, labels, "
            "section_keywords, header_keywords, units, include_all, keyvalue, "
            "fingerprint, builtin) "
            "VALUES (:code, :name, :description, :doc_type, "
            "CAST(:columns AS jsonb), CAST(:labels AS jsonb), "
            "CAST(:section_keywords AS jsonb), CAST(:header_keywords AS jsonb), "
            "CAST(:units AS jsonb), :include_all, :keyvalue, "
            ":fingerprint, true) "
            "ON CONFLICT (code) DO NOTHING"
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
    op.get_bind().execute(
        sa.text(
            "DELETE FROM extraction_presets "
            "WHERE code = 'pump_main' AND builtin IS TRUE"
        )
    )
