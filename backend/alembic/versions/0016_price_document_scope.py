# -*- coding: utf-8 -*-
"""Инварианты области действия ценовых документов.

Revision ID: 0016_price_document_scope
Revises: 0015_unique_batch_tender_ref
Create Date: 2026-09-13
"""

from __future__ import annotations

from alembic import op

revision = "0016_price_document_scope"
down_revision = "0015_unique_batch_tender_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_price_versions_document_scope",
        "price_versions",
        "(document_type = 'PRICE_LIST' AND check_batch_id IS NULL) OR "
        "(document_type = 'SUPPLIER_QUOTE' AND check_batch_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_price_versions_quote_not_active",
        "price_versions",
        "NOT (document_type = 'SUPPLIER_QUOTE' AND status = 'active')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_price_versions_quote_not_active",
        "price_versions",
        type_="check",
    )
    op.drop_constraint(
        "ck_price_versions_document_scope",
        "price_versions",
        type_="check",
    )
