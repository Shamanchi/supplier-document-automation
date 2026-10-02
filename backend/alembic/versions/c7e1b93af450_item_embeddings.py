"""Индекс эмбеддингов позиций прайсов.

Вектор считается по позиции, а не по каноническому товару: индекс собирается
по версиям — новый прайс его расширяет, изменённый пересобирает только свою
часть. Ключ уникальности включает модель, ревизию и версию нормализатора,
чтобы смена эмбеддера добавляла параллельный набор векторов, а не переписывала
исторические строки.

Revision ID: c7e1b93af450
Revises: a1c9e4b70d21
Create Date: 2026-07-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7e1b93af450"
down_revision = "a1c9e4b70d21"
branch_labels = None
depends_on = None

# Совпадает с `app.matching.text.EMBEDDING_DIM`: 768 измерений задаёт локальная
# модель из архитектуры, и та же колонка обслуживает триграммный эмбеддер.
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
        "item_embeddings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "supplier_price_item_id",
            sa.Uuid(),
            sa.ForeignKey("supplier_price_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "price_version_id",
            sa.Uuid(),
            sa.ForeignKey("price_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "supplier_id",
            sa.Uuid(),
            sa.ForeignKey("suppliers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("model_revision", sa.String(length=64), nullable=False),
        sa.Column("normalizer_version", sa.String(length=32), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False, server_default=""),
        _embedding_column(),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "supplier_price_item_id",
            "model_name",
            "model_revision",
            "normalizer_version",
            name="uq_item_embedding_model",
        ),
    )
    op.create_index("ix_item_embeddings_item", "item_embeddings", ["supplier_price_item_id"])
    op.create_index("ix_item_embeddings_version", "item_embeddings", ["price_version_id"])
    op.create_index("ix_item_embeddings_supplier", "item_embeddings", ["supplier_id"])
    op.create_index(
        "ix_item_embeddings_version_model", "item_embeddings", ["price_version_id", "model_name"]
    )
    if bind.dialect.name == "postgresql":
        # HNSW по косинусу: поиск обязан оставаться быстрым, когда в индексе
        # лежат позиции всех активных прайсов.
        op.execute(
            "CREATE INDEX ix_item_embeddings_embedding "
            "ON item_embeddings USING hnsw (embedding vector_cosine_ops)"
        )


def downgrade() -> None:
    op.drop_table("item_embeddings")
