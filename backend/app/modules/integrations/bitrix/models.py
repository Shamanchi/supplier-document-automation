"""ORM-модель `integration_jobs`.

Таблица есть в schema.sql с самого начала, модели до сих пор не было: внешних
операций никто не выполнял. `external_links` и `tender_refs` принадлежат модулю
`identity` и читаются оттуда по импорту.

Статус задания и статус документа/отчёта разделены намеренно: неудачная
публикация не должна менять состояние локального отчёта — контракт требует,
чтобы локальный артефакт оставался доступен при провале публикации.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB as PgJSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.core.enums import pg_enum
from app.db import Base

#: Шаг миграции 04: SQLite-вариант JSONB (правило шага 03 — P2-тесты поднимают
#: схему в create_all на SQLite; см. решение №3 в docs/migration/PROGRESS.md).
JSONB = PgJSONB().with_variant(JSON(), "sqlite")

#: Статусы, из которых задание уже не двинется само.
TERMINAL_STATUSES = ("COMPLETED", "FAILED", "CANCELLED")

#: Статусы, допускающие ручной retry (контракт §Retry policy).
RETRYABLE_STATUSES = ("FAILED", "RETRY_SCHEDULED")


class IntegrationJob(Base):
    """Состояние внешней операции: импорт файла или публикация отчёта."""

    __tablename__ = "integration_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    type: Mapped[str] = mapped_column(pg_enum("integration_job_type"), nullable=False)
    status: Mapped[str] = mapped_column(
        pg_enum("integration_job_status"), nullable=False, server_default="QUEUED"
    )
    tender_id: Mapped[str | None] = mapped_column(Text)
    check_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("check_batches.id", ondelete="SET NULL")
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Параметры запроса без секретов и auth-заголовков (комментарий к таблице).
    request_meta: Mapped[dict | None] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[dict | None] = mapped_column(JSONB)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Момент терминального статуса. `updated_at` меняется и при ретрае, поэтому
    #: «когда закончилось» по нему определить нельзя.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
