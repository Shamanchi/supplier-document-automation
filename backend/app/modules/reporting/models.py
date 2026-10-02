"""ORM-модели модуля `reporting`.

Владение таблицами — по docs/architecture/BACKEND_MODULE_MAP.md.

`report_snapshots` защищён триггером `prevent_modify`: строка неизменяема с
момента вставки. Отсюда важное следствие для сервиса — snapshot создаётся
**после** рендеринга файлов, уже с итоговым статусом; «QUEUED → COMPLETED»
переходом по строке не выразить.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, func, text
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB as PgJSONB

#: Шаг миграции 03 (решение №4 шага 02, docs/migration/PROGRESS.md):
#: тесты фазы 2 поднимают схему в SQLite, поэтому JSONB получает вариант.
JSONB = PgJSONB().with_variant(JSON(), "sqlite")
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import pg_enum
from app.db import Base


class ReportSnapshot(Base):
    """Неизменяемый набор данных отчёта.

    Файл всегда собирается из snapshot, а не из живых таблиц: отчёт, выданный
    заказчику, обязан воспроизводиться байт в байт, даже если проверку потом
    пересчитали.
    """

    __tablename__ = "report_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    check_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("check_batches.id", ondelete="CASCADE")
    )
    check_item_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("check_items.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(pg_enum("report_status"), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False, server_default="ru")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ReportArtifact(Base):
    """Файл отчёта: формат, ключ в хранилище, размер и checksum."""

    __tablename__ = "report_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    report_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("report_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    format: Mapped[str] = mapped_column(pg_enum("report_format"), nullable=False)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    checksum: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
