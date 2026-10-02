"""Схемы модуля `admin/catalogs` и read-only проекции `/dictionary`."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.core.enums import ENUM_VALUES
from app.core.schemas import BaseSchema, Factor

# Допустимые значения берутся из того же словаря, что и ORM-типы: иначе схема
# приняла бы значение, которое PostgreSQL отвергнет на вставке. `Literal`
# принимает кортеж и разворачивает его в набор литералов.
ValueKind = Literal[ENUM_VALUES["value_kind"]]
ComparisonOperator = Literal[ENUM_VALUES["comparison_operator"]]

CODE_PATTERN = r"^[A-Za-z0-9_.\-]+$"


class VersionCreate(BaseSchema):
    """Новый draft. По умолчанию копируется из опубликованной версии."""

    code: str = Field(min_length=1, max_length=100, pattern=CODE_PATTERN)
    #: Откуда копировать содержимое. Без значения берётся published-по-умолчанию;
    #: `false` в `clone` даёт пустой draft.
    parent_id: UUID | None = None
    clone: bool = True


class ValidationIssue(BaseSchema):
    code: str
    path: str
    message: str


class ValidationReport(BaseSchema):
    version_id: UUID
    status: str
    errors: list[ValidationIssue]
    warnings: list[ValidationIssue]
    #: Сводка регрессии на golden dataset. Датасета ещё нет (DATA_READINESS.md
    #: Gate 1), поэтому всегда null — поле в контракте объявлено nullable.
    regression_summary: dict | None = None


class VersionResponse(BaseSchema):
    id: UUID
    code: str
    status: str
    is_default: bool
    parent_id: UUID | None = None
    version: int
    validation: ValidationReport | None = None
    created_at: datetime
    published_at: datetime | None = None
    retired_at: datetime | None = None
    counts: dict[str, int]


class UnitCreate(BaseSchema):
    code: str = Field(min_length=1, max_length=100, pattern=CODE_PATTERN)
    symbol: str = Field(min_length=1, max_length=50)
    dimension: str = Field(min_length=1, max_length=100)
    active: bool = True


class UnitUpdate(BaseSchema):
    symbol: str | None = Field(default=None, min_length=1, max_length=50)
    dimension: str | None = Field(default=None, min_length=1, max_length=100)
    active: bool | None = None


class UnitResponse(BaseSchema):
    id: UUID
    code: str
    symbol: str
    dimension: str
    active: bool


class ParameterCreate(BaseSchema):
    code: str = Field(min_length=1, max_length=100, pattern=CODE_PATTERN)
    display_name: str = Field(min_length=1, max_length=300)
    value_kind: ValueKind
    dimension: str | None = Field(default=None, max_length=100)
    canonical_unit_code: str | None = Field(default=None, max_length=100)
    active: bool = True


class ParameterUpdate(BaseSchema):
    display_name: str | None = Field(default=None, min_length=1, max_length=300)
    value_kind: ValueKind | None = None
    dimension: str | None = Field(default=None, max_length=100)
    canonical_unit_code: str | None = Field(default=None, max_length=100)
    active: bool | None = None


class AliasResponse(BaseSchema):
    id: UUID
    alias_text: str
    normalized_text: str
    scope: str
    priority: int
    active: bool


class ParameterResponse(BaseSchema):
    id: UUID
    code: str
    display_name: str
    value_kind: str
    dimension: str | None = None
    canonical_unit_code: str | None = None
    active: bool
    aliases: list[AliasResponse]


class AliasCreate(BaseSchema):
    canonical_parameter_id: UUID
    alias_text: str = Field(min_length=1, max_length=300)
    #: `GLOBAL` либо идентификатор типа оборудования.
    scope: str = Field(default="GLOBAL", max_length=100)
    priority: int = Field(default=100, ge=1, le=1000)
    active: bool = True


class AliasUpdate(BaseSchema):
    alias_text: str | None = Field(default=None, min_length=1, max_length=300)
    scope: str | None = Field(default=None, max_length=100)
    priority: int | None = Field(default=None, ge=1, le=1000)
    active: bool | None = None


class ConversionCreate(BaseSchema):
    from_unit_code: str = Field(min_length=1, max_length=100)
    to_unit_code: str = Field(min_length=1, max_length=100)
    #: Только линейная форма: произвольное выражение контрактом запрещено.
    formula_type: Literal["LINEAR"] = "LINEAR"
    scale: Factor
    offset: Factor = Decimal(0)


class ConversionUpdate(BaseSchema):
    scale: Factor | None = None
    offset: Factor | None = None


class ConversionResponse(BaseSchema):
    id: UUID
    from_unit_code: str
    to_unit_code: str
    dimension: str
    formula_type: str
    scale: Factor
    offset: Factor


class ConversionTest(BaseSchema):
    from_unit_code: str = Field(min_length=1, max_length=100)
    to_unit_code: str = Field(min_length=1, max_length=100)
    value: Factor


class ConversionTestResult(BaseSchema):
    """Результат пробной конверсии. Правило не сохраняется."""

    from_unit_code: str
    to_unit_code: str
    value: Factor
    result: Factor
    explanation: str


class MaterialCreate(BaseSchema):
    code: str = Field(min_length=1, max_length=100, pattern=CODE_PATTERN)
    name: str = Field(min_length=1, max_length=300)
    parent_id: UUID | None = None
    active: bool = True


class MaterialUpdate(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    active: bool | None = None


class MaterialMove(BaseSchema):
    """Новый родитель узла. `null` поднимает узел в корень."""

    parent_id: UUID | None = None


class MaterialResponse(BaseSchema):
    id: UUID
    code: str
    name: str
    path: str
    parent_id: UUID | None = None
    active: bool


class EquipmentTypeCreate(BaseSchema):
    code: str = Field(min_length=1, max_length=100, pattern=CODE_PATTERN)
    name: str = Field(min_length=1, max_length=300)
    parent_id: UUID | None = None
    classifier_labels: dict | None = None
    active: bool = True


class EquipmentTypeUpdate(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    classifier_labels: dict | None = None
    active: bool | None = None


class EquipmentTypeResponse(BaseSchema):
    id: UUID
    code: str
    name: str
    parent_id: UUID | None = None
    classifier_labels: dict | None = None
    active: bool


class TemplateCreate(BaseSchema):
    canonical_parameter_id: UUID
    required: bool = False
    operator: ComparisonOperator | None = None
    tolerance: dict | None = None
    priority: int = Field(default=100, ge=1, le=1000)


class TemplateUpdate(BaseSchema):
    required: bool | None = None
    operator: ComparisonOperator | None = None
    tolerance: dict | None = None
    priority: int | None = Field(default=None, ge=1, le=1000)


class TemplateResponse(BaseSchema):
    id: UUID
    equipment_type_id: UUID
    canonical_parameter_id: UUID
    parameter_code: str
    required: bool
    operator: str | None = None
    tolerance: dict | None = None
    priority: int


# --- проекция /dictionary ----------------------------------------------------


class DictionaryParameter(BaseSchema):
    """Каноническое название с синонимами для экрана «Словарь»."""

    id: UUID
    code: str
    canonical: str
    value_kind: str
    #: Физическая размерность параметра. Нужна экрану для фильтра: по единице
    #: фильтровать нельзя — у безразмерных параметров её нет вовсе.
    dimension: str | None = None
    unit: str | None = None
    synonyms: list[str]


class DictionaryUnit(BaseSchema):
    """Единица со ссылкой на базовую: экран «Словарь» показывает конвертер.

    `baseUnit` и коэффициенты берутся из правила конверсии этой единицы. У самой
    базовой единицы правила нет, поэтому поля пустые — это не отсутствие данных,
    а сама база.
    """

    id: UUID
    code: str
    symbol: str
    dimension: str
    synonyms: list[str]
    base_unit: str | None = None
    scale: Factor | None = None
    offset: Factor | None = None


class DictionaryMaterial(BaseSchema):
    id: UUID
    code: str
    name: str
    path: str
    parent_id: UUID | None = None


class DictionaryResponse(BaseSchema):
    """Read-only проекция опубликованной версии каталога."""

    catalog_version: str | None = None
    parameters: list[DictionaryParameter]
    units: list[DictionaryUnit]
    materials: list[DictionaryMaterial]


class SuggestionCreate(BaseSchema):
    canonical: str = Field(min_length=1, max_length=300)
    synonym: str = Field(min_length=1, max_length=300)
    #: Стабильный идентификатор параметра. Отображаемое имя ключом не является:
    #: оно меняется при переименовании и может совпадать у разных параметров.
    #: Остаётся необязательным ради клиентов, которые его ещё не передают.
    canonical_parameter_id: UUID | None = None


class SuggestionDecision(BaseSchema):
    status: Literal["accepted", "rejected"]


class SuggestionResponse(BaseSchema):
    id: UUID
    canonical: str
    synonym: str
    status: str
    canonical_parameter_id: UUID | None = None
    author_id: UUID | None = None
    created_at: datetime
    decided_at: datetime | None = None
    #: Черновик, в который лёг принятый синоним: его администратору предстоит
    #: проверить и опубликовать. Заполняется только в ответе на решение.
    draft_version_id: UUID | None = None
    draft_version_code: str | None = None
