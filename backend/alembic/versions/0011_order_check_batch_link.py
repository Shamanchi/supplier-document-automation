# -*- coding: utf-8 -*-
"""Задача №7, шаг 1: связка заявок и журнал событий.

`price_requests.check_batch_id` — FK на каноническую заявку фазы 3
(check-batch), unique, nullable: заявка в UI остаётся check-batch, а
`price_requests` становится её расчётным листом (1:1). Ручные заявки P2
живут без связки.

`order_events` — журнал событий заявки (решение №1 открытых вопросов):
send / cancel-send / complete-check / confirm-quote / archive.

Revision ID: 0011_order_check_batch_link
Revises: 0010_pump_main_extraction_preset
Create Date: 2026-09-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_order_check_batch_link"
down_revision = "0010_pump_main_extraction_preset"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "price_requests",
        sa.Column("check_batch_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_price_requests_check_batch_id",
        "price_requests",
        "check_batches",
        ["check_batch_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_price_requests_check_batch_id",
        "price_requests",
        ["check_batch_id"],
    )
    op.create_table(
        "order_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "check_batch_id",
            sa.Uuid(),
            sa.ForeignKey("check_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column(
            "actor_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_order_events_check_batch_id",
        "order_events",
        ["check_batch_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_order_events_check_batch_id", table_name="order_events"
    )
    op.drop_table("order_events")
    op.drop_constraint(
        "uq_price_requests_check_batch_id", "price_requests", type_="unique"
    )
    op.drop_constraint(
        "fk_price_requests_check_batch_id", "price_requests", type_="foreignkey"
    )
    op.drop_column("price_requests", "check_batch_id")
