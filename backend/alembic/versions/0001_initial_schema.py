"""Начальная схема контура Фазы 3.

Revision ID: 0001_initial_schema
Revises: p3_bridge_identity_merge
Create Date: 2026-08-10

Миграция исполняет `alembic/sql/0001_initial_schema.sql` — копию проверенного
DDL из docs/architecture/db/schema.sql (46 таблиц, 30 enum, триггеры
immutability и updated_at, составные FK версии каталога).

Почему raw SQL, а не операции Alembic: DDL содержит plpgsql-функции, триггеры,
частичные уникальные индексы и составные внешние ключи, которые autogenerate не
воспроизводит. Файл уже проверен на PostgreSQL 15 и покрыт тестом
docs/architecture/db/schema_invariants_test.sql — переписывание его в op.*
означало бы потерю этой проверки.

Витрина Фазы 2 (products, suppliers, attributes, ...) сюда НЕ входит: она живёт
по db/schema_phase2_showcase.sql и принадлежит внешнему контуру, см.
docs/architecture/CONTOUR_BOUNDARIES.md.
"""

from pathlib import Path

from alembic import op

revision = "0001_initial_schema"
down_revision = "p3_bridge_identity_merge"
branch_labels = None
depends_on = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def upgrade() -> None:
    sql = (SQL_DIR / "0001_initial_schema.sql").read_text(encoding="utf-8")
    op.execute(_without_own_transaction(sql))


def _without_own_transaction(sql: str) -> str:
    """Убирает BEGIN/COMMIT исходного файла.

    schema.sql обёрнут в транзакцию, чтобы его можно было применять напрямую
    через psql. Внутри миграции это вредно: Alembic уже открыл транзакцию, и
    вложенный COMMIT завершил бы её до записи версии в alembic_version.
    Файл при этом остаётся точной копией schema.sql — так его можно сверять.
    """
    kept = [
        line
        for line in sql.splitlines()
        if line.strip().upper() not in {"BEGIN;", "COMMIT;"}
    ]
    return "\n".join(kept)


# Объекты, создаваемые upgrade (таблицы — в обратном порядке файла схемы,
# чтобы FK снимались до родительских таблиц; CASCADE покрывает остаток).
# Отличие от фазы 3: там downgrade делал DROP SCHEMA public CASCADE — в
# объединённом графе (docs/migration/05-migrations.md) это снесло бы весь
# прайс-контур фазы 2 и объекты pgvector, поэтому здесь удаляется только то,
# что создала сама миграция.
_DOWNGRADE_TABLES = [
    "showcase_product_refs",
    "audit_events",
    "integration_outbox",
    "integration_jobs",
    "external_links",
    "report_artifacts",
    "report_snapshots",
    "item_verdicts",
    "comparison_overrides",
    "comparisons",
    "match_candidates",
    "normalization_issues",
    "normalized_parameters",
    "parameter_revisions",
    "source_verifications",
    "extraction_evidence",
    "raw_parameters",
    "extraction_runs",
    "document_tables",
    "source_fragments",
    "document_pages",
    "document_versions",
    "documents",
    "ai_usage_log",
    "stage_runs",
    "pipeline_runs",
    "check_items",
    "check_batches",
    "rule_snapshots",
    "pipeline_definitions",
    "ai_run_configs",
    "prompt_registry",
    "parameter_templates",
    "equipment_types",
    "material_nodes",
    "conversion_rules",
    "unit_aliases",
    "units",
    "catalog_suggestions",
    "parameter_aliases",
    "canonical_parameters",
    "catalog_versions",
    "tender_refs",
    "refresh_sessions",
    "user_roles",
    "users",
]

_DOWNGRADE_TYPES = [
    "integration_job_status",
    "integration_job_type",
    "report_format",
    "report_status",
    "prompt_status",
    "verdict_value",
    "row_kind",
    "comparison_status",
    "comparison_operator",
    "match_method",
    "conversion_formula",
    "issue_severity",
    "suggestion_status",
    "catalog_status",
    "value_kind",
    "verification_method",
    "extraction_method",
    "fragment_method",
    "locator_kind",
    "document_source",
    "document_status",
    "document_type",
    "document_role",
    "pipeline_run_status",
    "stage_status",
    "pipeline_stage",
    "check_item_status",
    "check_batch_status",
    "user_status",
    "user_role",
]


def downgrade() -> None:
    # Схема создаётся целиком, поэтому и откатывается целиком: сначала
    # таблицы (каскадом снимая FK и триггеры), затем типы и функции.
    # Расширения (pgcrypto/pg_trgm/citext) не дропаются: в объединённой БД
    # их может использовать прайс-контур, а повторный upgrade создаёт их
    # через CREATE EXTENSION IF NOT EXISTS.
    for table in _DOWNGRADE_TABLES:
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    for type_ in _DOWNGRADE_TYPES:
        op.execute(f'DROP TYPE IF EXISTS "{type_}"')
    op.execute("DROP FUNCTION IF EXISTS trg_set_updated_at()")
    op.execute("DROP FUNCTION IF EXISTS trg_prevent_modify()")
