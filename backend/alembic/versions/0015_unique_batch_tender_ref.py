# -*- coding: utf-8 -*-
"""Одна локальная заявка на один внешний тендер.

Revision ID: 0015_unique_batch_tender_ref
Revises: 0014_calculation_price_snapshot
Create Date: 2026-09-13
"""

from __future__ import annotations

from alembic import op

revision = "0015_unique_batch_tender_ref"
down_revision = "0014_calculation_price_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_check_batches_tender_ref",
        "check_batches",
        ["tender_ref_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_check_batches_tender_ref",
        "check_batches",
        type_="unique",
    )
