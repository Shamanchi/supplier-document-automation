"""service clients with scoped api keys

Revision ID: 0006_fsa_service_clients
Revises: 0005_fsa_product_keys
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_fsa_service_clients"
down_revision = "0005_fsa_product_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_client",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("key_prefix", sa.String(length=32), nullable=False, unique=True),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("scopes", sa.String(length=200), nullable=False, server_default="read"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_service_client_key_prefix", "service_client", ["key_prefix"])


def downgrade() -> None:
    op.drop_index("ix_service_client_key_prefix", table_name="service_client")
    op.drop_table("service_client")
