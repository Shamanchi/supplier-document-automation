"""Normalize already stored gram weights to the canonical kilogram unit.

Revision ID: e8b3c5a7d9f1
Revises: c1f6a2e9d7b4
Create Date: 2026-07-13

The typed attribute model uses ``weight`` with canonical unit ``kg``.  The
initial backfill correctly identified headers such as ``Вес, грамм`` as
weight, but must not label their unconverted numeric values as kilograms.
This one-time migration corrects only explicitly gram-labelled source keys.
New imports perform the same conversion in app.attribute_service.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "e8b3c5a7d9f1"
down_revision = "c1f6a2e9d7b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE attribute_definitions "
            "SET display_name = 'Вес', canonical_unit = 'kg' "
            "WHERE code = 'weight'"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE supplier_price_item_attribute_values AS value "
            "SET value_decimal = value.value_decimal / 1000 "
            "FROM supplier_attribute_keys AS source_key, attribute_definitions AS definition "
            "WHERE value.source_attribute_key_id = source_key.id "
            "  AND definition.id = value.attribute_definition_id "
            "  AND value.parse_status = 'parsed' "
            "  AND value.value_kind = 'decimal' "
            "  AND definition.code = 'weight' "
            "  AND definition.canonical_unit = 'kg' "
            "  AND source_key.normalized_header ~ '(^| )(г|гр|грамм|грамма|граммов)( |$)'"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE supplier_price_item_attribute_values AS value "
            "SET value_decimal = value.value_decimal * 1000 "
            "FROM supplier_attribute_keys AS source_key, attribute_definitions AS definition "
            "WHERE value.source_attribute_key_id = source_key.id "
            "  AND definition.id = value.attribute_definition_id "
            "  AND value.parse_status = 'parsed' "
            "  AND value.value_kind = 'decimal' "
            "  AND definition.code = 'weight' "
            "  AND definition.canonical_unit = 'kg' "
            "  AND source_key.normalized_header ~ '(^| )(г|гр|грамм|грамма|граммов)( |$)'"
        )
    )
