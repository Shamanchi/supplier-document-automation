"""Deduplicate and enforce the identity of parse rules.

Revision ID: 7c9e2a6f4d81
Revises: 0b3a1f8c9d2e
Create Date: 2026-07-13
"""

from alembic import op


revision = "7c9e2a6f4d81"
down_revision = "0b3a1f8c9d2e"
branch_labels = None
depends_on = None


_DUPLICATES = """
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
            CASE WHEN validation ->> 'passed' = 'true' THEN 0 ELSE 1 END,
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
    # Existing deployments may contain duplicate rule identities created before
    # this invariant was enforced. Preserve the best usable rule, point all
    # dependent rows at it, then remove only the redundant records.
    op.execute(
        _DUPLICATES
        + """
        UPDATE price_versions AS version
        SET parse_rule_id = duplicates.keeper_id
        FROM duplicates
        WHERE version.parse_rule_id = duplicates.id
        """
    )
    op.execute(
        _DUPLICATES
        + """
        UPDATE corpus_run_files AS corpus_file
        SET parse_rule_id = duplicates.keeper_id
        FROM duplicates
        WHERE corpus_file.parse_rule_id = duplicates.id
        """
    )
    op.execute(
        _DUPLICATES
        + """
        DELETE FROM parse_rules AS rule
        USING duplicates
        WHERE rule.id = duplicates.id
        """
    )
    # A NULL supplier denotes a global rule. Its own sentinel makes global and
    # supplier-specific rule identities unique in the same index.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_parse_rules_identity
        ON parse_rules (header_hash, COALESCE(supplier_id::text, '__global__'), revision)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_parse_rules_identity")
