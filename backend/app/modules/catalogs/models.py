"""ORM-модели модуля `catalogs`.

Владение — по docs/architecture/BACKEND_MODULE_MAP.md: версии каталога и всё их
содержимое (параметры, синонимы, единицы, конверсии, материалы, типы
оборудования, шаблоны) принадлежат этому модулю. Модуль `checks` читает версию
каталога и типы оборудования, но не пишет в них.

Ключевая особенность схемы: содержимое привязано к версии каталога, а не живёт
глобально. Опубликованная версия неизменяема, поэтому прогон, сделанный в марте,
воспроизводится в июне с теми же справочниками.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import pg_enum
from app.db import Base

#: Область действия синонима по умолчанию: применим к любому типу оборудования.
GLOBAL_SCOPE = "GLOBAL"

#: Статусы, в которых содержимое версии можно менять. Опубликованная и
#: выведенная версии неизменяемы (contract §Версии каталога).
EDITABLE_STATUSES: tuple[str, ...] = ("DRAFT", "VALID", "INVALID")

#: Переходы FSM версии каталога.
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "DRAFT": ("VALIDATING",),
    "VALIDATING": ("VALID", "INVALID"),
    "VALID": ("VALIDATING", "PUBLISHED"),
    "INVALID": ("VALIDATING",),
    "PUBLISHED": ("RETIRED",),
    "RETIRED": (),
}


class CatalogVersion(Base):
    """Версия справочников: published неизменяема, правки — новый draft."""

    __tablename__ = "catalog_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(pg_enum("catalog_status"), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("catalog_versions.id")
    )
    #: Результат последней проверки: errors/warnings/regressionSummary.
    validation: Mapped[dict | None] = mapped_column(JSONB)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def is_editable(self) -> bool:
        return self.status in EDITABLE_STATUSES

    @property
    def is_published(self) -> bool:
        return self.status == "PUBLISHED"


class Unit(Base):
    """Единица измерения внутри версии каталога."""

    __tablename__ = "units"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("catalog_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    symbol: Mapped[str] = mapped_column(Text, nullable=False)
    dimension: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)


class UnitAlias(Base):
    """Синоним единицы: как она встречается в документе ('куб.м/час')."""

    __tablename__ = "unit_aliases"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("catalog_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    unit_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    alias_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # Составной FK: единица обязана принадлежать той же версии каталога.
    __table_args__ = (
        ForeignKeyConstraint(
            ["catalog_version_id", "unit_id"],
            ["units.catalog_version_id", "units.id"],
            ondelete="CASCADE",
        ),
    )


class CanonicalParameter(Base):
    """Канонический параметр ТХ внутри версии каталога."""

    __tablename__ = "canonical_parameters"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("catalog_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    value_kind: Mapped[str] = mapped_column(pg_enum("value_kind"), nullable=False)
    dimension: Mapped[str | None] = mapped_column(Text)
    canonical_unit_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # Связи на синонимы намеренно нет: обращение к ней у только что созданного
    # объекта вызывает ленивый SELECT, а в async-сессии синхронный ввод-вывод
    # запрещён (MissingGreenlet). Синонимы читаются явным запросом
    # `CatalogsService.aliases_by_parameter`; удаление каскадит сама БД.
    __table_args__ = (
        ForeignKeyConstraint(
            ["catalog_version_id", "canonical_unit_id"],
            ["units.catalog_version_id", "units.id"],
        ),
    )


class ParameterAlias(Base):
    """Синоним канонического параметра со scope и приоритетом."""

    __tablename__ = "parameter_aliases"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("catalog_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    canonical_parameter_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("canonical_parameters.id", ondelete="CASCADE"),
        nullable=False,
    )
    alias_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    #: `GLOBAL` либо идентификатор типа оборудования — колонка текстовая.
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)


class ConversionRule(Base):
    """Линейная конверсия единиц: только scale/offset, без произвольных формул."""

    __tablename__ = "conversion_rules"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("catalog_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    from_unit_code: Mapped[str] = mapped_column(Text, nullable=False)
    to_unit_code: Mapped[str] = mapped_column(Text, nullable=False)
    dimension: Mapped[str] = mapped_column(Text, nullable=False)
    formula_type: Mapped[str] = mapped_column(
        pg_enum("conversion_formula"), nullable=False
    )
    scale: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    # `offset` — зарезервированное слово SQL, в схеме колонка взята в кавычки.
    offset_value: Mapped[Decimal] = mapped_column("offset", Numeric, nullable=False)


class MaterialNode(Base):
    """Узел иерархии материалов: `AISI 316L` ⊂ «Сталь»."""

    __tablename__ = "material_nodes"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("catalog_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("material_nodes.id")
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    #: Материализованный путь вида `steel/aisi-316l`; циклы запрещает сервис.
    path: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)


class EquipmentType(Base):
    """Тип оборудования внутри версии каталога (иерархия)."""

    __tablename__ = "equipment_types"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("catalog_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("equipment_types.id")
    )
    code: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    #: Метки для классификатора, заполняющего `check_items.proposed_type_id`.
    classifier_labels: Mapped[dict | None] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)


class ParameterTemplate(Base):
    """Шаблон ТХ типа оборудования: параметр, required, оператор, допуск."""

    __tablename__ = "parameter_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("catalog_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    equipment_type_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("equipment_types.id", ondelete="CASCADE"),
        nullable=False,
    )
    canonical_parameter_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("canonical_parameters.id"), nullable=False
    )
    required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    operator: Mapped[str | None] = mapped_column(pg_enum("comparison_operator"))
    tolerance: Mapped[dict | None] = mapped_column(JSONB)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="100")


class CatalogSuggestion(Base):
    """Предложение синонима от менеджера. Published каталог не меняет."""

    __tablename__ = "catalog_suggestions"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    canonical_text: Mapped[str] = mapped_column(Text, nullable=False)
    synonym_text: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_parameter_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("canonical_parameters.id")
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id")
    )
    similarity: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    status: Mapped[str] = mapped_column(pg_enum("suggestion_status"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id")
    )

    @property
    def is_resolved(self) -> bool:
        return self.status != "PENDING"
