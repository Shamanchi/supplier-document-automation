"""Scope parse rules by project and preserve their actual ownership.

Revision ID: 6f9a2c4d8e10
Revises: 5e8c1f7a9b20
"""

from alembic import op
import sqlalchemy as sa


revision = "6f9a2c4d8e10"
down_revision = "5e8c1f7a9b20"
branch_labels = None
depends_on = None

ORIGINAL_ID = "10000000-0000-4000-8000-000000000001"
ARCHIVE_ID = "10000000-0000-4000-8000-000000000002"


_DUPLICATES_WITHOUT_PROJECT = """
WITH ranked AS (
    SELECT
        id,
        first_value(id) OVER canonical_rule AS keeper_id,
        row_number() OVER canonical_rule AS duplicate_no
    FROM parse_rules
    WINDOW canonical_rule AS (
        PARTITION BY header_hash, supplier_id, revision
        ORDER BY
            CASE status
                WHEN 'active' THEN 0
                WHEN 'draft' THEN 1
                WHEN 'deprecated' THEN 2
                ELSE 3
            END,
            CASE provenance
                WHEN 'user_confirmed' THEN 0
                WHEN 'manual' THEN 1
                WHEN 'auto' THEN 2
                ELSE 3
            END,
            updated_at DESC NULLS LAST,
            created_at DESC NULLS LAST,
            id
    )
), duplicates AS (
    SELECT id, keeper_id
    FROM ranked
    WHERE duplicate_no > 1
)
"""


def upgrade() -> None:
    op.add_column("parse_rules", sa.Column("project_id", sa.Uuid(), nullable=True))

    # One rule cannot safely belong to two workspaces. Abort instead of making
    # an arbitrary choice if an old database ever contains such a link.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT version.parse_rule_id
                FROM price_versions AS version
                JOIN price_files AS price_file ON price_file.id = version.price_file_id
                WHERE version.parse_rule_id IS NOT NULL
                GROUP BY version.parse_rule_id
                HAVING count(DISTINCT price_file.project_id) > 1
            ) THEN
                RAISE EXCEPTION 'parse rule is referenced from more than one project';
            END IF;
        END $$
        """
    )

    # Provenance is the strongest ownership signal. A used rule without
    # provenance inherits the project of the version that uses it. Remaining
    # legacy/seed rules belong to the archive rather than leaking into the new
    # default workspace.
    op.execute(
        sa.text(
            f"""
            UPDATE parse_rules AS rule
            SET project_id = COALESCE(
                (
                    SELECT price_file.project_id
                    FROM price_versions AS source_version
                    JOIN price_files AS price_file
                      ON price_file.id = source_version.price_file_id
                    WHERE source_version.id = rule.created_from_version_id
                    LIMIT 1
                ),
                (
                    SELECT price_file.project_id
                    FROM price_versions AS used_version
                    JOIN price_files AS price_file
                      ON price_file.id = used_version.price_file_id
                    WHERE used_version.parse_rule_id = rule.id
                    LIMIT 1
                ),
                '{ARCHIVE_ID}'::uuid
            )
            """
        )
    )
    op.alter_column(
        "parse_rules",
        "project_id",
        nullable=False,
        server_default=sa.text(f"'{ORIGINAL_ID}'::uuid"),
    )
    op.create_foreign_key(
        "fk_parse_rules_project_id",
        "parse_rules",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_parse_rules_project_id", "parse_rules", ["project_id"], unique=False)
    op.drop_index("uq_parse_rules_identity", table_name="parse_rules")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_parse_rules_identity
        ON parse_rules (
            project_id,
            header_hash,
            COALESCE(supplier_id::text, '__global__'),
            revision
        )
        """
    )


def downgrade() -> None:
    op.drop_index("uq_parse_rules_identity", table_name="parse_rules")

    # A downgraded schema cannot represent per-project copies of the same
    # identity. Keep the best usable rule and repoint all dependent rows using
    # the same deterministic policy as the original identity migration.
    for table in (
        "price_versions",
        "price_imports",
        "price_import_reviews",
        "rule_generation_attempts",
        "corpus_run_files",
    ):
        op.execute(
            _DUPLICATES_WITHOUT_PROJECT
            + f"""
            UPDATE {table} AS dependent
            SET parse_rule_id = duplicates.keeper_id
            FROM duplicates
            WHERE dependent.parse_rule_id = duplicates.id
            """
        )
    op.execute(
        _DUPLICATES_WITHOUT_PROJECT
        + """
        DELETE FROM parse_rules AS rule
        USING duplicates
        WHERE rule.id = duplicates.id
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_parse_rules_identity
        ON parse_rules (header_hash, COALESCE(supplier_id::text, '__global__'), revision)
        """
    )
    op.drop_index("ix_parse_rules_project_id", table_name="parse_rules")
    op.drop_constraint("fk_parse_rules_project_id", "parse_rules", type_="foreignkey")
    op.drop_column("parse_rules", "project_id")
