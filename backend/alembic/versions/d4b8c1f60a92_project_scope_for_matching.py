"""Проектная область подбора: проект у источников канона и у эмбеддингов.

Прайсы, позиции и категории в приложении разделены по проектам, а подбор до
сих пор искал по всем сразу. Проект дублируется в производные таблицы, чтобы
фильтр стоил один индекс, а не join к файлу прайса на каждый запрос.

Revision ID: d4b8c1f60a92
Revises: c7e1b93af450
Create Date: 2026-07-31
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4b8c1f60a92"
down_revision = "c7e1b93af450"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("canonical_product_sources", "item_embeddings"):
        op.add_column(
            table,
            sa.Column(
                "project_id",
                sa.Uuid(),
                sa.ForeignKey("projects.id", ondelete="CASCADE"),
                nullable=True,
            ),
        )
        op.create_index(f"ix_{table}_project", table, ["project_id"])

    # Существующие строки получают проект своего прайса: без этого подбор в
    # проекте молча вернул бы пустой результат до первой пересборки.
    op.execute(
        """
        UPDATE canonical_product_sources AS s
        SET project_id = f.project_id
        FROM supplier_price_items i
        JOIN price_versions v ON v.id = i.price_version_id
        JOIN price_files f ON f.id = v.price_file_id
        WHERE s.price_item_id = i.id
        """
    )
    op.execute(
        """
        UPDATE item_embeddings AS e
        SET project_id = f.project_id
        FROM price_versions v
        JOIN price_files f ON f.id = v.price_file_id
        WHERE e.price_version_id = v.id
        """
    )


def downgrade() -> None:
    for table in ("canonical_product_sources", "item_embeddings"):
        op.drop_index(f"ix_{table}_project", table_name=table)
        op.drop_column(table, "project_id")
