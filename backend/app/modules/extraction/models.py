"""ORM models owned by the document extraction contour."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    func,
    text,
)
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB as PgJSONB

#: Шаг миграции 03 (решение №4 шага 02, docs/migration/PROGRESS.md):
#: тесты фазы 2 поднимают схему в SQLite, поэтому JSONB получает вариант.
JSONB = PgJSONB().with_variant(JSON(), "sqlite")
#: ARRAY(Text) в SQLite рендерится как JSON — то же решение, что и для JSONB.
ArrayText = ARRAY(Text).with_variant(JSON(), "sqlite")
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import pg_enum
from app.db import Base


class DocumentPage(Base):
    __tablename__ = "document_pages"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    page_no: Mapped[int] = mapped_column(Integer)
    dpi: Mapped[int | None] = mapped_column(Integer)
    width_px: Mapped[int | None] = mapped_column(Integer)
    height_px: Mapped[int | None] = mapped_column(Integer)
    text_hash: Mapped[str | None] = mapped_column(Text)
    document_type: Mapped[str | None] = mapped_column(pg_enum("document_type"))
    ocr_needed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quality_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    active_layer: Mapped[str | None] = mapped_column(Text)
    parser_version: Mapped[str | None] = mapped_column(Text)


class SourceFragment(Base):
    __tablename__ = "source_fragments"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    page_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("document_pages.id")
    )
    method: Mapped[str] = mapped_column(pg_enum("fragment_method"))
    fragment_raw_text: Mapped[str] = mapped_column(Text)
    normalized_text: Mapped[str | None] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(Text)
    locator_kind: Mapped[str] = mapped_column(pg_enum("locator_kind"))
    locator: Mapped[dict] = mapped_column(JSONB)
    parser_version: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DocumentTable(Base):
    __tablename__ = "document_tables"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    source_fragment_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("source_fragments.id", ondelete="CASCADE")
    )
    structure: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("pipeline_runs.id", ondelete="CASCADE")
    )
    check_item_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("check_items.id", ondelete="CASCADE")
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE")
    )
    method: Mapped[str] = mapped_column(pg_enum("extraction_method"))
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    usage: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, default="QUEUED")
    pipeline_version: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    worker_token: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RawParameter(Base):
    __tablename__ = "raw_parameters"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    check_item_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("check_items.id", ondelete="CASCADE")
    )
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("extraction_runs.id", ondelete="SET NULL")
    )
    role: Mapped[str] = mapped_column(pg_enum("document_role"))
    name_text: Mapped[str] = mapped_column(Text)
    value_text: Mapped[str | None] = mapped_column(Text)
    unit_text: Mapped[str | None] = mapped_column(Text)
    extraction_method: Mapped[str] = mapped_column(pg_enum("extraction_method"))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    quality_flags: Mapped[list[str]] = mapped_column(ArrayText, default=list)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    #: Поколение значения. Отдельно от `version`: тот растёт и при exclude/restore,
    #: а evidence устаревает только когда меняется само значение.
    value_revision: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ExtractionEvidence(Base):
    __tablename__ = "extraction_evidence"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    raw_parameter_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("raw_parameters.id", ondelete="CASCADE")
    )
    source_fragment_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("source_fragments.id")
    )
    quote: Mapped[str] = mapped_column(Text)
    quote_start: Mapped[int | None] = mapped_column(Integer)
    quote_end: Mapped[int | None] = mapped_column(Integer)
    value_start: Mapped[int | None] = mapped_column(Integer)
    value_end: Mapped[int | None] = mapped_column(Integer)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Какое поколение значения подтверждает цитата. Таблица неизменяема, поэтому
    #: устаревшее evidence не правится, а отличается номером поколения.
    raw_parameter_value_revision: Mapped[int] = mapped_column(
        Integer, default=1, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SourceVerification(Base):
    __tablename__ = "source_verifications"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    extraction_evidence_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("extraction_evidence.id", ondelete="CASCADE"),
    )
    passed: Mapped[bool] = mapped_column(Boolean)
    method: Mapped[str] = mapped_column(pg_enum("verification_method"))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    verifier_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ExtractionPreset(Base):
    """User-managed parse rule for technical-document extraction."""

    __tablename__ = "extraction_presets"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    doc_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="passport")
    columns: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    labels: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    section_keywords: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    header_keywords: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    units: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")
    include_all: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    keyvalue: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    fingerprint: Mapped[str | None] = mapped_column(Text)
    builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
