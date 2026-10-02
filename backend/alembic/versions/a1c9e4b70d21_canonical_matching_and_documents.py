"""Канонический слой, словарь синонимов, документы и заявки.

Сертификат выдан на изделие производителя, а не на строку прайса дилера,
поэтому документы привязываются к каноническому товару. Вектор наименования
хранится в pgvector: подбор ищет по нему семантически, а расширение включается
здесь же — база уже собрана на образе `pgvector/pgvector`.

Revision ID: a1c9e4b70d21
Revises: b2d5f8a3c701
Create Date: 2026-07-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a1c9e4b70d21"
down_revision = "b2d5f8a3c701"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 768


def _embedding_column() -> sa.Column:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        from pgvector.sqlalchemy import Vector

        return sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True)
    return sa.Column("embedding", sa.JSON(), nullable=True)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "canonical_products",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("match_key", sa.String(length=256), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("normalized_title", sa.Text(), nullable=False),
        sa.Column("normalized_article", sa.Text(), nullable=True),
        sa.Column(
            "category_id",
            sa.Uuid(),
            sa.ForeignKey("global_categories.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("merge_status", sa.String(length=30), nullable=False, server_default="auto"),
        sa.Column("sources_count", sa.Integer(), nullable=False, server_default="0"),
        _embedding_column(),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("match_key", name="uq_canonical_products_key"),
    )
    op.create_index("ix_canonical_products_article", "canonical_products", ["normalized_article"])
    op.create_index("ix_canonical_products_normalized_title", "canonical_products", ["normalized_title"])
    op.create_index("ix_canonical_products_category", "canonical_products", ["category_id"])
    if bind.dialect.name == "postgresql":
        # HNSW по косинусу: поиск похожего наименования обязан оставаться
        # быстрым на сотнях тысяч канонических товаров.
        op.execute(
            "CREATE INDEX ix_canonical_products_embedding "
            "ON canonical_products USING hnsw (embedding vector_cosine_ops)"
        )

    op.create_table(
        "canonical_product_sources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("canonical_product_id", sa.Uuid(),
                  sa.ForeignKey("canonical_products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("price_item_id", sa.Uuid(),
                  sa.ForeignKey("supplier_price_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("supplier_id", sa.Uuid(),
                  sa.ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("price_item_id", name="uq_canonical_source_item"),
    )
    op.create_index("ix_canonical_sources_canonical", "canonical_product_sources", ["canonical_product_id"])
    op.create_index("ix_canonical_sources_supplier", "canonical_product_sources", ["supplier_id"])

    op.create_table(
        "synonym_dictionary",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_phrase", sa.Text(), nullable=False),
        sa.Column("normalized_phrase", sa.Text(), nullable=False),
        sa.Column("canonical_product_id", sa.Uuid(),
                  sa.ForeignKey("canonical_products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("origin", sa.String(length=30), nullable=False, server_default="manager_choice"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
        sa.Column("usage_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("normalized_phrase", name="uq_synonym_phrase"),
    )
    op.create_index("ix_synonym_canonical", "synonym_dictionary", ["canonical_product_id"])
    op.create_index("ix_synonym_phrase", "synonym_dictionary", ["normalized_phrase"])

    op.create_table(
        "product_documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="certificate"),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("registration_number", sa.Text(), nullable=True),
        sa.Column("registration_number_key", sa.String(length=128), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=False, server_default="fsa_service"),
        sa.Column("external_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_product_documents_number", "product_documents", ["registration_number"])
    op.create_index("ix_product_documents_number_key", "product_documents", ["registration_number_key"])
    op.create_index("ix_product_documents_sha256", "product_documents", ["sha256"])

    op.create_table(
        "document_links",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(),
                  sa.ForeignKey("product_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canonical_product_id", sa.Uuid(),
                  sa.ForeignKey("canonical_products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("matched_by", sa.String(length=30), nullable=False, server_default="registry"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("series_mask", sa.String(length=200), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("confirmed_by", sa.Text(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("document_id", "canonical_product_id", name="uq_document_link_pair"),
    )
    op.create_index("ix_document_links_canonical", "document_links", ["canonical_product_id"])
    op.create_index("ix_document_links_document", "document_links", ["document_id"])

    op.create_table(
        "price_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=30), nullable=False, server_default="manual"),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )

    op.create_table(
        "price_request_lines",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("request_id", sa.Uuid(),
                  sa.ForeignKey("price_requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_line", sa.Text(), nullable=False),
        sa.Column("query_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("quantity", sa.Numeric(14, 3), nullable=True),
        sa.Column("unit_code", sa.String(length=32), nullable=True),
        sa.Column("matched_canonical_id", sa.Uuid(),
                  sa.ForeignKey("canonical_products.id", ondelete="SET NULL"), nullable=True),
        sa.Column("match_stage", sa.String(length=20), nullable=False, server_default="none"),
        sa.Column("match_score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_request_lines_request", "price_request_lines", ["request_id"])


def downgrade() -> None:
    op.drop_table("price_request_lines")
    op.drop_table("price_requests")
    op.drop_table("document_links")
    op.drop_table("product_documents")
    op.drop_table("synonym_dictionary")
    op.drop_table("canonical_product_sources")
    op.drop_table("canonical_products")
