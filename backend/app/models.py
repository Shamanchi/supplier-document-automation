"""SQLAlchemy models — v0 subset of the price_loader data model."""
import enum
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.db import Base
from app.matching.text import EMBEDDING_DIM


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


ORIGINAL_PROJECT_ID = uuid.UUID("10000000-0000-4000-8000-000000000001")
ARCHIVE_PROJECT_ID = uuid.UUID("10000000-0000-4000-8000-000000000002")


class VersionStatus(str, enum.Enum):
    uploaded = "uploaded"
    parsing = "parsing"
    needs_mapping = "needs_mapping"
    complex = "complex"
    validation_failed = "validation_failed"
    validated = "validated"
    active = "active"
    superseded = "superseded"
    withdrawn = "withdrawn"
    failed = "failed"
    needs_ocr = "needs_ocr"
    unsupported_format = "unsupported_format"


class PriceImportStatus(str, enum.Enum):
    queued = "queued"
    parsing = "parsing"
    generating_rule = "generating_rule"
    compiling_rule = "compiling_rule"
    reparsing = "reparsing"
    quality_check = "quality_check"
    completed = "completed"
    review_required = "review_required"
    needs_ocr = "needs_ocr"
    failed = "failed"

    @classmethod
    def terminal_values(cls) -> frozenset[str]:
        return frozenset(
            {
                cls.completed.value,
                cls.review_required.value,
                cls.needs_ocr.value,
                cls.failed.value,
            }
        )


class PriceDocumentType(str, enum.Enum):
    """Назначение ценового документа (docs/PHASE2_PHASE3_ADAPTATION_PLAN.md §4).

    `PRICE_LIST` — общий прайс поставщика, после обработки может стать
    глобально активным. `SUPPLIER_QUOTE` — разовый счёт/КП: действует только
    для связанной заявки и в общий каталог активных версий не попадает.
    """

    price_list = "PRICE_LIST"
    supplier_quote = "SUPPLIER_QUOTE"


class PriceImportReviewStatus(str, enum.Enum):
    draft = "draft"
    validated = "validated"
    applying = "applying"
    applied = "applied"
    superseded = "superseded"


class PriceImportReviewSectionStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    validated = "validated"
    accepted = "accepted"
    rejected = "rejected"


class ParseRuleStatus(str, enum.Enum):
    draft = "draft"
    active = "active"
    deprecated = "deprecated"


class ParseRuleProvenance(str, enum.Enum):
    auto = "auto"
    # A model may propose a recipe, but it remains a draft and is deliberately
    # excluded from automatic rule matching until a person activates it.
    model_proposed = "model_proposed"
    user_confirmed = "user_confirmed"
    manual = "manual"


class RuleMatch(str, enum.Enum):
    exact = "exact"
    partial = "partial"
    none = "none"


class PriceKind(str, enum.Enum):
    wholesale = "wholesale"
    retail = "retail"
    dealer = "dealer"
    purchase = "purchase"
    other = "other"


class VatMode(str, enum.Enum):
    included = "included"
    excluded = "excluded"
    unknown = "unknown"


class IssueSeverity(str, enum.Enum):
    error = "error"
    warning = "warning"


# База объявлена в app.db (шаг миграции 02): общий метаданные для прайс-контура
# фазы 2 и модулей фазы 3, чтобы FK между контурами разрешались единым графом.
# Здесь она реэкспортируется для обратной совместимости импортов `app.models`.
Base = Base  # noqa: PLW0127 — намеренный реэкспорт


class ServiceClient(Base):
    """External integration identity; the full API key is never persisted."""

    __tablename__ = "service_clients"
    __table_args__ = (
        Index(
            "uq_service_clients_single_internal",
            "is_internal",
            unique=True,
            postgresql_where=text("is_internal"),
            sqlite_where=text("is_internal = 1"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # Internal clients provide a durable accounting/idempotency scope for the
    # browser UI. They never receive a bearer key and are hidden from the
    # external service-client lifecycle API.
    is_internal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    requests_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    max_concurrent_imports: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    max_input_tokens_per_import: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=5_000_000
    )
    max_output_tokens_per_import: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1_000_000
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Project(Base):
    """Operator workspace boundary for prices, items and category directories."""

    __tablename__ = "projects"
    __table_args__ = (
        Index(
            "uq_projects_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default = 1"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Supplier(Base):
    __tablename__ = "suppliers"
    __table_args__ = (
        UniqueConstraint(
            "service_client_id",
            "external_id",
            name="uq_suppliers_service_client_external_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    service_client_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("service_clients.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    versions: Mapped[list["PriceVersion"]] = relationship(back_populates="supplier")
    categories: Mapped[list["SupplierCategory"]] = relationship(
        back_populates="supplier", cascade="all, delete-orphan"
    )


class GlobalCategory(Base):
    """Reviewed, supplier-independent category hierarchy.

    ``normalized_name`` is only a technical duplicate key.  It deliberately
    does not attempt semantic synonym matching: that decision lives in the
    explicit SupplierCategoryMapping table and therefore remains reviewable.
    """

    __tablename__ = "global_categories"
    __table_args__ = (
        UniqueConstraint(
            "parent_id", "normalized_name", name="uq_global_categories_parent_name"
        ),
        Index(
            "uq_global_categories_root_name",
            "project_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("parent_id IS NULL"),
            sqlite_where=text("parent_id IS NULL"),
        ),
        CheckConstraint(
            "status IN ('active','deprecated')", name="ck_global_categories_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
        default=ORIGINAL_PROJECT_ID,
        index=True,
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("global_categories.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(512), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    parent: Mapped["GlobalCategory | None"] = relationship(
        remote_side="GlobalCategory.id", back_populates="children"
    )
    children: Mapped[list["GlobalCategory"]] = relationship(back_populates="parent")
    supplier_mappings: Mapped[list["SupplierCategoryMapping"]] = relationship(
        back_populates="global_category"
    )


class SupplierCategory(Base):
    """One category path as supplied by a particular supplier."""

    __tablename__ = "supplier_categories"
    # PostgreSQL identity is a bounded unique expression index over
    # (supplier_id, md5(normalized_path)); supplier text is never truncated.

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
        default=ORIGINAL_PROJECT_ID,
        index=True,
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("supplier_categories.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_path: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("price_versions.id", ondelete="SET NULL"), nullable=True
    )
    last_seen_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("price_versions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    supplier: Mapped[Supplier] = relationship(back_populates="categories")
    parent: Mapped["SupplierCategory | None"] = relationship(
        remote_side="SupplierCategory.id", back_populates="children"
    )
    children: Mapped[list["SupplierCategory"]] = relationship(back_populates="parent")
    aliases: Mapped[list["SupplierCategoryAlias"]] = relationship(
        back_populates="supplier_category", cascade="all, delete-orphan", lazy="selectin"
    )
    mapping: Mapped["SupplierCategoryMapping | None"] = relationship(
        back_populates="supplier_category", cascade="all, delete-orphan", uselist=False
    )


class SupplierCategoryAlias(Base):
    """Exact source spelling observed in a price list."""

    __tablename__ = "supplier_category_aliases"
    # PostgreSQL identity is a unique expression index over
    # (supplier_category_id, md5(source_path)); indexing the full provenance
    # Text can exceed the B-tree row-size limit.

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    supplier_category_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_categories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("price_versions.id", ondelete="SET NULL"), nullable=True
    )
    last_seen_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("price_versions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    supplier_category: Mapped[SupplierCategory] = relationship(back_populates="aliases")


class SupplierCategoryMapping(Base):
    """Explicit current mapping from supplier wording to the global directory."""

    __tablename__ = "supplier_category_mappings"
    __table_args__ = (
        UniqueConstraint(
            "supplier_category_id", name="uq_supplier_category_mappings_supplier_category"
        ),
        CheckConstraint(
            "mapping_source IN ('manual','import','model')",
            name="ck_supplier_category_mappings_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    supplier_category_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_categories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    global_category_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("global_categories.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    mapping_source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    supplier_category: Mapped[SupplierCategory] = relationship(back_populates="mapping")
    global_category: Mapped[GlobalCategory] = relationship(
        back_populates="supplier_mappings"
    )


class PriceFile(Base):
    __tablename__ = "price_files"
    __table_args__ = (
        CheckConstraint(
            "source IN ('UPLOAD', 'BITRIX', 'API')",
            name="ck_price_files_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
        default=ORIGINAL_PROJECT_ID,
        index=True,
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mime: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Откуда взят файл: ручная загрузка или скачивание из сделки Bitrix24.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="UPLOAD")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PriceVersion(Base):
    __tablename__ = "price_versions"
    __table_args__ = (
        UniqueConstraint("supplier_id", "version_no"),
        CheckConstraint(
            "document_type IN ('PRICE_LIST', 'SUPPLIER_QUOTE')",
            name="ck_price_versions_document_type",
        ),
        CheckConstraint(
            "(document_type = 'PRICE_LIST' AND check_batch_id IS NULL) OR "
            "(document_type = 'SUPPLIER_QUOTE' AND check_batch_id IS NOT NULL)",
            name="ck_price_versions_document_scope",
        ),
        CheckConstraint(
            "NOT (document_type = 'SUPPLIER_QUOTE' AND status = 'active')",
            name="ck_price_versions_quote_not_active",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    supplier_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suppliers.id"), nullable=False)
    price_file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("price_files.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=VersionStatus.uploaded.value)
    #: Назначение: общий прайс или разовый счёт/КП заявки.
    document_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PriceDocumentType.price_list.value
    )
    #: Разовый счёт/КП действует только в своей заявке; у общего прайса пусто.
    check_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("check_batches.id", ondelete="SET NULL"), nullable=True
    )
    mapping: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Bounded, review-only model request prepared when deterministic mapping
    # cannot identify the mandatory name+price roles.  It is never activated
    # as a rule without validation and human confirmation.
    rule_generation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warnings_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Parse-rule linkage: which rule (if any) parsed this version and how it
    # was matched. On a partial match parse_rule_id points at the candidate
    # rule whose recipe prefilled the mapping.
    parse_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("parse_rules.id"), nullable=True
    )
    rule_match: Mapped[str | None] = mapped_column(String(16), nullable=True)
    loader: Mapped[str | None] = mapped_column(String(16), nullable=True)

    supplier: Mapped[Supplier] = relationship(back_populates="versions")
    price_file: Mapped[PriceFile] = relationship()


class PriceImport(Base):
    """Durable public job that orchestrates one immutable price version."""

    __tablename__ = "price_imports"
    __table_args__ = (
        UniqueConstraint(
            "service_client_id", "idempotency_key", name="uq_price_imports_client_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    service_client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("service_clients.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    price_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_files.id", ondelete="RESTRICT"), nullable=False
    )
    price_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_versions.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PriceImportStatus.queued.value, index=True
    )
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rule_batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    locale: Mapped[str] = mapped_column(String(32), nullable=False, default="ru-RU")
    currency_hint: Mapped[str | None] = mapped_column(String(8), nullable=True)
    vat_rate_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    callback_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy: Mapped[str] = mapped_column(String(16), nullable=False, default="strict")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    completeness_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    telemetry_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    loader: Mapped[str | None] = mapped_column(String(16), nullable=True)
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    parse_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("parse_rules.id", ondelete="SET NULL"), nullable=True
    )
    parse_rule_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class PriceImportEvent(Base):
    __tablename__ = "price_import_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    price_import_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_imports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PriceImportWebhookDelivery(Base):
    __tablename__ = "price_import_webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("price_import_id", name="uq_import_webhook_terminal_delivery"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    price_import_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_imports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    callback_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class PriceImportReview(Base):
    """Revisioned, human-authored correction bound to one immutable import."""

    __tablename__ = "price_import_reviews"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_price_import_reviews_revision"),
        CheckConstraint(
            "last_applied_revision IS NULL OR last_applied_revision <= revision",
            name="ck_price_import_reviews_applied_revision",
        ),
        CheckConstraint(
            "status IN ('draft','validated','applying','applied','superseded')",
            name="ck_price_import_reviews_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    price_import_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_imports.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PriceImportReviewStatus.draft.value
    )
    mapping: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    rule_recipe: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    rule_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    parse_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("parse_rules.id", ondelete="SET NULL"), nullable=True, index=True
    )
    section_decisions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    currency_override: Mapped[str | None] = mapped_column(String(8), nullable=True)
    validation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_applied_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class PriceImportReviewEvent(Base):
    """Content-free append-only audit for review mutations and actions."""

    __tablename__ = "price_import_review_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    price_import_review_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_import_reviews.id", ondelete="CASCADE"), nullable=False, index=True
    )
    price_import_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_imports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PriceImportReviewSection(Base):
    """Small durable queue row for one immutable extractor section."""

    __tablename__ = "price_import_review_sections"
    __table_args__ = (
        UniqueConstraint(
            "price_import_review_id",
            "section_id",
            name="uq_price_import_review_sections_identity",
        ),
        CheckConstraint(
            "status IN ('pending','in_progress','validated','accepted','rejected')",
            name="ck_price_import_review_sections_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    price_import_review_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_import_reviews.id", ondelete="CASCADE"), nullable=False, index=True
    )
    price_import_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_imports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    section_id: Mapped[str] = mapped_column(String(96), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PriceImportReviewSectionStatus.pending.value, index=True
    )
    source_context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sheet_name: Mapped[str] = mapped_column(String(255), nullable=False)
    section_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    start_row_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_row_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Currency(Base):
    """ISO 4217 currency справочник.

    ``is_default`` marks the project-wide fallback currency (RUB) applied by
    the money gate when a price has no source evidence and no request hint.
    """

    __tablename__ = "currencies"
    __table_args__ = (
        UniqueConstraint("code", name="uq_currencies_code"),
        CheckConstraint("code = upper(code)", name="ck_currencies_code_upper"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(3), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(8), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class SupplierPriceItem(Base):
    __tablename__ = "supplier_price_items"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    price_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_versions.id"), nullable=False, index=True
    )
    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # These are source-derived strings. A supplier can put a verbose code,
    # sheet label, or unit text in a column that otherwise looks structured;
    # do not truncate a price row merely because such a value exceeds an
    # arbitrary VARCHAR limit.
    source_article: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_article: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    unit_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    supplier_category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("supplier_categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Operator-assigned override: when set it wins over the supplier-derived
    # category so positions from "flat" price lists (no sections) can still be
    # classified by hand or by the auto-classifier.
    manual_category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("global_categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    dimensions: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_sheet: Mapped[str] = mapped_column(Text, nullable=False)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False)
    source_section_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    source_panel_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_column_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_column_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Raw source evidence captured at parse time as [column_index, cell_text]
    # pairs, so a reviewer can verify this position against the original
    # document. ``source_category_cells`` is a list of such rows.
    source_header_cells: Mapped[list | None] = mapped_column(JSON, nullable=True)
    source_data_cells: Mapped[list | None] = mapped_column(JSON, nullable=True)
    source_category_cells: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 1-based source rows whose price tiers were glued into this item
    # (merged-fill name ladders); completeness gates treat them as covered.
    glued_source_rows: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    values: Mapped[list["SupplierPriceValue"]] = relationship(
        back_populates="item", cascade="all, delete-orphan", lazy="selectin"
    )
    attribute_values: Mapped[list["SupplierPriceItemAttributeValue"]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="SupplierPriceItemAttributeValue.source_column_index, "
        "SupplierPriceItemAttributeValue.ordinal, SupplierPriceItemAttributeValue.created_at",
    )
    supplier_category: Mapped[SupplierCategory | None] = relationship(lazy="selectin")
    manual_category: Mapped[GlobalCategory | None] = relationship(lazy="selectin")


class SupplierPriceValue(Base):
    __tablename__ = "supplier_price_values"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_price_items.id"), nullable=False, index=True
    )
    price_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    currency_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("currencies.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    qualifier: Mapped[str] = mapped_column(String(16), nullable=False, default="exact")
    amount_from: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    amount_to: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency_source: Mapped[str] = mapped_column(
        String(24), nullable=False, default="unresolved"
    )
    basis_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    basis_unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_column_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vat_mode: Mapped[str] = mapped_column(String(16), nullable=False, default=VatMode.unknown.value)
    vat_rate_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    min_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    max_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    is_on_request: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    item: Mapped[SupplierPriceItem] = relationship(back_populates="values")
    currency_directory: Mapped[Currency | None] = relationship(lazy="joined")


class AttributeDefinition(Base):
    """Canonical meaning and type of a supplier/product characteristic.

    A new characteristic is a row in this dictionary, never a new column in
    ``supplier_price_items``.  ``value_kind`` uses a portable CHECK instead
    of a PostgreSQL ENUM: the latter cannot represent a union of scalar
    types, and a check keeps SQLite-based tests and future type changes safe.
    """

    __tablename__ = "attribute_definitions"
    __table_args__ = (
        UniqueConstraint("code", name="uq_attribute_definitions_code"),
        # Required by the composite FK from a fact to its declared type.
        UniqueConstraint("id", "value_kind", name="uq_attribute_definitions_id_kind"),
        CheckConstraint(
            "value_kind IN ('text', 'integer', 'decimal', 'boolean', 'date', 'datetime', 'option')",
            name="ck_attribute_definitions_value_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(160), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    value_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    canonical_unit: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ``draft`` is intentionally the default for source-specific automatically
    # discovered headers; it can later be mapped to a reviewed canonical code.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    is_multivalued: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    source_keys: Mapped[list["SupplierAttributeKey"]] = relationship(
        back_populates="attribute_definition"
    )
    values: Mapped[list["SupplierPriceItemAttributeValue"]] = relationship(
        back_populates="attribute_definition",
        foreign_keys="SupplierPriceItemAttributeValue.attribute_definition_id",
    )
    options: Mapped[list["AttributeOption"]] = relationship(
        back_populates="attribute_definition", cascade="all, delete-orphan"
    )


class SupplierAttributeKey(Base):
    """A supplier's source-header spelling mapped to an attribute definition."""

    __tablename__ = "supplier_attribute_keys"
    __table_args__ = (
        UniqueConstraint(
            "supplier_id", "normalized_header", name="uq_supplier_attribute_keys_supplier_header"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_header: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_header: Mapped[str] = mapped_column(Text, nullable=False)
    attribute_definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attribute_definitions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    attribute_definition: Mapped[AttributeDefinition] = relationship(back_populates="source_keys")
    values: Mapped[list["SupplierPriceItemAttributeValue"]] = relationship(
        back_populates="source_key"
    )


class AttributeOption(Base):
    """Allowed dynamic option for an ``option``-typed attribute definition."""

    __tablename__ = "attribute_options"
    __table_args__ = (
        UniqueConstraint(
            "attribute_definition_id", "code", name="uq_attribute_options_definition_code"
        ),
        # PostgreSQL requires this exact key to enforce that an option used by
        # a fact belongs to that fact's attribute definition.
        UniqueConstraint(
            "id", "attribute_definition_id", name="uq_attribute_options_id_definition"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    attribute_definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("attribute_definitions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(160), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    attribute_definition: Mapped[AttributeDefinition] = relationship(back_populates="options")
    values: Mapped[list["SupplierPriceItemAttributeValue"]] = relationship(
        back_populates="option", foreign_keys="SupplierPriceItemAttributeValue.option_id"
    )


class SupplierPriceItemAttributeValue(Base):
    """One existing characteristic fact from one supplier price-list cell.

    ``raw_value`` is the source evidence.  A successful normalization fills
    exactly one native scalar column; an unsuccessful explicit conversion is
    retained as ``parse_status='invalid'`` without inventing a string/object
    fallback in JSON.
    """

    __tablename__ = "supplier_price_item_attribute_values"
    __table_args__ = (
        UniqueConstraint(
            "item_id", "source_attribute_key_id", "ordinal",
            name="uq_supplier_price_item_attribute_values_item_source_ordinal",
        ),
        ForeignKeyConstraint(
            ["attribute_definition_id", "value_kind"],
            ["attribute_definitions.id", "attribute_definitions.value_kind"],
            name="fk_supplier_price_item_attribute_values_definition_kind",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["option_id", "attribute_definition_id"],
            ["attribute_options.id", "attribute_options.attribute_definition_id"],
            name="fk_supplier_price_item_attribute_values_option_definition",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "value_kind IN ('text', 'integer', 'decimal', 'boolean', 'date', 'datetime', 'option')",
            name="ck_supplier_price_item_attribute_values_kind",
        ),
        CheckConstraint(
            "parse_status IN ('parsed', 'invalid')",
            name="ck_supplier_price_item_attribute_values_parse_status",
        ),
        CheckConstraint(
            "(parse_status = 'invalid' "
            " AND value_text IS NULL AND value_integer IS NULL AND value_decimal IS NULL"
            " AND value_boolean IS NULL AND value_date IS NULL AND value_timestamp IS NULL"
            " AND option_id IS NULL)"
            " OR (parse_status = 'parsed' AND ("
            " (value_kind = 'text' AND value_text IS NOT NULL"
            "  AND value_integer IS NULL AND value_decimal IS NULL AND value_boolean IS NULL"
            "  AND value_date IS NULL AND value_timestamp IS NULL AND option_id IS NULL)"
            " OR (value_kind = 'integer' AND value_integer IS NOT NULL"
            "  AND value_text IS NULL AND value_decimal IS NULL AND value_boolean IS NULL"
            "  AND value_date IS NULL AND value_timestamp IS NULL AND option_id IS NULL)"
            " OR (value_kind = 'decimal' AND value_decimal IS NOT NULL"
            "  AND value_text IS NULL AND value_integer IS NULL AND value_boolean IS NULL"
            "  AND value_date IS NULL AND value_timestamp IS NULL AND option_id IS NULL)"
            " OR (value_kind = 'boolean' AND value_boolean IS NOT NULL"
            "  AND value_text IS NULL AND value_integer IS NULL AND value_decimal IS NULL"
            "  AND value_date IS NULL AND value_timestamp IS NULL AND option_id IS NULL)"
            " OR (value_kind = 'date' AND value_date IS NOT NULL"
            "  AND value_text IS NULL AND value_integer IS NULL AND value_decimal IS NULL"
            "  AND value_boolean IS NULL AND value_timestamp IS NULL AND option_id IS NULL)"
            " OR (value_kind = 'datetime' AND value_timestamp IS NOT NULL"
            "  AND value_text IS NULL AND value_integer IS NULL AND value_decimal IS NULL"
            "  AND value_boolean IS NULL AND value_date IS NULL AND option_id IS NULL)"
            " OR (value_kind = 'option' AND option_id IS NOT NULL"
            "  AND value_text IS NULL AND value_integer IS NULL AND value_decimal IS NULL"
            "  AND value_boolean IS NULL AND value_date IS NULL AND value_timestamp IS NULL)"
            "))",
            name="ck_supplier_price_item_attribute_values_scalar",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_price_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_attribute_key_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_attribute_keys.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    attribute_definition_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    value_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_column_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_value: Mapped[str] = mapped_column(Text, nullable=False)
    parse_status: Mapped[str] = mapped_column(String(16), nullable=False, default="parsed")
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_integer: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    value_decimal: Mapped[Decimal | None] = mapped_column(Numeric(20, 6), nullable=True)
    value_boolean: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    value_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    value_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    option_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("attribute_options.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    item: Mapped[SupplierPriceItem] = relationship(back_populates="attribute_values")
    source_key: Mapped[SupplierAttributeKey] = relationship(back_populates="values")
    attribute_definition: Mapped[AttributeDefinition] = relationship(
        back_populates="values", foreign_keys=[attribute_definition_id]
    )
    option: Mapped[AttributeOption | None] = relationship(
        back_populates="values", foreign_keys=[option_id]
    )


class ParseRule(Base):
    """Fingerprint + recipe: how to parse a price file of a known structure.

    Uniqueness of (header_hash, supplier_id, revision) is enforced in the
    application: a DB-level unique constraint would treat NULL supplier_id
    rows as always distinct, so it cannot guard global rules anyway.
    """

    __tablename__ = "parse_rules"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
        default=ORIGINAL_PROJECT_ID,
        index=True,
    )
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("suppliers.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    loader: Mapped[str] = mapped_column(String(16), nullable=False)
    header_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    fingerprint: Mapped[dict] = mapped_column(JSON, nullable=False)
    recipe: Mapped[dict] = mapped_column(JSON, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ParseRuleStatus.draft.value
    )
    provenance: Mapped[str] = mapped_column(String(32), nullable=False)
    # Plain uuid (no FK): an FK would make parse_rules <-> price_versions
    # circular, and the referenced version may be deleted independently.
    created_from_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    validation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class RuleGenerationArtifact(Base):
    """Immutable, locally verified output of one exact model work unit."""

    __tablename__ = "rule_generation_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "work_key", name="uq_rule_generation_artifacts_work_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    work_key: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    request_settings_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal: Mapped[dict] = mapped_column(JSON, nullable=False)
    validation: Mapped[dict] = mapped_column(JSON, nullable=False)
    compiler_dry_run_summary: Mapped[dict] = mapped_column(JSON, nullable=False)
    provenance: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class RuleGenerationAttempt(Base):
    """One bounded model request for one table section of a price version.

    This stores operational/audit data for the rule-generation workflow, not
    product characteristics. Product attributes remain atomic facts in
    ``supplier_price_item_attribute_values``. A separate attempt row makes
    retries and per-section review visible without overloading the version's
    legacy prompt preview field.
    """

    __tablename__ = "rule_generation_attempts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    price_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    price_import_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("price_imports.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # One UI request can make several independent model calls: one for every
    # detected header/section. ``batch_id`` groups them into one future rule.
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    section_id: Mapped[str] = mapped_column(String(96), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="openai")
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    request_settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Hash of the exact original bytes. It binds the audit context and model
    # proposal to the immutable price-file revision, not merely a sheet name.
    source_file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # The source sample is redacted and bounded. JSON is appropriate here as
    # an immutable rule-generation artifact; it is never used for a product
    # attribute value or executable code.
    input_context: Mapped[dict] = mapped_column(JSON, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    proposal: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    validation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Nullable during the first DEV-139 rollout so historical attempts remain
    # valid until a separately reviewed backfill exists.
    work_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("rule_generation_artifacts.id", ondelete="SET NULL"), nullable=True
    )
    failure_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    semantic_retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reused_from_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("rule_generation_attempts.id", ondelete="SET NULL"), nullable=True
    )
    terminal_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parse_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("parse_rules.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class RuleGenerationOperation(Base):
    """Append-only audit row for one separately billable model operation."""

    __tablename__ = "rule_generation_operations"
    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "operation_no",
            name="uq_rule_generation_operations_attempt_operation",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rule_generation_attempts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    operation_no: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_operation_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    http_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    response_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CorpusRun(Base):
    __tablename__ = "corpus_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_files: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    totals: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    report_storage_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class CorpusRunFile(Base):
    __tablename__ = "corpus_run_files"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    corpus_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("corpus_runs.id"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    loader: Mapped[str | None] = mapped_column(String(16), nullable=True)
    rule_match: Mapped[str | None] = mapped_column(String(16), nullable=True)
    parse_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("parse_rules.id"), nullable=True
    )
    price_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("price_versions.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warnings_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PriceParseIssue(Base):
    __tablename__ = "price_parse_issues"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    price_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_versions.id"), nullable=False, index=True
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    sheet: Mapped[str | None] = mapped_column(String(255), nullable=True)
    row_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    column_ref: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ColumnAliasScope(str, enum.Enum):
    """Насколько широко действует запись словаря ролей."""

    global_ = "global"          # для всех прайсов
    supplier = "supplier"       # только для прайсов одного поставщика
    price_file = "price_file"   # исключение под конкретный прайс


class ColumnAlias(Base):
    """Словарь подписей колонок: подпись -> роль (и вид цены с режимом НДС).

    Дополняет встроенный словарь `app.parsing.headers._ALIASES`, не заменяя его.
    Подпись хранится в нижнем регистре со схлопнутыми пробелами: в прайсах одна
    и та же колонка называется «Цена ОПТ», «цена опт» и «ЦЕНА ОПТ», и это одна
    запись словаря.

    Область действия задаёт `scope`: глобальная запись работает для всех прайсов,
    supplier-запись перекрывает её для одного поставщика, price_file-запись — для
    одного файла. Исключения заложены сразу: у одного поставщика «расч.» может
    значить оптовую цену, у другого — расчётную величину, и общий словарь не
    должен из-за этого ломаться.
    """

    __tablename__ = "column_aliases"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    alias: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    price_kind: Mapped[str | None] = mapped_column(String(16))
    vat_mode: Mapped[str | None] = mapped_column(String(16))
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ColumnAliasScope.global_.value
    )
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE")
    )
    price_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("price_files.id", ondelete="CASCADE")
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="human")
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint(
            "alias", "scope", "supplier_id", "price_file_id", name="uq_column_alias_scope"
        ),
    )


# ---------------------------------------------------------------------------
# Канонический слой, подбор и документация к позициям
# ---------------------------------------------------------------------------


class EmbeddingVector(TypeDecorator):
    """Вектор наименования: `vector` в PostgreSQL, JSON в SQLite.

    Боевая база — PostgreSQL с pgvector, и поиск идёт оператором расстояния.
    Тесты гоняются на SQLite, где такого типа нет; подменять на JSON честнее,
    чем держать отдельную схему для тестов и проверять не то, что работает.
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):  # noqa: ANN001 - сигнатура SQLAlchemy
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector

            return dialect.type_descriptor(Vector(EMBEDDING_DIM))
        return dialect.type_descriptor(JSON())


class MergeStatus(str, enum.Enum):
    """Как позиции поставщиков сведены в один канонический товар."""

    auto = "auto"
    confirmed = "confirmed"
    disputed = "disputed"


class MatchStage(str, enum.Enum):
    """Ступень каскада, на которой нашлось совпадение.

    Порядок значим: чем выше ступень, тем меньше догадки в результате.
    """

    synonym = "synonym"
    article = "article"
    designation = "designation"
    semantic = "semantic"
    vector = "vector"
    fuzzy = "fuzzy"
    none = "none"


class CanonicalProduct(Base):
    """Изделие производителя, а не строка прайса дилера.

    Сертификат выдан на изделие и обычно покрывает серию, поэтому документы
    привязываются сюда. Привязка к строке прайса умножалась бы на число
    поставщиков и версий и терялась бы при каждой новой загрузке.
    """

    __tablename__ = "canonical_products"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    match_key: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_title: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_article: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("global_categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    merge_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=MergeStatus.auto.value
    )
    sources_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding: Mapped[list | None] = mapped_column(EmbeddingVector, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("match_key", name="uq_canonical_products_key"),
        Index("ix_canonical_products_normalized_title", "normalized_title"),
    )


class CanonicalProductSource(Base):
    """Какая строка какого прайса вошла в канонический товар."""

    __tablename__ = "canonical_product_sources"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    canonical_product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("canonical_products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    price_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_price_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Проект дублируется из прайса намеренно: подбор фильтруется по нему на
    # каждый запрос, и join к файлу ради одного поля был бы лишним.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("price_item_id", name="uq_canonical_source_item"),
    )


class SynonymDictionary(Base):
    """«Как сказал клиент» → канонический товар.

    Пополняется выбором менеджера: одно подтверждение учит подбор для всех
    поставщиков сразу, потому что цель — канон, а не строка конкретного прайса.
    """

    __tablename__ = "synonym_dictionary"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_phrase: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_phrase: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    canonical_product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("canonical_products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    origin: Mapped[str] = mapped_column(String(30), nullable=False, default="manager_choice")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("normalized_phrase", name="uq_synonym_phrase"),
    )


class ProductDocument(Base):
    """Паспорт или сертификат: файл и его реквизиты.

    Реквизиты приходят из контура FSA (реестр Росаккредитации), файл — с диска
    Б24 или из загрузки. Вердикт здесь не хранится: он меняется сам со временем
    и запрашивается на дату расчёта.
    """

    __tablename__ = "product_documents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="certificate")
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    registration_number: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    registration_number_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="fsa_service")
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class DocumentLink(Base):
    """Связь «канонический товар → документ» с доказательством.

    `evidence` обязателен по смыслу: без него через полгода нельзя ответить,
    почему к позиции приложен именно этот файл. `series_mask` — атрибут связи,
    а не документа: один сертификат покрывает разные диапазоны у разных канонов.
    """

    __tablename__ = "document_links"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("product_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    canonical_product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("canonical_products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    matched_by: Mapped[str] = mapped_column(String(30), nullable=False, default="registry")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    series_mask: Mapped[str | None] = mapped_column(String(200), nullable=True)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint(
            "document_id", "canonical_product_id", name="uq_document_link_pair"
        ),
    )


class PriceRequest(Base):
    """Заявка клиента до подбора: свободный текст, файл или строки из сделки."""

    __tablename__ = "price_requests"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="manual")
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    client: Mapped[str] = mapped_column(Text, nullable=False, default="")
    number: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Задача №7 (docs/merge-remote/09-task7-kp-contour.md): заявка в UI —
    # это check-batch фазы 3, а `price_requests` — её расчётный лист.
    # Связь 1:1, nullable: ручные заявки P2 остаются без связки.
    check_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("check_batches.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    lines: Mapped[list["PriceRequestLine"]] = relationship(
        back_populates="request", cascade="all, delete-orphan", lazy="selectin"
    )


class PriceRequestLine(Base):
    """Строка заявки после разбора — до подбора и всегда редактируемая.

    Количество и единица отделены от наименования на этом шаге, а не в подборе:
    ТЗ требует, чтобы менеджер правил разбор до того, как система начнёт искать.
    """

    __tablename__ = "price_request_lines"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_line: Mapped[str] = mapped_column(Text, nullable=False)
    query_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 3), nullable=True)
    unit_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    matched_canonical_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("canonical_products.id", ondelete="SET NULL"), nullable=True
    )
    match_stage: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MatchStage.none.value
    )
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    request: Mapped[PriceRequest] = relationship(back_populates="lines")


class OrderEvent(Base):
    """Журнал событий заявки: кто, что и когда делал с check-batch.

    Решение №1 открытых вопросов задачи №7: `send`, `cancel-send`,
    `complete-check`, `confirm-quote` и `archive` пишутся сюда; правки КП
    живут отдельно в журнале quote-правок (там есть причина и значения).
    """

    __tablename__ = "order_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    check_batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("check_batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class QuoteAdjustment(Base):
    """Журнал ручных правок КП: НДС, скидка, поставщик — с причиной.

    Решение №5 задачи №7: актуальные значения — последняя запись по полю,
    а не колонки «в последний раз»: журнал хранит всю историю правок с
    причиной (контракт `frontend/API.md` требует причину при ручном
    изменении НДС/скидки).
    """

    __tablename__ = "quote_adjustments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Правимое поле: `vat_rate` | `special_discount` | `supplier`.
    field: Mapped[str] = mapped_column(String(20), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class OrderFile(Base):
    """Файл заявки: зарегистрированное имя и роль.

    Бинарные данные через эту сущность не гоняются: файл загружается
    каноническим `POST /uploads`, а здесь заявка запоминает его имя и
    роль (контракт `frontend/API.md`: `{ fileName, kind }`).
    """

    __tablename__ = "order_files"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    #: specification | passport | price | other.
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="other")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ItemEmbedding(Base):
    """Вектор поискового текста позиции прайса.

    Отдельная таблица, а не колонка у позиции: индекс собирается по версиям
    (новый прайс расширяет его, изменённый — пересобирает свою часть), а смена
    модели эмбеддера должна добавлять параллельный набор векторов, не переписывая
    исторические строки. Поэтому ключ уникальности — позиция плюс тройка
    «модель, ревизия, версия нормализатора».
    """

    __tablename__ = "item_embeddings"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    supplier_price_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("supplier_price_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Версия и поставщик продублированы намеренно: по ним идут и фильтры
    # подбора, и удаление при пересборке, и join к позиции ради них не нужен.
    price_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("price_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )

    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    normalizer_version: Mapped[str] = mapped_column(String(32), nullable=False)

    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    embedding: Mapped[list | None] = mapped_column(EmbeddingVector, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint(
            "supplier_price_item_id",
            "model_name",
            "model_revision",
            "normalizer_version",
            name="uq_item_embedding_model",
        ),
        Index("ix_item_embeddings_version_model", "price_version_id", "model_name"),
    )


class DiscountScope(str, enum.Enum):
    """Уровень действия скидки — от общего к конкретному.

    Порядок важен: по ТЗ конкретная скидка перекрывает общую, а не
    складывается с ней.
    """

    supplier = "supplier"
    category = "category"
    article = "article"
    volume = "volume"


class DiscountRule(Base):
    """Правило скидки поставщика.

    Скидки заказчика описываются четырьмя видами: процент по поставщику, по
    категории, по конкретному артикулу и объёмная от суммы заказа. Правило
    хранится одной таблицей с разными уровнями, а не четырьмя таблицами:
    выбор между ними — это сравнение уровней, и держать их врозь значит
    собирать сравнение в коде на каждый расчёт.
    """

    __tablename__ = "discount_rules"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        default=ORIGINAL_PROJECT_ID,
        index=True,
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)

    category_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("global_categories.id", ondelete="CASCADE"), nullable=True, index=True
    )
    normalized_article: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    # Порог для объёмной скидки: сумма позиций этого поставщика в расчёте.
    min_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)

    percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        CheckConstraint("percent >= 0 AND percent <= 100", name="ck_discount_percent"),
        Index("ix_discount_rules_supplier_scope", "supplier_id", "scope"),
    )


class ProductPhysicalReference(Base):
    """Управляемый справочник физических параметров с приоритетом над прайсом."""

    __tablename__ = "product_physical_reference"
    __table_args__ = (
        UniqueConstraint("project_id", "match_key", name="uq_physical_reference_project_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    match_key: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_article: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    volume_m3: Mapped[Decimal | None] = mapped_column(Numeric(14, 9), nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="manual_import")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Calculation(Base):
    """Расчёт по заявке: набор позиций с ценами, скидками и правками.

    Отдельная сущность от заявки: одна заявка может пересчитываться несколько
    раз (сменился прайс, менеджер выбрал другое исполнение), и каждая версия
    расчёта должна оставаться воспроизводимой.
    """

    __tablename__ = "calculations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        default=ORIGINAL_PROJECT_ID,
        index=True,
    )
    request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("price_requests.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    items: Mapped[list["CalculationItem"]] = relationship(
        back_populates="calculation", cascade="all, delete-orphan", lazy="selectin"
    )
    request: Mapped["PriceRequest | None"] = relationship(lazy="selectin")


class CalculationItem(Base):
    """Строка расчёта: выбранная позиция прайса, количество, цена и скидка.

    Цена поставщика и цена после скидки хранятся раздельно, а ручная правка —
    третьим полем. Слить их в одно значило бы потерять ответ на вопрос «почему
    в КП именно столько», а он возникает первым при разборе с клиентом.
    """

    __tablename__ = "calculation_items"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    calculation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("calculations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    source_line: Mapped[str] = mapped_column(Text, nullable=False, default="")
    price_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("supplier_price_items.id", ondelete="SET NULL"), nullable=True
    )
    canonical_product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("canonical_products.id", ondelete="SET NULL"), nullable=True
    )
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    supplier_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False, default=1)
    unit_code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Снимок источника цены: строка расчёта должна оставаться воспроизводимой
    # и после смены активного прайса, и после закрытия разового счёта заявки.
    # `price_document_type` отличает общий прайс от разового счёта/КП, а
    # `price_source` — ручную загрузку от Bitrix24.
    price_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("price_versions.id", ondelete="SET NULL"), nullable=True
    )
    price_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("price_files.id", ondelete="SET NULL"), nullable=True
    )
    price_document_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    price_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    price_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    price_tier_min_quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 3), nullable=True
    )
    price_tier_max_quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 3), nullable=True
    )

    base_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True)
    volume_m3: Mapped[Decimal | None] = mapped_column(Numeric(14, 9), nullable=True)
    weight_source: Mapped[str] = mapped_column(String(24), nullable=False, default="")
    volume_source: Mapped[str] = mapped_column(String(24), nullable=False, default="")
    certificate_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    certificate_valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    certificate_confirmed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    certificate_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    discount_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=0
    )
    discount_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("discount_rules.id", ondelete="SET NULL"), nullable=True
    )
    discount_source: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    price_after_discount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)

    vat_rate_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    vat_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    price_without_vat: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    vat_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    price_with_vat: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)

    line_total_without_vat: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    line_vat_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    line_total_with_vat: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)

    # Ручная правка живёт отдельно от расчётной цены: пересчёт скидок её не
    # затирает, а в выгрузке видно и расчётную цену, и решение менеджера.
    manual_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    manual_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    manual_by: Mapped[str] = mapped_column(Text, nullable=False, default="")
    manual_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    calculation: Mapped[Calculation] = relationship(back_populates="items")


class PriceOverrideLog(Base):
    """Журнал ручных правок цены — append-only.

    ТЗ требует не просто хранить последнюю цену, а фиксировать каждую правку с
    причиной: «ручная корректировка цены с причиной → лог в БД». Перезапись
    поля этого не даёт, поэтому журнал отдельной таблицей.
    """

    __tablename__ = "price_override_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    calculation_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("calculation_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    previous_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    new_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    changed_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
