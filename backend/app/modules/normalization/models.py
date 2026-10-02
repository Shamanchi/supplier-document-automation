"""ORM models owned by deterministic parameter normalization."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB as PgJSONB

#: Шаг миграции 03 (решение №4 шага 02, docs/migration/PROGRESS.md):
#: тесты фазы 2 поднимают схему в SQLite, поэтому JSONB получает вариант.
JSONB = PgJSONB().with_variant(JSON(), "sqlite")
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import pg_enum
from app.db import Base


class NormalizedParameter(Base):
    __tablename__ = "normalized_parameters"
    __table_args__ = (
        ForeignKeyConstraint(
            ["catalog_version_id", "canonical_unit_id"],
            ["units.catalog_version_id", "units.id"],
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    raw_parameter_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("raw_parameters.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("catalog_versions.id"), nullable=False
    )
    canonical_parameter_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("canonical_parameters.id")
    )
    canonical_name: Mapped[str | None] = mapped_column(Text)
    value_kind: Mapped[str] = mapped_column(pg_enum("value_kind"), nullable=False)
    typed_value: Mapped[dict | None] = mapped_column(JSONB)
    canonical_unit_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    normalized_ok: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class NormalizationIssue(Base):
    __tablename__ = "normalization_issues"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    normalized_parameter_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("normalized_parameters.id", ondelete="CASCADE"),
        nullable=False,
    )
    issue_type: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(
        pg_enum("issue_severity"), nullable=False, server_default="WARNING"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
