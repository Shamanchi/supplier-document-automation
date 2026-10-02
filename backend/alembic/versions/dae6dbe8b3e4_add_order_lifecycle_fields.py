"""Жизненный цикл заявки: статус, клиент и номер (price_requests).

Revision ID: dae6dbe8b3e4
Revises: n1_merge_vat_durable_rule
Create Date: 2026-09-03
"""

from alembic import op
import sqlalchemy as sa


revision = "dae6dbe8b3e4"
down_revision = "n1_merge_vat_durable_rule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "price_requests",
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
    )
    op.add_column(
        "price_requests",
        sa.Column("client", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "price_requests",
        sa.Column("number", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("price_requests", "number")
    op.drop_column("price_requests", "client")
    op.drop_column("price_requests", "status")
