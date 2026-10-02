"""Add global and per-supplier category directories.

Revision ID: 6b7c9d2e4f10
Revises: 4f8b2d1c7a90
"""

from alembic import context, op
import sqlalchemy as sa
import re
import unicodedata
import uuid
from datetime import datetime, timezone


revision = "6b7c9d2e4f10"
down_revision = "4f8b2d1c7a90"
branch_labels = None
depends_on = None


_WS_RE = re.compile(r"\s+")
_PATH_RE = re.compile(r"\s+/\s+")


def _segments(value: str) -> list[str]:
    source = unicodedata.normalize("NFKC", value).strip()
    return [segment.strip() for segment in _PATH_RE.split(source) if segment.strip()]


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return _WS_RE.sub(" ", value).strip()


def _backfill_existing_paths() -> None:
    """Materialize category links for rows parsed before this migration."""

    if context.is_offline_mode():
        return
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT
              v.supplier_id,
              i.category_path,
              (array_agg(i.price_version_id ORDER BY v.created_at, i.created_at))[1]
                AS first_version_id,
              (array_agg(i.price_version_id ORDER BY v.created_at DESC, i.created_at DESC))[1]
                AS last_version_id,
              MAX(v.created_at) AS last_seen_at
            FROM supplier_price_items i
            JOIN price_versions v ON v.id = i.price_version_id
            WHERE i.category_path IS NOT NULL AND TRIM(i.category_path) <> ''
            GROUP BY v.supplier_id, i.category_path
            ORDER BY MIN(v.created_at), MIN(i.created_at)
            """
        )
    ).mappings().all()
    now = datetime.now(timezone.utc)
    categories: dict[tuple[object, str], uuid.UUID] = {}
    aliases: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
    category_records: dict[uuid.UUID, dict] = {}
    alias_records: dict[uuid.UUID, dict] = {}
    category_last_seen_at: dict[uuid.UUID, datetime] = {}
    alias_last_seen_at: dict[uuid.UUID, datetime] = {}
    path_links: list[dict] = []
    for row in rows:
        raw_path = unicodedata.normalize("NFKC", str(row["category_path"])).strip()
        parts = _segments(raw_path)
        parent_id = None
        source_prefix: list[str] = []
        normalized_prefix: list[str] = []
        leaf_id = None
        for index, part in enumerate(parts):
            source_prefix.append(part)
            normalized_prefix.append(_normalized(part))
            normalized_path = " / ".join(normalized_prefix)
            key = (row["supplier_id"], normalized_path)
            category_id = categories.get(key)
            source_path = raw_path if index == len(parts) - 1 else " / ".join(source_prefix)
            if category_id is None:
                category_id = uuid.uuid4()
                category_records[category_id] = {
                    "id": category_id,
                    "supplier_id": row["supplier_id"],
                    "parent_id": parent_id,
                    "source_name": part,
                    "normalized_name": normalized_prefix[-1],
                    "source_path": source_path,
                    "normalized_path": normalized_path,
                    "first_seen_version_id": row["first_version_id"],
                    "last_seen_version_id": row["last_version_id"],
                    "now": now,
                }
                category_last_seen_at[category_id] = row["last_seen_at"]
                categories[key] = category_id
            elif row["last_seen_at"] > category_last_seen_at[category_id]:
                category_records[category_id]["last_seen_version_id"] = row[
                    "last_version_id"
                ]
                category_last_seen_at[category_id] = row["last_seen_at"]
            alias_key = (category_id, source_path)
            alias_id = aliases.get(alias_key)
            if alias_id is None:
                alias_id = uuid.uuid4()
                alias_records[alias_id] = {
                    "id": alias_id,
                    "category_id": category_id,
                    "source_path": source_path,
                    "first_seen_version_id": row["first_version_id"],
                    "last_seen_version_id": row["last_version_id"],
                    "now": now,
                }
                alias_last_seen_at[alias_id] = row["last_seen_at"]
                aliases[alias_key] = alias_id
            elif row["last_seen_at"] > alias_last_seen_at[alias_id]:
                alias_records[alias_id]["last_seen_version_id"] = row[
                    "last_version_id"
                ]
                alias_last_seen_at[alias_id] = row["last_seen_at"]
            parent_id = category_id
            leaf_id = category_id
        if leaf_id is not None:
            path_links.append(
                {
                    "supplier_id": row["supplier_id"],
                    "category_path": row["category_path"],
                    "category_id": leaf_id,
                }
            )

    category_insert = sa.text(
        """
        INSERT INTO supplier_categories
          (id, supplier_id, parent_id, source_name, normalized_name,
           source_path, normalized_path, first_seen_version_id,
           last_seen_version_id, created_at, updated_at)
        VALUES
          (:id, :supplier_id, :parent_id, :source_name, :normalized_name,
           :source_path, :normalized_path, :first_seen_version_id,
           :last_seen_version_id, :now, :now)
        """
    )
    category_values = list(category_records.values())
    for offset in range(0, len(category_values), 10_000):
        bind.execute(category_insert, category_values[offset : offset + 10_000])

    alias_insert = sa.text(
        """
        INSERT INTO supplier_category_aliases
          (id, supplier_category_id, source_path, first_seen_version_id,
           last_seen_version_id, created_at, last_seen_at)
        VALUES
          (:id, :category_id, :source_path, :first_seen_version_id,
           :last_seen_version_id, :now, :now)
        """
    )
    alias_values = list(alias_records.values())
    for offset in range(0, len(alias_values), 10_000):
        bind.execute(alias_insert, alias_values[offset : offset + 10_000])
    bind.execute(
        sa.text(
            """
            CREATE TEMPORARY TABLE price_category_backfill_links (
              supplier_id uuid NOT NULL,
              category_path text NOT NULL,
              category_id uuid NOT NULL
            ) ON COMMIT DROP
            """
        )
    )
    link_insert = sa.text(
        """
        INSERT INTO price_category_backfill_links
          (supplier_id, category_path, category_id)
        VALUES (:supplier_id, :category_path, :category_id)
        """
    )
    for offset in range(0, len(path_links), 10_000):
        bind.execute(link_insert, path_links[offset : offset + 10_000])
    bind.execute(
        sa.text(
            """
            UPDATE supplier_price_items AS item
            SET supplier_category_id = link.category_id
            FROM price_versions AS version, price_category_backfill_links AS link
            WHERE item.price_version_id = version.id
              AND version.supplier_id = link.supplier_id
              AND item.category_path = link.category_path
            """
        )
    )


def upgrade() -> None:
    op.create_table(
        "global_categories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.String(length=512), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("normalized_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('active','deprecated')", name="ck_global_categories_status"
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["global_categories.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "parent_id", "normalized_name", name="uq_global_categories_parent_name"
        ),
    )
    op.create_index(
        "ix_global_categories_parent_id", "global_categories", ["parent_id"], unique=False
    )
    op.create_index(
        "uq_global_categories_root_name",
        "global_categories",
        ["normalized_name"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NULL"),
        sqlite_where=sa.text("parent_id IS NULL"),
    )

    op.create_table(
        "supplier_categories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("supplier_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("normalized_path", sa.Text(), nullable=False),
        sa.Column("first_seen_version_id", sa.Uuid(), nullable=True),
        sa.Column("last_seen_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["supplier_categories.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["first_seen_version_id"], ["price_versions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["last_seen_version_id"], ["price_versions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_supplier_categories_supplier_path "
        "ON supplier_categories (supplier_id, md5(normalized_path))"
    )
    op.create_index(
        "ix_supplier_categories_supplier_id",
        "supplier_categories",
        ["supplier_id"],
        unique=False,
    )
    op.create_index(
        "ix_supplier_categories_parent_id",
        "supplier_categories",
        ["parent_id"],
        unique=False,
    )

    op.create_table(
        "supplier_category_aliases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("supplier_category_id", sa.Uuid(), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("first_seen_version_id", sa.Uuid(), nullable=True),
        sa.Column("last_seen_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["supplier_category_id"], ["supplier_categories.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["first_seen_version_id"], ["price_versions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["last_seen_version_id"], ["price_versions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # A full Text value can exceed PostgreSQL's ~2704-byte B-tree row limit.
    # Keep the exact provenance text and enforce practical identity on its
    # deterministic digest instead of truncating the source value.
    op.execute(
        "CREATE UNIQUE INDEX uq_supplier_category_alias_path "
        "ON supplier_category_aliases (supplier_category_id, md5(source_path))"
    )
    op.create_index(
        "ix_supplier_category_aliases_supplier_category_id",
        "supplier_category_aliases",
        ["supplier_category_id"],
        unique=False,
    )

    op.create_table(
        "supplier_category_mappings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("supplier_category_id", sa.Uuid(), nullable=False),
        sa.Column("global_category_id", sa.Uuid(), nullable=False),
        sa.Column(
            "mapping_source", sa.String(length=16), nullable=False, server_default="manual"
        ),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "mapping_source IN ('manual','import','model')",
            name="ck_supplier_category_mappings_source",
        ),
        sa.ForeignKeyConstraint(
            ["supplier_category_id"], ["supplier_categories.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["global_category_id"], ["global_categories.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "supplier_category_id", name="uq_supplier_category_mappings_supplier_category"
        ),
    )
    op.create_index(
        "ix_supplier_category_mappings_supplier_category_id",
        "supplier_category_mappings",
        ["supplier_category_id"],
        unique=False,
    )
    op.create_index(
        "ix_supplier_category_mappings_global_category_id",
        "supplier_category_mappings",
        ["global_category_id"],
        unique=False,
    )

    op.add_column("supplier_price_items", sa.Column("supplier_category_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_supplier_price_items_supplier_category",
        "supplier_price_items",
        "supplier_categories",
        ["supplier_category_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_supplier_price_items_supplier_category_id",
        "supplier_price_items",
        ["supplier_category_id"],
        unique=False,
    )
    _backfill_existing_paths()


def downgrade() -> None:
    op.drop_index(
        "ix_supplier_price_items_supplier_category_id", table_name="supplier_price_items"
    )
    op.drop_constraint(
        "fk_supplier_price_items_supplier_category",
        "supplier_price_items",
        type_="foreignkey",
    )
    op.drop_column("supplier_price_items", "supplier_category_id")
    op.drop_index(
        "ix_supplier_category_mappings_global_category_id",
        table_name="supplier_category_mappings",
    )
    op.drop_index(
        "ix_supplier_category_mappings_supplier_category_id",
        table_name="supplier_category_mappings",
    )
    op.drop_table("supplier_category_mappings")
    op.drop_index(
        "ix_supplier_category_aliases_supplier_category_id",
        table_name="supplier_category_aliases",
    )
    op.drop_index(
        "uq_supplier_category_alias_path",
        table_name="supplier_category_aliases",
    )
    op.drop_table("supplier_category_aliases")
    op.drop_index("ix_supplier_categories_parent_id", table_name="supplier_categories")
    op.drop_index("ix_supplier_categories_supplier_id", table_name="supplier_categories")
    op.drop_index(
        "uq_supplier_categories_supplier_path",
        table_name="supplier_categories",
    )
    op.drop_table("supplier_categories")
    op.drop_index("uq_global_categories_root_name", table_name="global_categories")
    op.drop_index("ix_global_categories_parent_id", table_name="global_categories")
    op.drop_table("global_categories")
