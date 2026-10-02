"""ORM-модели модуля `rules`: снимок правил и результаты сравнения.

Владение таблицами — по docs/architecture/BACKEND_MODULE_MAP.md: `comparisons`
принадлежат этому модулю, `rule_snapshots` — тоже (schema.sql прямо называет
их «rules module»).

Схема создаётся миграцией из schema.sql; модели описывают только те колонки,
которые нужны коду.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text, func, text
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB as PgJSONB

#: Шаг миграции 03 (решение №4 шага 02, docs/migration/PROGRESS.md):
#: тесты фазы 2 поднимают схему в SQLite, поэтому JSONB получает вариант.
JSONB = PgJSONB().with_variant(JSON(), "sqlite")
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import pg_enum
from app.db import Base


class RuleSnapshot(Base):
    """Неизменяемый набор правил, зафиксированный прогоном.

    `rules` вместо канонического `content` и отсутствие `catalog_version_id` —
    так в `db/schema.sql`; версия каталога хранится внутри JSON. Расхождение с
    каноном §15.8 отмечено в docs/todo/pipeline.md.
    """

    __tablename__ = "rule_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    rule_version: Mapped[str] = mapped_column(Text, nullable=False)
    rules: Mapped[dict] = mapped_column(JSONB, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Comparison(Base):
    """Строка таблицы сопоставления: требование, факт, статус и объяснение.

    `computed_status` — результат движка, `effective_status` — то, что видит
    менеджер. Пока ручных override нет, они совпадают; расходиться им позволено
    по контракту (comparisons §Override), поэтому колонки разные с самого
    начала — иначе первый же override потребовал бы миграции данных.
    """

    __tablename__ = "comparisons"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    check_item_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("check_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("pipeline_runs.id")
    )
    canonical_parameter_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("canonical_parameters.id")
    )
    requirement_parameter_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("normalized_parameters.id")
    )
    fact_parameter_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("normalized_parameters.id")
    )
    row_kind: Mapped[str] = mapped_column(pg_enum("row_kind"), nullable=False)
    operator: Mapped[str | None] = mapped_column(pg_enum("comparison_operator"))
    computed_status: Mapped[str] = mapped_column(
        pg_enum("comparison_status"), nullable=False
    )
    effective_status: Mapped[str] = mapped_column(
        pg_enum("comparison_status"), nullable=False
    )
    explanation: Mapped[dict | None] = mapped_column(JSONB)
    lineage_chain: Mapped[list | None] = mapped_column(JSONB)
    overridden: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ComparisonOverride(Base):
    """Ручная смена статуса сравнения с обязательным комментарием.

    Отмена override — не удаление строки, а `active = false`: почему статус
    когда-то был изменён и кем, из журнала пропадать не должно
    (comparisons §QA). Активной у одного сравнения может быть только одна
    запись, и её `new_status` — это и есть `effective_status` строки.
    """

    __tablename__ = "comparison_overrides"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    comparison_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("comparisons.id", ondelete="CASCADE"),
        nullable=False,
    )
    old_status: Mapped[str] = mapped_column(
        pg_enum("comparison_status"), nullable=False
    )
    new_status: Mapped[str] = mapped_column(
        pg_enum("comparison_status"), nullable=False
    )
    reason_code: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
