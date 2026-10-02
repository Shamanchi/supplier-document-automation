"""Журнал событий прогона — источник SSE и след аудита."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Text, func, text
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB as PgJSONB

#: Шаг миграции 03 (решение №4 шага 02, docs/migration/PROGRESS.md):
#: тесты фазы 2 поднимают схему в SQLite, поэтому JSONB получает вариант.
JSONB = PgJSONB().with_variant(JSON(), "sqlite")
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PipelineEvent(Base):
    """Неизменяемое событие прогона.

    `id` — BIGSERIAL: контракт SSE (check-items §SSE) требует монотонный курсор,
    по которому клиент после обрыва добирает пропущенное. UUID такого порядка не
    даёт, поэтому здесь целочисленная последовательность.
    """

    __tablename__ = "pipeline_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    check_item_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("check_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)
    progress_current: Mapped[int | None] = mapped_column(Integer)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    progress_unit: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(
        # Шаг миграции 03: явный ::jsonb-каст не парсится SQLite-DDL при
        # create_all в тестах фазы 2; для jsonb-колонки PG '{}'-литерал
        # приводит неявно, семантика не меняется.
        JSONB, nullable=False, server_default=text("'{}'")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
