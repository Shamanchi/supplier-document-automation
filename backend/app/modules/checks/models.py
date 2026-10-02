"""ORM-модели модуля `checks`.

Владение таблицами — по docs/architecture/BACKEND_MODULE_MAP.md: `check_batches`,
`check_items`, `pipeline_runs`, `stage_runs` принадлежат этому модулю. Другие
модули их читают (так делает `dashboard`), но не пишут.

`item_verdicts` читается отсюда (`summary` пакета показывает вердикты), но
принадлежит модулю `verdicts` — модель живёт там.

Версия каталога и типы оборудования принадлежат модулю `catalogs` — читаются
оттуда по импорту, как `dashboard` читает модели этого модуля.

Схема создаётся миграцией из schema.sql; модели описывают только те колонки,
которые нужны коду.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB as PgJSONB

#: Шаг миграции 03 (решение №4 шага 02, docs/migration/PROGRESS.md):
#: тесты фазы 2 поднимают схему в SQLite, поэтому JSONB получает вариант.
JSONB = PgJSONB().with_variant(JSON(), "sqlite")
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import pg_enum
from app.db import Base

#: Статусы пакета, в которых допустима правка метаданных и состава позиций.
BATCH_EDITABLE_STATUSES = ("DRAFT",)

#: Статусы позиции, из которых позицию можно запустить.
ITEM_RUNNABLE_STATUSES = ("DRAFT", "NEEDS_INPUT", "FAILED", "CANCELLED")

#: Статусы позиции, означающие незавершённую работу: их отменяет `cancel`.
ITEM_ACTIVE_STATUSES = (
    "QUEUED",
    "INGESTING",
    "NEEDS_INPUT",
    "EXTRACTING",
    "VERIFYING_SOURCES",
    "NORMALIZING",
    "AWAITING_PARAMETER_REVIEW",
    "MATCHING",
)

#: Прогоны, которые ещё не закончились.
RUN_ACTIVE_STATUSES = ("QUEUED", "RUNNING")

#: Максимум позиций в пакете (schema.sql: ck_item_position_range).
MAX_ITEMS_PER_BATCH = 5

#: Все значения check_batch_status в порядке FSM.
DB_BATCH_STATUSES: tuple[str, ...] = (
    "DRAFT",
    "QUEUED",
    "RUNNING",
    "REVIEW_REQUIRED",
    "COMPLETED",
    "CANCELLED",
    "ARCHIVED",
)

#: Статус БД → укрупнённый статус экрана Orders (решение 2026-08-10). В базе FSM
#: хранит точное состояние прогона, экрану нужна укрупнённая картина.
STATUS_TO_API: dict[str, str] = {
    "DRAFT": "DRAFT",
    "QUEUED": "ACTIVE",
    "RUNNING": "ACTIVE",
    "REVIEW_REQUIRED": "CONFIRMATION",
    "COMPLETED": "CLOSED",
    "CANCELLED": "CLOSED",
    "ARCHIVED": "ARCHIVED",
}

#: Обратное отображение для фильтра `?status=`. Собирается из STATUS_TO_API,
#: чтобы два отображения не разъезжались.
STATUS_FILTER_TO_DB: dict[str, tuple[str, ...]] = {
    api_status: tuple(db for db, api in STATUS_TO_API.items() if api == api_status)
    for api_status in dict.fromkeys(STATUS_TO_API.values())
}


class CheckBatch(Base):
    """Пакет проверки сделки: 1..5 позиций, optimistic lock через `version`."""

    __tablename__ = "check_batches"
    __table_args__ = (
        UniqueConstraint("tender_ref_id", name="uq_check_batches_tender_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tender_ref_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tender_refs.id")
    )
    title: Mapped[str | None] = mapped_column(Text)
    customer: Mapped[str | None] = mapped_column(Text)
    supplier: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(pg_enum("check_batch_status"), nullable=False)
    #: Расчётный вердикт заявки: агрегат по вердиктам позиций.
    final_verdict: Mapped[str] = mapped_column(pg_enum("verdict_value"), nullable=False)
    #: Решение менеджера по заявке — отдельно от расчёта, как и у позиции.
    manager_verdict: Mapped[str] = mapped_column(
        pg_enum("verdict_value"), nullable=False, server_default="PENDING"
    )
    verdict_basis: Mapped[dict | None] = mapped_column(JSONB)
    verdict_comment: Mapped[str | None] = mapped_column(Text)
    verdict_approved_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id")
    )
    verdict_approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    #: Свой optimistic lock: пересчёт вердикта не должен ронять `If-Match` у
    #: клиента, который правит состав заявки.
    verdict_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list[CheckItem]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="CheckItem.position_no",
    )

    @property
    def is_editable(self) -> bool:
        return self.status in BATCH_EDITABLE_STATUSES


class CheckItem(Base):
    """Позиция = пара «ТЗ ↔ документ одного поставщика»."""

    __tablename__ = "check_items"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("check_batches.id", ondelete="CASCADE"),
        nullable=False,
    )
    position_no: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(pg_enum("check_item_status"), nullable=False)
    equipment_type_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("equipment_types.id")
    )
    proposed_type_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("equipment_types.id")
    )
    # Доля 0..1; наружу отдаётся процентами (API_CONVENTIONS.md §3).
    type_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    supplier: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    batch: Mapped[CheckBatch] = relationship(back_populates="items")

    @property
    def is_editable(self) -> bool:
        return self.status in ("DRAFT", "NEEDS_INPUT")


class PipelineRun(Base):
    """Воспроизводимый прогон позиции с зафиксированными версиями."""

    __tablename__ = "pipeline_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    check_item_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("check_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(pg_enum("pipeline_run_status"), nullable=False)
    pipeline_definition_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("pipeline_definitions.id"), nullable=False
    )
    catalog_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("catalog_versions.id")
    )
    #: Снимок правил сравнения, зафиксированный этапом MATCHING. Без него отчёт
    #: не воспроизвести: правила меняются, а выданный результат — нет (§15.8).
    rule_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("rule_snapshots.id")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Лизинг воркера: пока он жив, прогон не подхватит второй воркер, а по
    # истечении — подхватит, и «повисший» уже не запишет результат (0006).
    worker_token: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Запрос отмены; в CANCELLED переводит воркер между этапами.
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    stages: Mapped[list[StageRun]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def is_active(self) -> bool:
        return self.status in RUN_ACTIVE_STATUSES


class StageRun(Base):
    """Состояние этапа pipeline с попытками и прогрессом."""

    __tablename__ = "stage_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage: Mapped[str] = mapped_column(pg_enum("pipeline_stage"), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    status: Mapped[str] = mapped_column(pg_enum("stage_status"), nullable=False)
    progress_current: Mapped[int | None] = mapped_column(Integer)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(Text)
    error: Mapped[dict | None] = mapped_column(JSONB)
    metrics: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[PipelineRun] = relationship(back_populates="stages")


class PipelineDefinition(Base):
    """Неизменяемая версия FSM pipeline. Владелец — модуль `pipeline`."""

    __tablename__ = "pipeline_definitions"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    version: Mapped[str] = mapped_column(Text, nullable=False)
    fsm: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
