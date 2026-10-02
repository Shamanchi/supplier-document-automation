"""Use a bounded expression index for exact category aliases.

Revision ID: 3d8e6f1a2b40
Revises: 1a7d9c4e2b60
"""

from alembic import op
import sqlalchemy as sa


revision = "3d8e6f1a2b40"
down_revision = "1a7d9c4e2b60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Environments that already applied 6b7c9d2e4f10 have a table constraint;
    # fresh installs already have the expression index. Both paths converge.
    op.execute(
        "ALTER TABLE supplier_category_aliases "
        "DROP CONSTRAINT IF EXISTS uq_supplier_category_alias_path"
    )
    op.execute("DROP INDEX IF EXISTS uq_supplier_category_alias_path")
    op.execute(
        "CREATE UNIQUE INDEX uq_supplier_category_alias_path "
        "ON supplier_category_aliases (supplier_category_id, md5(source_path))"
    )
    op.alter_column(
        "supplier_categories",
        "normalized_name",
        existing_type=sa.String(length=512),
        type_=sa.Text(),
        existing_nullable=False,
    )
    op.execute(
        "ALTER TABLE supplier_categories "
        "DROP CONSTRAINT IF EXISTS uq_supplier_categories_supplier_path"
    )
    op.execute("DROP INDEX IF EXISTS uq_supplier_categories_supplier_path")
    op.execute(
        "CREATE UNIQUE INDEX uq_supplier_categories_supplier_path "
        "ON supplier_categories (supplier_id, md5(normalized_path))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_supplier_categories_supplier_path")
    op.create_unique_constraint(
        "uq_supplier_categories_supplier_path",
        "supplier_categories",
        ["supplier_id", "normalized_path"],
    )
    op.alter_column(
        "supplier_categories",
        "normalized_name",
        existing_type=sa.Text(),
        type_=sa.String(length=512),
        existing_nullable=False,
    )
    op.execute("DROP INDEX IF EXISTS uq_supplier_category_alias_path")
    op.create_unique_constraint(
        "uq_supplier_category_alias_path",
        "supplier_category_aliases",
        ["supplier_category_id", "source_path"],
    )
