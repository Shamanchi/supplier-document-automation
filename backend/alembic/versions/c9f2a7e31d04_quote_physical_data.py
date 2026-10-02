"""physical reference and reproducible quote snapshots

Revision ID: c9f2a7e31d04
Revises: b6d1f4a29c73
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c9f2a7e31d04"
down_revision = "b6d1f4a29c73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE attribute_definitions SET canonical_unit = 'm3' "
        "WHERE code = 'volume' AND canonical_unit IS NULL"
    )
    op.create_table(
        "product_physical_reference",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("match_key", sa.Text(), nullable=False),
        sa.Column("normalized_article", sa.Text(), nullable=True),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("weight_kg", sa.Numeric(14, 6), nullable=True),
        sa.Column("volume_m3", sa.Numeric(14, 9), nullable=True),
        sa.Column("source", sa.Text(), nullable=False, server_default="manual_import"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "match_key", name="uq_physical_reference_project_key"),
    )
    op.create_index("ix_product_physical_reference_project_id", "product_physical_reference", ["project_id"])
    op.create_index("ix_product_physical_reference_normalized_article", "product_physical_reference", ["normalized_article"])
    op.create_index("ix_product_physical_reference_normalized_name", "product_physical_reference", ["normalized_name"])

    for name, column in (
        ("supplier_name", sa.Column("supplier_name", sa.Text(), nullable=False, server_default="")),
        ("currency", sa.Column("currency", sa.String(3), nullable=True)),
        ("weight_kg", sa.Column("weight_kg", sa.Numeric(14, 6), nullable=True)),
        ("volume_m3", sa.Column("volume_m3", sa.Numeric(14, 9), nullable=True)),
        ("weight_source", sa.Column("weight_source", sa.String(24), nullable=False, server_default="")),
        ("volume_source", sa.Column("volume_source", sa.String(24), nullable=False, server_default="")),
        ("certificate_number", sa.Column("certificate_number", sa.Text(), nullable=True)),
        ("certificate_valid_until", sa.Column("certificate_valid_until", sa.Date(), nullable=True)),
        ("certificate_confirmed_by", sa.Column("certificate_confirmed_by", sa.Text(), nullable=True)),
        ("certificate_reference", sa.Column("certificate_reference", sa.Text(), nullable=True)),
    ):
        op.add_column("calculation_items", column)


def downgrade() -> None:
    for name in (
        "certificate_reference", "certificate_confirmed_by", "certificate_valid_until",
        "certificate_number", "volume_source", "weight_source", "volume_m3",
        "weight_kg", "currency", "supplier_name",
    ):
        op.drop_column("calculation_items", name)
    op.drop_index("ix_product_physical_reference_normalized_name", table_name="product_physical_reference")
    op.drop_index("ix_product_physical_reference_normalized_article", table_name="product_physical_reference")
    op.drop_index("ix_product_physical_reference_project_id", table_name="product_physical_reference")
    op.drop_table("product_physical_reference")
