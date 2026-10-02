# -*- coding: utf-8 -*-
"""Снимок источника цены в строке расчёта (Ф2 адаптации).

Строка расчёта должна отвечать на вопрос «откуда эта цена» и после того, как
общий прайс обновлён, а разовый счёт заявки закрыт. Поэтому в
`calculation_items` фиксируется снимок источника:

* `price_version_id` — конкретная версия прайса;
* `price_document_id` — ценовой документ (`price_files.id`);
* `price_document_type` — `PRICE_LIST` (общий прайс) или `SUPPLIER_QUOTE`
  (разовый счёт/КП);
* `price_source` — откуда пришёл файл (`UPLOAD`/`BITRIX`/`API`);
* `price_kind` — вид цены (дилерская, оптовая, розничная…);
* `price_tier_min_quantity`/`price_tier_max_quantity` — ступень количества,
  по которой выбрана цена.

Поля nullable: старые строки расчётов остаются валидными, а цена могла быть
проставлена вручную.

Revision ID: 0014_calculation_price_snapshot
Revises: 0013_bitrix_price_documents
Create Date: 2026-09-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_calculation_price_snapshot"
down_revision = "0013_bitrix_price_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "calculation_items",
        sa.Column("price_version_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "calculation_items",
        sa.Column("price_document_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "calculation_items",
        sa.Column("price_document_type", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "calculation_items",
        sa.Column("price_source", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "calculation_items",
        sa.Column("price_kind", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "calculation_items",
        sa.Column("price_tier_min_quantity", sa.Numeric(14, 3), nullable=True),
    )
    op.add_column(
        "calculation_items",
        sa.Column("price_tier_max_quantity", sa.Numeric(14, 3), nullable=True),
    )
    op.create_foreign_key(
        "fk_calculation_items_price_version_id",
        "calculation_items",
        "price_versions",
        ["price_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_calculation_items_price_document_id",
        "calculation_items",
        "price_files",
        ["price_document_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_calculation_items_price_version_id",
        "calculation_items",
        ["price_version_id"],
    )
    op.create_check_constraint(
        "ck_calculation_items_price_document_type",
        "calculation_items",
        "price_document_type IS NULL"
        " OR price_document_type IN ('PRICE_LIST', 'SUPPLIER_QUOTE')",
    )
    op.execute(
        "COMMENT ON COLUMN calculation_items.price_document_type IS"
        " 'PRICE_LIST — общий прайс; SUPPLIER_QUOTE — разовый счёт/КП заявки.'"
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_calculation_items_price_document_type",
        "calculation_items",
        type_="check",
    )
    op.drop_index(
        "ix_calculation_items_price_version_id", table_name="calculation_items"
    )
    op.drop_constraint(
        "fk_calculation_items_price_document_id",
        "calculation_items",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_calculation_items_price_version_id",
        "calculation_items",
        type_="foreignkey",
    )
    op.drop_column("calculation_items", "price_tier_max_quantity")
    op.drop_column("calculation_items", "price_tier_min_quantity")
    op.drop_column("calculation_items", "price_kind")
    op.drop_column("calculation_items", "price_source")
    op.drop_column("calculation_items", "price_document_type")
    op.drop_column("calculation_items", "price_document_id")
    op.drop_column("calculation_items", "price_version_id")
