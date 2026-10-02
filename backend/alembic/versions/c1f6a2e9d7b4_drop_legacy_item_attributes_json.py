"""Drop the legacy JSON attribute column after the typed backfill is complete.

Revision ID: c1f6a2e9d7b4
Revises: d7a4e8f1b2c3
Create Date: 2026-07-13

The online guard makes an accidental destructive deploy fail closed: the old
column may be removed only when every non-null legacy value has been migrated
or deliberately cleared by ``app.attribute_backfill``.
"""
from __future__ import annotations

from alembic import context, op
import sqlalchemy as sa


revision = "c1f6a2e9d7b4"
down_revision = "d7a4e8f1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQL generation has no database connection to inspect.  The same
    # migration always performs this check in a real deployment.
    if not context.is_offline_mode():
        remaining = op.get_bind().execute(
            sa.text("SELECT count(*) FROM supplier_price_items WHERE attributes IS NOT NULL")
        ).scalar_one()
        if remaining:
            raise RuntimeError(
                "Cannot drop supplier_price_items.attributes: "
                f"{remaining} legacy values have not been backfilled"
            )

    op.drop_column("supplier_price_items", "attributes")


def downgrade() -> None:
    op.add_column("supplier_price_items", sa.Column("attributes", sa.JSON(), nullable=True))
