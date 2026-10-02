# -*- coding: utf-8 -*-
"""Ценовые документы заявки из Bitrix24 (Ф2).

Заявка из сделки — это комплект: ТЗ/техдокументы (Ф3) и ценовой документ (Ф2).
Роль PRICE ведёт не в `documents`, а в контур прайсов, поэтому разделение
фиксируется в модели данных:

* `price_files.source` — откуда пришёл файл: ручная загрузка или Bitrix24;
* `price_versions.document_type` — `PRICE_LIST` (общий прайс поставщика,
  может стать глобально активным) или `SUPPLIER_QUOTE` (разовый счёт/КП,
  действует только в связанной заявке);
* `price_versions.check_batch_id` — заявка для разового счёта/КП.

Связь Bitrix-файла с локальным прайсом хранится в `external_links`
(entity_type = `price_file`), как и для документов Ф3.

Revision ID: 0013_bitrix_price_documents
Revises: 0012_quote_adj_order_files
Create Date: 2026-09-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_bitrix_price_documents"
down_revision = "0012_quote_adj_order_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "price_files",
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="UPLOAD",
        ),
    )
    op.create_check_constraint(
        "ck_price_files_source",
        "price_files",
        "source IN ('UPLOAD', 'BITRIX', 'API')",
    )
    op.add_column(
        "price_versions",
        sa.Column(
            "document_type",
            sa.String(length=16),
            nullable=False,
            server_default="PRICE_LIST",
        ),
    )
    op.add_column(
        "price_versions",
        sa.Column("check_batch_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_price_versions_check_batch_id",
        "price_versions",
        "check_batches",
        ["check_batch_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_price_versions_check_batch_id",
        "price_versions",
        ["check_batch_id"],
    )
    op.create_check_constraint(
        "ck_price_versions_document_type",
        "price_versions",
        "document_type IN ('PRICE_LIST', 'SUPPLIER_QUOTE')",
    )
    op.execute(
        "COMMENT ON COLUMN price_versions.document_type IS"
        " 'PRICE_LIST — общий прайс; SUPPLIER_QUOTE — разовый счёт/КП заявки.'"
    )
    op.execute(
        "COMMENT ON COLUMN price_versions.check_batch_id IS"
        " 'Заявка разового счёта/КП; у общего прайса NULL.'"
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_price_versions_document_type", "price_versions", type_="check"
    )
    op.drop_index("ix_price_versions_check_batch_id", table_name="price_versions")
    op.drop_constraint(
        "fk_price_versions_check_batch_id", "price_versions", type_="foreignkey"
    )
    op.drop_column("price_versions", "check_batch_id")
    op.drop_column("price_versions", "document_type")
    op.drop_constraint("ck_price_files_source", "price_files", type_="check")
    op.drop_column("price_files", "source")
