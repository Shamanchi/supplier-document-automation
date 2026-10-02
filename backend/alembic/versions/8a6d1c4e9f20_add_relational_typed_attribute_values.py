"""Add relational typed attribute facts without removing legacy JSON yet.

Revision ID: 8a6d1c4e9f20
Revises: 54d7f4c8b92a
Create Date: 2026-07-13

This is deliberately an additive migration.  Existing ``attributes`` JSON is
backfilled and reconciled by ``app.attribute_backfill`` before a later,
separate contraction migration is allowed to drop that legacy column.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "8a6d1c4e9f20"
down_revision = "54d7f4c8b92a"
branch_labels = None
depends_on = None


_VALUE_KINDS = "'text', 'integer', 'decimal', 'boolean', 'date', 'datetime', 'option'"


def upgrade() -> None:
    op.create_table(
        "attribute_definitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=160), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("value_kind", sa.String(length=16), nullable=False),
        sa.Column("canonical_unit", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("is_multivalued", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"value_kind IN ({_VALUE_KINDS})", name="ck_attribute_definitions_value_kind"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_attribute_definitions_code"),
        sa.UniqueConstraint("id", "value_kind", name="uq_attribute_definitions_id_kind"),
    )
    op.create_table(
        "supplier_attribute_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("supplier_id", sa.Uuid(), nullable=False),
        sa.Column("source_header", sa.Text(), nullable=False),
        sa.Column("normalized_header", sa.Text(), nullable=False),
        sa.Column("attribute_definition_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["attribute_definition_id"], ["attribute_definitions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "supplier_id", "normalized_header", name="uq_supplier_attribute_keys_supplier_header"
        ),
    )
    op.create_index(
        "ix_supplier_attribute_keys_supplier_id", "supplier_attribute_keys", ["supplier_id"], unique=False
    )
    op.create_index(
        "ix_supplier_attribute_keys_attribute_definition_id",
        "supplier_attribute_keys",
        ["attribute_definition_id"],
        unique=False,
    )
    op.create_table(
        "attribute_options",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("attribute_definition_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=160), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["attribute_definition_id"], ["attribute_definitions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "attribute_definition_id", "code", name="uq_attribute_options_definition_code"
        ),
    )
    op.create_index(
        "ix_attribute_options_attribute_definition_id",
        "attribute_options",
        ["attribute_definition_id"],
        unique=False,
    )
    op.create_table(
        "supplier_price_item_attribute_values",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("source_attribute_key_id", sa.Uuid(), nullable=False),
        sa.Column("attribute_definition_id", sa.Uuid(), nullable=False),
        sa.Column("value_kind", sa.String(length=16), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source_column_index", sa.Integer(), nullable=True),
        sa.Column("raw_value", sa.Text(), nullable=False),
        sa.Column("raw_unit", sa.Text(), nullable=True),
        sa.Column("parse_status", sa.String(length=16), nullable=False),
        sa.Column("parse_error", sa.Text(), nullable=True),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_integer", sa.BigInteger(), nullable=True),
        sa.Column("value_decimal", sa.Numeric(precision=20, scale=6), nullable=True),
        sa.Column("value_boolean", sa.Boolean(), nullable=True),
        sa.Column("value_date", sa.Date(), nullable=True),
        sa.Column("value_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("option_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"value_kind IN ({_VALUE_KINDS})",
            name="ck_supplier_price_item_attribute_values_kind",
        ),
        sa.CheckConstraint(
            "parse_status IN ('parsed', 'invalid')",
            name="ck_supplier_price_item_attribute_values_parse_status",
        ),
        sa.CheckConstraint(
            "(parse_status = 'invalid' "
            " AND value_text IS NULL AND value_integer IS NULL AND value_decimal IS NULL"
            " AND value_boolean IS NULL AND value_date IS NULL AND value_timestamp IS NULL"
            " AND option_id IS NULL)"
            " OR (parse_status = 'parsed' AND ("
            " (value_kind = 'text' AND value_text IS NOT NULL"
            "  AND value_integer IS NULL AND value_decimal IS NULL AND value_boolean IS NULL"
            "  AND value_date IS NULL AND value_timestamp IS NULL AND option_id IS NULL)"
            " OR (value_kind = 'integer' AND value_integer IS NOT NULL"
            "  AND value_text IS NULL AND value_decimal IS NULL AND value_boolean IS NULL"
            "  AND value_date IS NULL AND value_timestamp IS NULL AND option_id IS NULL)"
            " OR (value_kind = 'decimal' AND value_decimal IS NOT NULL"
            "  AND value_text IS NULL AND value_integer IS NULL AND value_boolean IS NULL"
            "  AND value_date IS NULL AND value_timestamp IS NULL AND option_id IS NULL)"
            " OR (value_kind = 'boolean' AND value_boolean IS NOT NULL"
            "  AND value_text IS NULL AND value_integer IS NULL AND value_decimal IS NULL"
            "  AND value_date IS NULL AND value_timestamp IS NULL AND option_id IS NULL)"
            " OR (value_kind = 'date' AND value_date IS NOT NULL"
            "  AND value_text IS NULL AND value_integer IS NULL AND value_decimal IS NULL"
            "  AND value_boolean IS NULL AND value_timestamp IS NULL AND option_id IS NULL)"
            " OR (value_kind = 'datetime' AND value_timestamp IS NOT NULL"
            "  AND value_text IS NULL AND value_integer IS NULL AND value_decimal IS NULL"
            "  AND value_boolean IS NULL AND value_date IS NULL AND option_id IS NULL)"
            " OR (value_kind = 'option' AND option_id IS NOT NULL"
            "  AND value_text IS NULL AND value_integer IS NULL AND value_decimal IS NULL"
            "  AND value_boolean IS NULL AND value_date IS NULL AND value_timestamp IS NULL)"
            "))",
            name="ck_supplier_price_item_attribute_values_scalar",
        ),
        sa.ForeignKeyConstraint(["item_id"], ["supplier_price_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_attribute_key_id"], ["supplier_attribute_keys.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["attribute_definition_id", "value_kind"],
            ["attribute_definitions.id", "attribute_definitions.value_kind"],
            name="fk_supplier_price_item_attribute_values_definition_kind",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["option_id"], ["attribute_options.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "item_id",
            "source_attribute_key_id",
            "ordinal",
            name="uq_supplier_price_item_attribute_values_item_source_ordinal",
        ),
    )
    op.create_index(
        "ix_supplier_price_item_attribute_values_item_id",
        "supplier_price_item_attribute_values",
        ["item_id"],
        unique=False,
    )
    op.create_index(
        "ix_supplier_price_item_attribute_values_source_attribute_key_id",
        "supplier_price_item_attribute_values",
        ["source_attribute_key_id"],
        unique=False,
    )
    op.create_index(
        "ix_supplier_price_item_attribute_values_attribute_definition_id",
        "supplier_price_item_attribute_values",
        ["attribute_definition_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_supplier_price_item_attribute_values_attribute_definition_id",
        table_name="supplier_price_item_attribute_values",
    )
    op.drop_index(
        "ix_supplier_price_item_attribute_values_source_attribute_key_id",
        table_name="supplier_price_item_attribute_values",
    )
    op.drop_index(
        "ix_supplier_price_item_attribute_values_item_id",
        table_name="supplier_price_item_attribute_values",
    )
    op.drop_table("supplier_price_item_attribute_values")
    op.drop_index("ix_attribute_options_attribute_definition_id", table_name="attribute_options")
    op.drop_table("attribute_options")
    op.drop_index(
        "ix_supplier_attribute_keys_attribute_definition_id", table_name="supplier_attribute_keys"
    )
    op.drop_index("ix_supplier_attribute_keys_supplier_id", table_name="supplier_attribute_keys")
    op.drop_table("supplier_attribute_keys")
    op.drop_table("attribute_definitions")
