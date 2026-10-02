"""Add project workspaces and archive existing data.

Revision ID: 5e8c1f7a9b20
Revises: 3d8e6f1a2b40
"""

from alembic import op
import sqlalchemy as sa


revision = "5e8c1f7a9b20"
down_revision = "3d8e6f1a2b40"
branch_labels = None
depends_on = None

ORIGINAL_ID = "10000000-0000-4000-8000-000000000001"
ARCHIVE_ID = "10000000-0000-4000-8000-000000000002"


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=96), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_projects_slug"),
    )
    op.create_index(
        "uq_projects_default",
        "projects",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
        sqlite_where=sa.text("is_default = 1"),
    )
    op.execute(
        sa.text(
            "INSERT INTO projects (id, slug, name, description, is_default, is_archived) VALUES "
            f"('{ORIGINAL_ID}', 'original-prices', 'Оригинальные прайсы', "
            "'Только исходные файлы из original/datasets/прайсы', true, false), "
            f"('{ARCHIVE_ID}', 'legacy-archive', 'Архив прежних загрузок', "
            "'Все данные, загруженные до введения проектов', false, true)"
        )
    )

    for table in ("price_files", "global_categories", "supplier_categories"):
        op.add_column(table, sa.Column("project_id", sa.Uuid(), nullable=True))
        op.execute(sa.text(f"UPDATE {table} SET project_id = '{ARCHIVE_ID}'"))
        op.alter_column(
            table,
            "project_id",
            nullable=False,
            server_default=sa.text(f"'{ORIGINAL_ID}'::uuid"),
        )
        op.create_foreign_key(
            f"fk_{table}_project_id", table, "projects", ["project_id"], ["id"], ondelete="RESTRICT"
        )
        op.create_index(f"ix_{table}_project_id", table, ["project_id"], unique=False)

    op.drop_index("uq_global_categories_root_name", table_name="global_categories")
    op.create_index(
        "uq_global_categories_root_name",
        "global_categories",
        ["project_id", "normalized_name"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NULL"),
        sqlite_where=sa.text("parent_id IS NULL"),
    )
    op.drop_index("uq_supplier_categories_supplier_path", table_name="supplier_categories")
    op.execute(
        "CREATE UNIQUE INDEX uq_supplier_categories_supplier_path "
        "ON supplier_categories (project_id, supplier_id, md5(normalized_path))"
    )


def downgrade() -> None:
    op.drop_index("uq_supplier_categories_supplier_path", table_name="supplier_categories")
    op.execute(
        "CREATE UNIQUE INDEX uq_supplier_categories_supplier_path "
        "ON supplier_categories (supplier_id, md5(normalized_path))"
    )
    op.drop_index("uq_global_categories_root_name", table_name="global_categories")
    op.create_index(
        "uq_global_categories_root_name",
        "global_categories",
        ["normalized_name"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NULL"),
        sqlite_where=sa.text("parent_id IS NULL"),
    )
    for table in ("supplier_categories", "global_categories", "price_files"):
        op.drop_index(f"ix_{table}_project_id", table_name=table)
        op.drop_constraint(f"fk_{table}_project_id", table, type_="foreignkey")
        op.drop_column(table, "project_id")
    op.drop_index("uq_projects_default", table_name="projects")
    op.drop_table("projects")
