"""Индекс обозначений изделий: вход в реестр по коду продукта, а не по номеру.

У позиции прайса номера сертификата нет — есть артикул и наименование.
Таблица производная и пересобирается из карточек реестра.

Revision ID: 0005_fsa_product_keys
Revises: 0004_fsa_decisions
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_fsa_product_keys"
down_revision = "0004_fsa_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "registry_product_key",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "certificate_id",
            sa.Uuid(),
            sa.ForeignKey("registry_certificate.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("product_key", sa.String(length=128), nullable=False),
        sa.Column("source_field", sa.String(length=32), server_default="product_full_name"),
        sa.UniqueConstraint("certificate_id", "product_key", name="uq_registry_product_key"),
    )
    op.create_index("ix_registry_product_key", "registry_product_key", ["product_key"])


def downgrade() -> None:
    op.drop_index("ix_registry_product_key", table_name="registry_product_key")
    op.drop_table("registry_product_key")
