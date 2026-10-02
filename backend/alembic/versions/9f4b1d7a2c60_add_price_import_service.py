"""Add durable public price-import service.

Revision ID: 9f4b1d7a2c60
Revises: a6c2e7f9b1d4
"""

from alembic import op
import sqlalchemy as sa


revision = "9f4b1d7a2c60"
down_revision = "a6c2e7f9b1d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_clients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("key_prefix", sa.String(length=32), nullable=False),
        sa.Column("key_digest", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("requests_per_minute", sa.Integer(), server_default="60", nullable=False),
        sa.Column("max_concurrent_imports", sa.Integer(), server_default="4", nullable=False),
        sa.Column(
            "max_input_tokens_per_import", sa.BigInteger(), server_default="5000000", nullable=False
        ),
        sa.Column(
            "max_output_tokens_per_import", sa.BigInteger(), server_default="1000000", nullable=False
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_prefix"),
    )
    op.create_index("ix_service_clients_key_prefix", "service_clients", ["key_prefix"])

    op.add_column("suppliers", sa.Column("service_client_id", sa.Uuid(), nullable=True))
    op.add_column("suppliers", sa.Column("external_id", sa.String(length=255), nullable=True))
    op.create_foreign_key(
        "fk_suppliers_service_client",
        "suppliers",
        "service_clients",
        ["service_client_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_suppliers_service_client_id", "suppliers", ["service_client_id"])
    op.create_unique_constraint(
        "uq_suppliers_service_client_external_id",
        "suppliers",
        ["service_client_id", "external_id"],
    )

    op.add_column("supplier_price_items", sa.Column("source_section_id", sa.String(96)))
    op.add_column("supplier_price_items", sa.Column("source_panel_index", sa.Integer()))
    op.add_column("supplier_price_items", sa.Column("source_column_start", sa.Integer()))
    op.add_column("supplier_price_items", sa.Column("source_column_end", sa.Integer()))

    op.alter_column("supplier_price_values", "currency", existing_type=sa.String(8), nullable=True)
    op.add_column(
        "supplier_price_values",
        sa.Column("qualifier", sa.String(16), server_default="exact", nullable=False),
    )
    op.add_column("supplier_price_values", sa.Column("amount_from", sa.Numeric(14, 2)))
    op.add_column("supplier_price_values", sa.Column("amount_to", sa.Numeric(14, 2)))
    op.add_column("supplier_price_values", sa.Column("raw_value", sa.Text()))
    op.add_column(
        "supplier_price_values",
        sa.Column(
            "currency_source", sa.String(24), server_default="legacy_default", nullable=False
        ),
    )
    op.add_column("supplier_price_values", sa.Column("basis_quantity", sa.Numeric(14, 3)))
    op.add_column("supplier_price_values", sa.Column("basis_unit", sa.String(64)))
    op.add_column("supplier_price_values", sa.Column("source_column_index", sa.Integer()))
    op.execute("UPDATE supplier_price_values SET amount_from = amount WHERE amount IS NOT NULL")
    op.create_check_constraint(
        "ck_supplier_price_values_qualifier",
        "supplier_price_values",
        "qualifier IN ('exact','range','from','on_request','unparsed')",
    )
    op.create_check_constraint(
        "ck_supplier_price_values_bounds",
        "supplier_price_values",
        "amount_to IS NULL OR amount_from IS NULL OR amount_to >= amount_from",
    )

    op.create_table(
        "price_imports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("service_client_id", sa.Uuid(), nullable=False),
        sa.Column("supplier_id", sa.Uuid(), nullable=False),
        sa.Column("price_file_id", sa.Uuid(), nullable=False),
        sa.Column("price_version_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), server_default="queued", nullable=False),
        sa.Column("stage", sa.String(32), server_default="queued", nullable=False),
        sa.Column("progress_percent", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempt_no", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rule_batch_id", sa.Uuid(), nullable=True),
        sa.Column("locale", sa.String(32), server_default="ru-RU", nullable=False),
        sa.Column("currency_hint", sa.String(8), nullable=True),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("callback_url", sa.Text(), nullable=True),
        sa.Column("policy", sa.String(16), server_default="strict", nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("completeness_summary", sa.JSON(), nullable=True),
        sa.Column("telemetry_summary", sa.JSON(), nullable=True),
        sa.Column("loader", sa.String(16), nullable=True),
        sa.Column("parser_version", sa.String(32), server_default="v1", nullable=False),
        sa.Column("parse_rule_id", sa.Uuid(), nullable=True),
        sa.Column("parse_rule_revision", sa.Integer(), nullable=True),
        sa.Column("prompt_version", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["service_client_id"], ["service_clients.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["price_file_id"], ["price_files.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["price_version_id"], ["price_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parse_rule_id"], ["parse_rules.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("price_version_id"),
        sa.UniqueConstraint(
            "service_client_id", "idempotency_key", name="uq_price_imports_client_key"
        ),
    )
    op.create_index("ix_price_imports_service_client_id", "price_imports", ["service_client_id"])
    op.create_index("ix_price_imports_supplier_id", "price_imports", ["supplier_id"])
    op.create_index("ix_price_imports_status", "price_imports", ["status"])
    op.create_index("ix_price_imports_rule_batch_id", "price_imports", ["rule_batch_id"])

    op.create_table(
        "price_import_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("price_import_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=True),
        sa.Column("operation_id", sa.String(64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["price_import_id"], ["price_imports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_price_import_events_price_import_id", "price_import_events", ["price_import_id"])

    op.create_table(
        "price_import_webhook_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("price_import_id", sa.Uuid(), nullable=False),
        sa.Column("callback_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), server_default="queued", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["price_import_id"], ["price_imports.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("price_import_id", name="uq_import_webhook_terminal_delivery"),
    )
    op.create_index(
        "ix_price_import_webhook_deliveries_price_import_id",
        "price_import_webhook_deliveries",
        ["price_import_id"],
    )

    op.add_column("rule_generation_attempts", sa.Column("price_import_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_rule_generation_attempts_price_import",
        "rule_generation_attempts",
        "price_imports",
        ["price_import_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_rule_generation_attempts_price_import_id",
        "rule_generation_attempts",
        ["price_import_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_rule_generation_attempts_price_import_id", table_name="rule_generation_attempts")
    op.drop_constraint(
        "fk_rule_generation_attempts_price_import", "rule_generation_attempts", type_="foreignkey"
    )
    op.drop_column("rule_generation_attempts", "price_import_id")
    op.drop_index(
        "ix_price_import_webhook_deliveries_price_import_id",
        table_name="price_import_webhook_deliveries",
    )
    op.drop_table("price_import_webhook_deliveries")
    op.drop_index("ix_price_import_events_price_import_id", table_name="price_import_events")
    op.drop_table("price_import_events")
    op.drop_index("ix_price_imports_rule_batch_id", table_name="price_imports")
    op.drop_index("ix_price_imports_status", table_name="price_imports")
    op.drop_index("ix_price_imports_supplier_id", table_name="price_imports")
    op.drop_index("ix_price_imports_service_client_id", table_name="price_imports")
    op.drop_table("price_imports")

    op.drop_constraint("ck_supplier_price_values_bounds", "supplier_price_values", type_="check")
    op.drop_constraint("ck_supplier_price_values_qualifier", "supplier_price_values", type_="check")
    for column in (
        "source_column_index",
        "basis_unit",
        "basis_quantity",
        "currency_source",
        "raw_value",
        "amount_to",
        "amount_from",
        "qualifier",
    ):
        op.drop_column("supplier_price_values", column)
    op.alter_column("supplier_price_values", "currency", existing_type=sa.String(8), nullable=False)

    for column in (
        "source_column_end",
        "source_column_start",
        "source_panel_index",
        "source_section_id",
    ):
        op.drop_column("supplier_price_items", column)
    op.drop_index("ix_suppliers_service_client_id", table_name="suppliers")
    op.drop_constraint(
        "uq_suppliers_service_client_external_id", "suppliers", type_="unique"
    )
    op.drop_constraint("fk_suppliers_service_client", "suppliers", type_="foreignkey")
    op.drop_column("suppliers", "external_id")
    op.drop_column("suppliers", "service_client_id")
    op.drop_index("ix_service_clients_key_prefix", table_name="service_clients")
    op.drop_table("service_clients")
