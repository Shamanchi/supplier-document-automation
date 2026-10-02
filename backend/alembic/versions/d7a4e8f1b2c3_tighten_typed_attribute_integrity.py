"""Tighten relational attribute integrity after the additive rollout.

Revision ID: d7a4e8f1b2c3
Revises: 8a6d1c4e9f20
Create Date: 2026-07-13

``raw_unit`` was never a source fact: the source header is retained through
``supplier_attribute_keys`` and the reviewed unit through
``attribute_definitions.canonical_unit``.  Removing the unused nullable
column avoids a misleading second unit representation.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d7a4e8f1b2c3"
down_revision = "8a6d1c4e9f20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_attribute_options_id_definition",
        "attribute_options",
        ["id", "attribute_definition_id"],
    )
    op.create_foreign_key(
        "fk_supplier_price_item_attribute_values_option_definition",
        "supplier_price_item_attribute_values",
        "attribute_options",
        ["option_id", "attribute_definition_id"],
        ["id", "attribute_definition_id"],
        ondelete="RESTRICT",
    )
    op.drop_column("supplier_price_item_attribute_values", "raw_unit")


def downgrade() -> None:
    op.add_column(
        "supplier_price_item_attribute_values", sa.Column("raw_unit", sa.Text(), nullable=True)
    )
    op.drop_constraint(
        "fk_supplier_price_item_attribute_values_option_definition",
        "supplier_price_item_attribute_values",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_attribute_options_id_definition", "attribute_options", type_="unique"
    )
