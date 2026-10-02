"""Значения enum-типов PostgreSQL из schema.sql.

Значения дублируются здесь намеренно: SQLAlchemy должен знать их и для записи
(иначе PostgreSQL отвергает varchar в enum-колонке: «column is of type
check_item_status but expression is of type character varying»), и для чтения
(иначе LookupError на значении, которого нет в списке).

Расхождение с базой ловит тест `test_enum_values_match_database`: он читает
`pg_enum` живой схемы и сравнивает с этим словарём.

Перенесено из supplier-document-automation без изменений (docs/migration/01-backend-core.md).
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import ENUM

#: Все enum-типы, которые нужны ORM-моделям. Порядок значений — как в schema.sql.
ENUM_VALUES: dict[str, tuple[str, ...]] = {
    "user_status": ("INVITED", "ACTIVE", "DISABLED", "LOCKED"),
    "user_role": ("manager", "admin", "sysadmin", "viewer"),
    "check_batch_status": (
        "DRAFT",
        "QUEUED",
        "RUNNING",
        "REVIEW_REQUIRED",
        "COMPLETED",
        "CANCELLED",
        "ARCHIVED",
    ),
    "check_item_status": (
        "DRAFT",
        "QUEUED",
        "INGESTING",
        "NEEDS_INPUT",
        "EXTRACTING",
        "VERIFYING_SOURCES",
        "NORMALIZING",
        "AWAITING_PARAMETER_REVIEW",
        "MATCHING",
        "REVIEW_REQUIRED",
        "READY_FOR_MANAGER_REVIEW",
        "VERDICT_APPROVED",
        "REPORTING",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
    ),
    "verdict_value": ("COMPLIANT", "PARTIAL", "NON_COMPLIANT", "PENDING"),
    "pipeline_stage": (
        "INGESTING",
        "EXTRACTING",
        "VERIFYING_SOURCES",
        "NORMALIZING",
        "MATCHING",
        "REPORTING",
    ),
    "pipeline_run_status": ("QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"),
    "stage_status": ("QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"),
    "document_role": ("REQUIREMENT", "FACT"),
    "document_status": ("UPLOADED", "QUARANTINE", "READY", "REJECTED"),
    "document_source": ("UPLOAD", "BITRIX", "API"),
    "document_type": ("NATIVE", "SCANNED", "MIXED", "CORRUPTED"),
    "locator_kind": ("PDF", "XLSX", "DOCX", "MANUAL"),
    "fragment_method": ("TABLE", "TEXT", "OCR", "COMPOSITE"),
    "extraction_method": ("TABLE", "TEMPLATE", "REGEX", "LLM", "MANUAL"),
    "verification_method": ("EXACT_SUBSTRING", "NORMALIZED", "FUZZY", "MANUAL"),
    "catalog_status": (
        "DRAFT",
        "VALIDATING",
        "VALID",
        "INVALID",
        "PUBLISHED",
        "RETIRED",
    ),
    "value_kind": (
        "NUMBER",
        "RANGE",
        "TEXT",
        "BOOLEAN",
        "SET",
        "MATERIAL",
        "UNKNOWN",
    ),
    "suggestion_status": ("PENDING", "ACCEPTED", "REJECTED"),
    "issue_severity": ("INFO", "WARNING", "ERROR"),
    "conversion_formula": ("LINEAR",),
    "match_method": ("CANONICAL", "ALIAS", "FUZZY", "EMBEDDING", "MANUAL"),
    "comparison_status": (
        "COMPLIANT",
        "NON_COMPLIANT",
        "REVIEW_REQUIRED",
        "NOT_FOUND_IN_FACT",
        "NOT_REQUIRED",
    ),
    "row_kind": ("PAIRED", "MISSING_FACT", "EXTRA_FACT"),
    "report_status": (
        "QUEUED",
        "GENERATING",
        "COMPLETED",
        "PARTIALLY_COMPLETED",
        "FAILED",
    ),
    "report_format": ("XLSX", "PDF"),
    "integration_job_type": ("IMPORT_FILE", "PUBLISH_REPORT"),
    "integration_job_status": (
        "QUEUED",
        "RUNNING",
        "RETRY_SCHEDULED",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
    ),
    "comparison_operator": (
        "EQUAL",
        "NOT_EQUAL",
        "GREATER",
        "GREATER_OR_EQUAL",
        "LESS",
        "LESS_OR_EQUAL",
        "RANGE",
        "SET_CONTAINS",
        "TEXT_MATCH",
        "MATERIAL_MATCH",
    ),
}


def pg_enum(name: str) -> ENUM:
    """Ссылка на enum-тип, созданный миграцией.

    `create_type=False` обязателен: тип уже существует в БД, и попытка создать
    его повторно ломает миграции.
    """
    return ENUM(*ENUM_VALUES[name], name=name, create_type=False)
