# -*- coding: utf-8 -*-
"""Задача №7, шаг 2: журнал правок КП и файлы заявки.

`quote_adjustments` — журнал ручных правок КП (НДС/скидка/поставщик) с
причиной; актуальные значения читаются как последняя запись по полю.

`order_files` — зарегистрированные файлы заявки `{ fileName, kind }`;
бинарные данные идут через канонический `POST /uploads`.

Revision ID: 0012_quote_adj_order_files
Revises: 0011_order_check_batch_link
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_quote_adj_order_files"
down_revision = "0011_order_check_batch_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quote_adjustments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "request_id",
            sa.Uuid(),
            sa.ForeignKey("price_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field", sa.String(length=20), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "actor_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_quote_adjustments_request_id",
        "quote_adjustments",
        ["request_id", "created_at"],
    )
    op.create_table(
        "order_files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "request_id",
            sa.Uuid(),
            sa.ForeignKey("price_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="other"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_order_files_request_id",
        "order_files",
        ["request_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_order_files_request_id", table_name="order_files")
    op.drop_table("order_files")
    op.drop_index("ix_quote_adjustments_request_id", table_name="quote_adjustments")
    op.drop_table("quote_adjustments")
