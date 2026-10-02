"""Add internal UI scope to service clients.

Revision ID: 2c7a9e4d1b63
Revises: 9f4b1d7a2c60
"""

from alembic import op
import sqlalchemy as sa


revision = "2c7a9e4d1b63"
down_revision = "9f4b1d7a2c60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "service_clients",
        sa.Column("is_internal", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_index(
        "uq_service_clients_single_internal",
        "service_clients",
        ["is_internal"],
        unique=True,
        postgresql_where=sa.text("is_internal"),
    )


def downgrade() -> None:
    op.drop_index("uq_service_clients_single_internal", table_name="service_clients")
    op.drop_column("service_clients", "is_internal")
