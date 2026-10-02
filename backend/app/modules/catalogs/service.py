"""Сценарии модуля `admin/catalogs`.

Главный инвариант: опубликованная версия неизменяема. Любая правка выполняется
в draft-версии, затем validate → publish. Прогоны ссылаются на версию каталога,
поэтому изменение published-содержимого сломало бы воспроизводимость прошлых
проверок (docs/api/admin/catalogs/README.md §Версии каталога).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError, ErrorCode, NotFoundError
from app.modules.catalogs.models import (
    ALLOWED_TRANSITIONS,
    GLOBAL_SCOPE,
    CanonicalParameter,
    CatalogSuggestion,
    CatalogVersion,
    ConversionRule,
    EquipmentType,
    MaterialNode,
    ParameterAlias,
    ParameterTemplate,
    Unit,
    UnitAlias,
)
from app.modules.catalogs.normalize import normalize
from app.modules.catalogs.schemas import (
    AliasCreate,
    AliasUpdate,
    ConversionCreate,
    ConversionUpdate,
    EquipmentTypeCreate,
    EquipmentTypeUpdate,
    MaterialCreate,
    MaterialUpdate,
    ParameterCreate,
    ParameterUpdate,
    TemplateCreate,
    TemplateUpdate,
    UnitCreate,
    UnitUpdate,
    VersionCreate,
)

#: Операторы, применимые к виду значения. Шаблон с несовместимой парой сравнивать
#: нечем: `GREATER` для TEXT или `MATERIAL_MATCH` для NUMBER — ошибка каталога.
OPERATORS_BY_VALUE_KIND: dict[str, tuple[str, ...]] = {
    "NUMBER": (
        "EQUAL",
        "NOT_EQUAL",
        "GREATER",
        "GREATER_OR_EQUAL",
        "LESS",
        "LESS_OR_EQUAL",
        "RANGE",
    ),
    "RANGE": ("RANGE", "EQUAL", "NOT_EQUAL"),
    "TEXT": ("EQUAL", "NOT_EQUAL", "TEXT_MATCH"),
    "BOOLEAN": ("EQUAL", "NOT_EQUAL"),
    "SET": ("SET_CONTAINS", "EQUAL", "NOT_EQUAL"),
    "MATERIAL": ("MATERIAL_MATCH", "EQUAL", "NOT_EQUAL"),
    "UNKNOWN": (),
}


class CatalogErrorCode(StrEnum):
    """Коды ошибок (docs/api/admin/catalogs/README.md §Ошибки)."""

    CATALOG_VERSION_NOT_FOUND = "CATALOG_VERSION_NOT_FOUND"
    CATALOG_VERSION_IMMUTABLE = "CATALOG_VERSION_IMMUTABLE"
    CATALOG_PUBLISH_CONFLICT = "CATALOG_PUBLISH_CONFLICT"
    CATALOG_VALIDATION_FAILED = "CATALOG_VALIDATION_FAILED"
    EDIT_CONFLICT = "EDIT_CONFLICT"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    ALIAS_CONFLICT = "ALIAS_CONFLICT"
    UNIT_DIMENSION_MISMATCH = "UNIT_DIMENSION_MISMATCH"
    CONVERSION_CYCLE_OR_AMBIGUITY = "CONVERSION_CYCLE_OR_AMBIGUITY"
    CONVERSION_NOT_FOUND = "CONVERSION_NOT_FOUND"
    MATERIAL_HIERARCHY_CYCLE = "MATERIAL_HIERARCHY_CYCLE"
    MATERIAL_IN_USE = "MATERIAL_IN_USE"
    MATERIAL_NOT_FOUND = "MATERIAL_NOT_FOUND"
    ALIAS_NOT_FOUND = "ALIAS_NOT_FOUND"
    TEMPLATE_NOT_FOUND = "TEMPLATE_NOT_FOUND"
    DUPLICATE_CODE = "DUPLICATE_CODE"
    UNIT_NOT_FOUND = "UNIT_NOT_FOUND"
    PARAMETER_NOT_FOUND = "PARAMETER_NOT_FOUND"
    EQUIPMENT_TYPE_NOT_FOUND = "EQUIPMENT_TYPE_NOT_FOUND"
    TEMPLATE_OPERATOR_INCOMPATIBLE = "TEMPLATE_OPERATOR_INCOMPATIBLE"
    SUGGESTION_NOT_FOUND = "SUGGESTION_NOT_FOUND"
    SUGGESTION_ALREADY_RESOLVED = "SUGGESTION_ALREADY_RESOLVED"
    NO_PUBLISHED_CATALOG = "NO_PUBLISHED_CATALOG"


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    message: str


class CatalogsService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- версии ---------------------------------------------------------

    async def list_versions(self) -> list[CatalogVersion]:
        return list(
            (
                await self._session.scalars(
                    select(CatalogVersion).order_by(CatalogVersion.created_at.desc())
                )
            ).all()
        )

    async def get_version(self, version_id: uuid.UUID) -> CatalogVersion:
        version = await self._session.get(CatalogVersion, version_id)
        if version is None:
            raise ApiError(
                CatalogErrorCode.CATALOG_VERSION_NOT_FOUND,
                "Версия каталога не найдена",
                status_code=404,
                details={"versionId": str(version_id)},
            )
        return version

    async def published_version(self) -> CatalogVersion | None:
        """Версия по умолчанию для новых прогонов и для экрана «Словарь»."""
        return await self._session.scalar(
            select(CatalogVersion).where(
                CatalogVersion.status == "PUBLISHED",
                CatalogVersion.is_default.is_(True),
            )
        )

    async def create_version(
        self, payload: VersionCreate, author_id: uuid.UUID
    ) -> CatalogVersion:
        """Создаёт draft, по умолчанию копируя содержимое published-версии."""
        if await self._code_taken(payload.code):
            raise ApiError(
                CatalogErrorCode.DUPLICATE_CODE,
                f"Версия каталога {payload.code} уже существует",
                status_code=409,
            )

        parent = (
            await self.get_version(payload.parent_id)
            if payload.parent_id
            else await self.published_version()
        )

        version = CatalogVersion(
            code=payload.code,
            status="DRAFT",
            is_default=False,
            parent_id=parent.id if parent else None,
            version=1,
            created_by=author_id,
        )
        self._session.add(version)
        await self._session.flush()

        if payload.clone and parent is not None:
            await self._clone_content(parent.id, version.id)
        return version

    async def validate_version(self, version_id: uuid.UUID) -> list[Issue]:
        """Проверяет содержимое draft и записывает результат в версию.

        Возвращает список ошибок; предупреждения кладутся в `validation`.
        """
        version = await self.get_version(version_id)
        self._assert_editable(version)

        errors, warnings = await self._collect_issues(version_id)
        version.status = "INVALID" if errors else "VALID"
        version.validation = {
            "errors": [issue.__dict__ for issue in errors],
            "warnings": [issue.__dict__ for issue in warnings],
            # Регрессия на golden dataset появится вместе с датасетом (Gate 1).
            "regression_summary": None,
        }
        version.version += 1
        await self._session.flush()
        return errors

    async def publish_version(
        self, version_id: uuid.UUID, expected_version: int
    ) -> CatalogVersion:
        """Атомарно публикует версию, снимая default с предыдущей."""
        version = await self.get_version(version_id)
        self._assert_version_match(version, expected_version)

        if version.status == "PUBLISHED":
            raise ApiError(
                CatalogErrorCode.CATALOG_PUBLISH_CONFLICT,
                "Версия уже опубликована",
                status_code=409,
            )
        if version.status != "VALID":
            raise ApiError(
                CatalogErrorCode.CATALOG_VALIDATION_FAILED,
                "Публикация возможна только после успешной валидации"
                f" (статус версии: {version.status})",
                status_code=422,
            )

        # Снятие default до установки нового: частичный уникальный индекс
        # `uq_catalog_single_default` не допускает двух версий по умолчанию.
        await self._session.execute(
            update(CatalogVersion)
            .where(CatalogVersion.is_default.is_(True))
            .values(is_default=False)
        )
        version.status = "PUBLISHED"
        version.is_default = True
        version.published_at = datetime.now(UTC)
        version.version += 1
        await self._session.flush()
        return version

    async def retire_version(
        self, version_id: uuid.UUID, expected_version: int
    ) -> CatalogVersion:
        """Выводит версию из выбора для новых прогонов.

        Прошлые прогоны продолжают ссылаться на неё: их снимок остаётся
        воспроизводимым (contract §QA «старые Pipeline runs сохраняют снимок»).
        """
        version = await self.get_version(version_id)
        self._assert_version_match(version, expected_version)
        self._assert_transition(version, "RETIRED")

        version.status = "RETIRED"
        version.is_default = False
        version.retired_at = datetime.now(UTC)
        version.version += 1
        await self._session.flush()
        return version

    async def counts(self, version_id: uuid.UUID) -> dict[str, int]:
        """Сколько объектов каждого вида в версии — для списка версий."""
        result: dict[str, int] = {}
        for key, model in (
            ("parameters", CanonicalParameter),
            ("aliases", ParameterAlias),
            ("units", Unit),
            ("conversions", ConversionRule),
            ("materials", MaterialNode),
            ("equipmentTypes", EquipmentType),
            ("templates", ParameterTemplate),
        ):
            result[key] = (
                await self._session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.catalog_version_id == version_id)
                )
            ) or 0
        return result

    # --- единицы --------------------------------------------------------

    async def list_units(self, version_id: uuid.UUID) -> list[Unit]:
        await self.get_version(version_id)
        return list(
            (
                await self._session.scalars(
                    select(Unit)
                    .where(Unit.catalog_version_id == version_id)
                    .order_by(Unit.code)
                )
            ).all()
        )

    async def create_unit(self, version_id: uuid.UUID, payload: UnitCreate) -> Unit:
        await self._editable_version(version_id)
        if await self._exists(Unit, version_id, Unit.code == payload.code):
            raise ApiError(
                CatalogErrorCode.DUPLICATE_CODE,
                f"Единица {payload.code} в этой версии уже есть",
                status_code=409,
            )
        unit = Unit(
            catalog_version_id=version_id,
            code=payload.code,
            symbol=payload.symbol,
            dimension=payload.dimension,
            active=payload.active,
        )
        self._session.add(unit)
        await self._session.flush()
        return unit

    async def update_unit(
        self, version_id: uuid.UUID, unit_id: uuid.UUID, payload: UnitUpdate
    ) -> Unit:
        await self._editable_version(version_id)
        unit = await self._child(
            Unit, version_id, unit_id, CatalogErrorCode.UNIT_NOT_FOUND
        )
        changes = payload.model_dump(exclude_unset=True)

        if "dimension" in changes and changes["dimension"] != unit.dimension:
            await self._assert_dimension_change_safe(version_id, unit)

        for field, value in changes.items():
            setattr(unit, field, value)
        await self._session.flush()
        return unit

    async def _assert_dimension_change_safe(
        self, version_id: uuid.UUID, unit: Unit
    ) -> None:
        """Размерность нельзя менять, пока на единицу ссылаются параметры.

        Иначе канонический параметр «Производительность» получил бы единицу
        давления, и сравнение молча сравнивало бы несравнимое.
        """
        used = await self._session.scalar(
            select(func.count())
            .select_from(CanonicalParameter)
            .where(
                CanonicalParameter.catalog_version_id == version_id,
                CanonicalParameter.canonical_unit_id == unit.id,
            )
        )
        if used:
            raise ApiError(
                CatalogErrorCode.UNIT_DIMENSION_MISMATCH,
                f"Единица {unit.code} используется каноническими параметрами:"
                " смените их единицу прежде, чем менять размерность",
                status_code=422,
                details={"parameters": used},
            )

    # --- канонические параметры ----------------------------------------

    async def list_parameters(self, version_id: uuid.UUID) -> list[CanonicalParameter]:
        await self.get_version(version_id)
        return list(
            (
                await self._session.scalars(
                    select(CanonicalParameter)
                    .where(CanonicalParameter.catalog_version_id == version_id)
                    .order_by(CanonicalParameter.code)
                )
            ).all()
        )

    async def create_parameter(
        self, version_id: uuid.UUID, payload: ParameterCreate
    ) -> CanonicalParameter:
        await self._editable_version(version_id)
        if await self._exists(
            CanonicalParameter, version_id, CanonicalParameter.code == payload.code
        ):
            raise ApiError(
                CatalogErrorCode.DUPLICATE_CODE,
                f"Параметр {payload.code} в этой версии уже есть",
                status_code=409,
            )

        unit = await self._unit_by_code(version_id, payload.canonical_unit_code)
        self._assert_dimension_agrees(payload.dimension, unit)

        parameter = CanonicalParameter(
            catalog_version_id=version_id,
            code=payload.code,
            display_name=payload.display_name,
            value_kind=payload.value_kind,
            dimension=payload.dimension,
            canonical_unit_id=unit.id if unit else None,
            active=payload.active,
        )
        self._session.add(parameter)
        await self._session.flush()
        return parameter

    async def update_parameter(
        self,
        version_id: uuid.UUID,
        parameter_id: uuid.UUID,
        payload: ParameterUpdate,
    ) -> CanonicalParameter:
        await self._editable_version(version_id)
        parameter = await self._child(
            CanonicalParameter,
            version_id,
            parameter_id,
            CatalogErrorCode.PARAMETER_NOT_FOUND,
        )
        changes = payload.model_dump(exclude_unset=True)

        if "canonical_unit_code" in changes:
            unit = await self._unit_by_code(version_id, payload.canonical_unit_code)
            dimension = changes.get("dimension", parameter.dimension)
            self._assert_dimension_agrees(dimension, unit)
            parameter.canonical_unit_id = unit.id if unit else None
        for field in ("display_name", "value_kind", "dimension", "active"):
            if field in changes:
                setattr(parameter, field, changes[field])
        await self._session.flush()
        return parameter

    async def delete_parameter(
        self, version_id: uuid.UUID, parameter_id: uuid.UUID
    ) -> None:
        await self._editable_version(version_id)
        parameter = await self._child(
            CanonicalParameter,
            version_id,
            parameter_id,
            CatalogErrorCode.PARAMETER_NOT_FOUND,
        )
        await self._session.delete(parameter)
        await self._session.flush()

    @staticmethod
    def _assert_dimension_agrees(dimension: str | None, unit: Unit | None) -> None:
        if unit is None or dimension is None:
            return
        if unit.dimension != dimension:
            raise ApiError(
                CatalogErrorCode.UNIT_DIMENSION_MISMATCH,
                f"Единица {unit.code} имеет размерность {unit.dimension},"
                f" а параметр объявлен как {dimension}",
                status_code=422,
            )

    async def _unit_by_code(
        self, version_id: uuid.UUID, code: str | None
    ) -> Unit | None:
        if not code:
            return None
        unit = await self._session.scalar(
            select(Unit).where(Unit.catalog_version_id == version_id, Unit.code == code)
        )
        if unit is None:
            raise ApiError(
                CatalogErrorCode.UNIT_NOT_FOUND,
                f"Единица {code} не найдена в этой версии каталога",
                status_code=422,
            )
        return unit

    # --- синонимы параметров --------------------------------------------

    async def list_aliases(self, version_id: uuid.UUID) -> list[ParameterAlias]:
        await self.get_version(version_id)
        return list(
            (
                await self._session.scalars(
                    select(ParameterAlias)
                    .where(ParameterAlias.catalog_version_id == version_id)
                    .order_by(ParameterAlias.normalized_text)
                )
            ).all()
        )

    async def aliases_by_parameter(
        self, version_id: uuid.UUID
    ) -> dict[uuid.UUID, list[ParameterAlias]]:
        """Синонимы версии, сгруппированные по параметру и упорядоченные по приоритету."""
        grouped: dict[uuid.UUID, list[ParameterAlias]] = {}
        for alias in await self.list_aliases(version_id):
            grouped.setdefault(alias.canonical_parameter_id, []).append(alias)
        for aliases in grouped.values():
            aliases.sort(key=lambda item: (item.priority, item.alias_text))
        return grouped

    async def create_alias(
        self, version_id: uuid.UUID, payload: AliasCreate
    ) -> ParameterAlias:
        await self._editable_version(version_id)
        await self._child(
            CanonicalParameter,
            version_id,
            payload.canonical_parameter_id,
            CatalogErrorCode.PARAMETER_NOT_FOUND,
        )
        normalized = self._normalized_or_reject(payload.alias_text)
        await self._assert_alias_free(
            version_id, normalized, payload.scope, exclude_id=None
        )

        alias = ParameterAlias(
            catalog_version_id=version_id,
            canonical_parameter_id=payload.canonical_parameter_id,
            alias_text=payload.alias_text.strip(),
            normalized_text=normalized,
            scope=payload.scope,
            priority=payload.priority,
            active=payload.active,
        )
        self._session.add(alias)
        await self._session.flush()
        return alias

    async def update_alias(
        self, version_id: uuid.UUID, alias_id: uuid.UUID, payload: AliasUpdate
    ) -> ParameterAlias:
        await self._editable_version(version_id)
        alias = await self._child(
            ParameterAlias, version_id, alias_id, CatalogErrorCode.ALIAS_NOT_FOUND
        )
        changes = payload.model_dump(exclude_unset=True)

        normalized = alias.normalized_text
        if "alias_text" in changes:
            normalized = self._normalized_or_reject(payload.alias_text or "")
        scope = changes.get("scope", alias.scope)
        if normalized != alias.normalized_text or scope != alias.scope:
            await self._assert_alias_free(
                version_id, normalized, scope, exclude_id=alias.id
            )

        if "alias_text" in changes:
            alias.alias_text = (payload.alias_text or "").strip()
            alias.normalized_text = normalized
        for field in ("scope", "priority", "active"):
            if field in changes:
                setattr(alias, field, changes[field])
        await self._session.flush()
        return alias

    async def delete_alias(self, version_id: uuid.UUID, alias_id: uuid.UUID) -> None:
        await self._editable_version(version_id)
        alias = await self._child(
            ParameterAlias, version_id, alias_id, CatalogErrorCode.ALIAS_NOT_FOUND
        )
        await self._session.delete(alias)
        await self._session.flush()

    @staticmethod
    def _normalized_or_reject(text: str) -> str:
        normalized = normalize(text)
        if not normalized:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "Синоним не содержит значимых символов",
                status_code=422,
            )
        return normalized

    async def _assert_alias_free(
        self,
        version_id: uuid.UUID,
        normalized: str,
        scope: str,
        *,
        exclude_id: uuid.UUID | None,
    ) -> None:
        """Один нормализованный текст в одном scope — один параметр.

        Иначе сопоставление получило бы два кандидата и не смогло выбрать
        (contract: «два активных aliases ... не должны неоднозначно ссылаться»).
        """
        query = select(ParameterAlias).where(
            ParameterAlias.catalog_version_id == version_id,
            ParameterAlias.normalized_text == normalized,
            ParameterAlias.scope == scope,
        )
        if exclude_id is not None:
            query = query.where(ParameterAlias.id != exclude_id)
        clash = await self._session.scalar(query)
        if clash is not None:
            raise ApiError(
                CatalogErrorCode.ALIAS_CONFLICT,
                f"Синоним «{normalized}» в области {scope} уже занят",
                details={"aliasId": str(clash.id)},
                status_code=422,
            )

    # --- конверсии ------------------------------------------------------

    async def list_conversions(self, version_id: uuid.UUID) -> list[ConversionRule]:
        await self.get_version(version_id)
        return list(
            (
                await self._session.scalars(
                    select(ConversionRule)
                    .where(ConversionRule.catalog_version_id == version_id)
                    .order_by(
                        ConversionRule.from_unit_code, ConversionRule.to_unit_code
                    )
                )
            ).all()
        )

    async def create_conversion(
        self, version_id: uuid.UUID, payload: ConversionCreate
    ) -> ConversionRule:
        await self._editable_version(version_id)
        source = await self._require_unit(version_id, payload.from_unit_code)
        target = await self._require_unit(version_id, payload.to_unit_code)

        if source.code == target.code:
            raise ApiError(
                CatalogErrorCode.CONVERSION_CYCLE_OR_AMBIGUITY,
                "Конверсия единицы в саму себя не имеет смысла",
                status_code=422,
            )
        if source.dimension != target.dimension:
            raise ApiError(
                CatalogErrorCode.UNIT_DIMENSION_MISMATCH,
                f"Единицы разной размерности: {source.dimension} и {target.dimension}",
                status_code=422,
            )
        if payload.scale == 0:
            raise ApiError(
                CatalogErrorCode.CONVERSION_CYCLE_OR_AMBIGUITY,
                "Нулевой множитель делает конверсию необратимой",
                status_code=422,
            )
        if await self._exists(
            ConversionRule,
            version_id,
            ConversionRule.from_unit_code == source.code,
            ConversionRule.to_unit_code == target.code,
        ):
            raise ApiError(
                CatalogErrorCode.CONVERSION_CYCLE_OR_AMBIGUITY,
                f"Конверсия {source.code} → {target.code} уже задана",
                status_code=422,
            )

        rule = ConversionRule(
            catalog_version_id=version_id,
            from_unit_code=source.code,
            to_unit_code=target.code,
            dimension=source.dimension,
            formula_type=payload.formula_type,
            scale=payload.scale,
            offset_value=payload.offset,
        )
        self._session.add(rule)
        await self._session.flush()
        return rule

    async def update_conversion(
        self,
        version_id: uuid.UUID,
        conversion_id: uuid.UUID,
        payload: ConversionUpdate,
    ) -> ConversionRule:
        await self._editable_version(version_id)
        rule = await self._child(
            ConversionRule,
            version_id,
            conversion_id,
            CatalogErrorCode.CONVERSION_NOT_FOUND,
        )
        changes = payload.model_dump(exclude_unset=True)
        if "scale" in changes:
            if payload.scale == 0:
                raise ApiError(
                    CatalogErrorCode.CONVERSION_CYCLE_OR_AMBIGUITY,
                    "Нулевой множитель делает конверсию необратимой",
                    status_code=422,
                )
            rule.scale = payload.scale
        if "offset" in changes:
            rule.offset_value = payload.offset
        await self._session.flush()
        return rule

    async def test_conversion(
        self, version_id: uuid.UUID, from_code: str, to_code: str, value: Decimal
    ) -> tuple[Decimal, str]:
        """Пробная конверсия: правило не сохраняется, только считается."""
        await self.get_version(version_id)
        rule = await self._session.scalar(
            select(ConversionRule).where(
                ConversionRule.catalog_version_id == version_id,
                ConversionRule.from_unit_code == from_code,
                ConversionRule.to_unit_code == to_code,
            )
        )
        if rule is None:
            # Обратное правило считается делением: задавать обе стороны вручную
            # значило бы держать два источника истины для одного коэффициента.
            inverse = await self._session.scalar(
                select(ConversionRule).where(
                    ConversionRule.catalog_version_id == version_id,
                    ConversionRule.from_unit_code == to_code,
                    ConversionRule.to_unit_code == from_code,
                )
            )
            if inverse is None:
                raise ApiError(
                    CatalogErrorCode.CONVERSION_NOT_FOUND,
                    f"Правило {from_code} → {to_code} не задано",
                    status_code=422,
                )
            result = (value - inverse.offset_value) / inverse.scale
            return result, (
                f"обратное правило {inverse.to_unit_code} → {inverse.from_unit_code}:"
                f" ({value} − {inverse.offset_value}) / {inverse.scale}"
            )

        result = value * rule.scale + rule.offset_value
        return result, f"{value} × {rule.scale} + {rule.offset_value}"

    async def _require_unit(self, version_id: uuid.UUID, code: str) -> Unit:
        """Единица по коду. `_unit_by_code` уже отвергает отсутствующую."""
        unit = await self._unit_by_code(version_id, code)
        assert unit is not None  # noqa: S101 — code непустой по схеме запроса
        return unit

    # --- материалы ------------------------------------------------------

    async def list_materials(self, version_id: uuid.UUID) -> list[MaterialNode]:
        await self.get_version(version_id)
        return list(
            (
                await self._session.scalars(
                    select(MaterialNode)
                    .where(MaterialNode.catalog_version_id == version_id)
                    .order_by(MaterialNode.path)
                )
            ).all()
        )

    async def create_material(
        self, version_id: uuid.UUID, payload: MaterialCreate
    ) -> MaterialNode:
        await self._editable_version(version_id)
        if await self._exists(
            MaterialNode, version_id, MaterialNode.code == payload.code
        ):
            raise ApiError(
                CatalogErrorCode.DUPLICATE_CODE,
                f"Материал {payload.code} в этой версии уже есть",
                status_code=409,
            )
        parent = (
            await self._child(
                MaterialNode,
                version_id,
                payload.parent_id,
                CatalogErrorCode.MATERIAL_NOT_FOUND,
            )
            if payload.parent_id
            else None
        )

        node = MaterialNode(
            catalog_version_id=version_id,
            parent_id=parent.id if parent else None,
            code=payload.code,
            path=self._child_path(parent, payload.code),
            name=payload.name,
            active=payload.active,
        )
        self._session.add(node)
        await self._session.flush()
        return node

    async def update_material(
        self, version_id: uuid.UUID, node_id: uuid.UUID, payload: MaterialUpdate
    ) -> MaterialNode:
        await self._editable_version(version_id)
        node = await self._child(
            MaterialNode, version_id, node_id, CatalogErrorCode.MATERIAL_NOT_FOUND
        )
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(node, field, value)
        await self._session.flush()
        return node

    async def move_material(
        self, version_id: uuid.UUID, node_id: uuid.UUID, parent_id: uuid.UUID | None
    ) -> MaterialNode:
        """Переносит узел вместе с поддеревом, запрещая циклы."""
        await self._editable_version(version_id)
        node = await self._child(
            MaterialNode, version_id, node_id, CatalogErrorCode.MATERIAL_NOT_FOUND
        )
        parent = (
            await self._child(
                MaterialNode, version_id, parent_id, CatalogErrorCode.MATERIAL_NOT_FOUND
            )
            if parent_id
            else None
        )

        if parent is not None and (
            parent.id == node.id or parent.path.startswith(f"{node.path}/")
        ):
            raise ApiError(
                CatalogErrorCode.MATERIAL_HIERARCHY_CYCLE,
                "Узел нельзя перенести в себя или в собственное поддерево",
                status_code=422,
            )

        old_path = node.path
        node.parent_id = parent.id if parent else None
        node.path = self._child_path(parent, node.code)
        await self._session.flush()
        await self._rebase_subtree(version_id, old_path, node.path)
        return node

    async def delete_material(self, version_id: uuid.UUID, node_id: uuid.UUID) -> None:
        """Удаляет только лист: узел с детьми сначала переносят или гасят."""
        await self._editable_version(version_id)
        node = await self._child(
            MaterialNode, version_id, node_id, CatalogErrorCode.MATERIAL_NOT_FOUND
        )
        children = await self._session.scalar(
            select(func.count())
            .select_from(MaterialNode)
            .where(MaterialNode.parent_id == node.id)
        )
        if children:
            raise ApiError(
                CatalogErrorCode.MATERIAL_IN_USE,
                "У материала есть дочерние узлы: перенесите их или деактивируйте узел",
                status_code=422,
                details={"children": children},
            )
        await self._session.delete(node)
        await self._session.flush()

    @staticmethod
    def _child_path(parent: MaterialNode | None, code: str) -> str:
        slug = code.strip().lower()
        return f"{parent.path}/{slug}" if parent else slug

    async def _rebase_subtree(
        self, version_id: uuid.UUID, old_prefix: str, new_prefix: str
    ) -> None:
        """Пересобирает пути поддерева после переноса родителя."""
        descendants = (
            await self._session.scalars(
                select(MaterialNode).where(
                    MaterialNode.catalog_version_id == version_id,
                    MaterialNode.path.startswith(f"{old_prefix}/"),
                )
            )
        ).all()
        for node in descendants:
            node.path = f"{new_prefix}{node.path[len(old_prefix) :]}"
        await self._session.flush()

    # --- типы оборудования и шаблоны ------------------------------------

    async def list_equipment_types(self, version_id: uuid.UUID) -> list[EquipmentType]:
        await self.get_version(version_id)
        return list(
            (
                await self._session.scalars(
                    select(EquipmentType)
                    .where(EquipmentType.catalog_version_id == version_id)
                    .order_by(EquipmentType.code)
                )
            ).all()
        )

    async def create_equipment_type(
        self, version_id: uuid.UUID, payload: EquipmentTypeCreate
    ) -> EquipmentType:
        await self._editable_version(version_id)
        if await self._exists(
            EquipmentType, version_id, EquipmentType.code == payload.code
        ):
            raise ApiError(
                CatalogErrorCode.DUPLICATE_CODE,
                f"Тип оборудования {payload.code} в этой версии уже есть",
                status_code=409,
            )
        if payload.parent_id:
            await self._child(
                EquipmentType,
                version_id,
                payload.parent_id,
                CatalogErrorCode.EQUIPMENT_TYPE_NOT_FOUND,
            )

        equipment_type = EquipmentType(
            catalog_version_id=version_id,
            parent_id=payload.parent_id,
            code=payload.code,
            name=payload.name,
            classifier_labels=payload.classifier_labels,
            active=payload.active,
        )
        self._session.add(equipment_type)
        await self._session.flush()
        return equipment_type

    async def update_equipment_type(
        self, version_id: uuid.UUID, type_id: uuid.UUID, payload: EquipmentTypeUpdate
    ) -> EquipmentType:
        await self._editable_version(version_id)
        equipment_type = await self._child(
            EquipmentType,
            version_id,
            type_id,
            CatalogErrorCode.EQUIPMENT_TYPE_NOT_FOUND,
        )
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(equipment_type, field, value)
        await self._session.flush()
        return equipment_type

    async def list_templates(
        self, version_id: uuid.UUID, type_id: uuid.UUID
    ) -> list[tuple[ParameterTemplate, str]]:
        await self._child(
            EquipmentType,
            version_id,
            type_id,
            CatalogErrorCode.EQUIPMENT_TYPE_NOT_FOUND,
        )
        rows = await self._session.execute(
            select(ParameterTemplate, CanonicalParameter.code)
            .join(
                CanonicalParameter,
                CanonicalParameter.id == ParameterTemplate.canonical_parameter_id,
            )
            .where(
                ParameterTemplate.catalog_version_id == version_id,
                ParameterTemplate.equipment_type_id == type_id,
            )
            .order_by(ParameterTemplate.priority, CanonicalParameter.code)
        )
        return [(template, code) for template, code in rows.all()]

    async def create_template(
        self, version_id: uuid.UUID, type_id: uuid.UUID, payload: TemplateCreate
    ) -> tuple[ParameterTemplate, str]:
        await self._editable_version(version_id)
        await self._child(
            EquipmentType,
            version_id,
            type_id,
            CatalogErrorCode.EQUIPMENT_TYPE_NOT_FOUND,
        )
        parameter = await self._child(
            CanonicalParameter,
            version_id,
            payload.canonical_parameter_id,
            CatalogErrorCode.PARAMETER_NOT_FOUND,
        )
        self._assert_operator_fits(parameter, payload.operator)

        if await self._exists(
            ParameterTemplate,
            version_id,
            ParameterTemplate.equipment_type_id == type_id,
            ParameterTemplate.canonical_parameter_id == parameter.id,
        ):
            raise ApiError(
                CatalogErrorCode.DUPLICATE_CODE,
                f"Шаблон для параметра {parameter.code} у этого типа уже есть",
                status_code=409,
            )

        template = ParameterTemplate(
            catalog_version_id=version_id,
            equipment_type_id=type_id,
            canonical_parameter_id=parameter.id,
            required=payload.required,
            operator=payload.operator,
            tolerance=payload.tolerance,
            priority=payload.priority,
        )
        self._session.add(template)
        await self._session.flush()
        return template, parameter.code

    async def update_template(
        self, version_id: uuid.UUID, template_id: uuid.UUID, payload: TemplateUpdate
    ) -> tuple[ParameterTemplate, str]:
        await self._editable_version(version_id)
        template = await self._child(
            ParameterTemplate,
            version_id,
            template_id,
            CatalogErrorCode.TEMPLATE_NOT_FOUND,
        )
        parameter = await self._session.get(
            CanonicalParameter, template.canonical_parameter_id
        )
        changes = payload.model_dump(exclude_unset=True)
        if "operator" in changes and parameter is not None:
            self._assert_operator_fits(parameter, payload.operator)
        for field, value in changes.items():
            setattr(template, field, value)
        await self._session.flush()
        return template, parameter.code if parameter else ""

    @staticmethod
    def _assert_operator_fits(
        parameter: CanonicalParameter, operator: str | None
    ) -> None:
        if operator is None:
            return
        allowed = OPERATORS_BY_VALUE_KIND.get(parameter.value_kind, ())
        if operator not in allowed:
            raise ApiError(
                CatalogErrorCode.TEMPLATE_OPERATOR_INCOMPATIBLE,
                f"Оператор {operator} не применим к значению вида"
                f" {parameter.value_kind}",
                status_code=422,
                details={"allowed": list(allowed)},
            )

    # --- проекция словаря и предложения синонимов -----------------------

    async def dictionary(self) -> dict:
        """Read-only проекция опубликованной версии для экрана «Словарь»."""
        version = await self.published_version()
        if version is None:
            return {
                "catalog_version": None,
                "parameters": [],
                "units": [],
                "materials": [],
            }

        units = await self.list_units(version.id)
        unit_symbols = {unit.id: unit.symbol for unit in units}
        symbol_by_code = {unit.code: unit.symbol for unit in units}
        base_rules = await self._base_rule_map(version.id)
        unit_aliases = await self._unit_alias_map(version.id)
        parameter_aliases = await self.aliases_by_parameter(version.id)

        parameters = []
        for parameter in await self.list_parameters(version.id):
            parameters.append(
                {
                    "id": parameter.id,
                    "code": parameter.code,
                    "canonical": parameter.display_name,
                    "value_kind": parameter.value_kind,
                    "dimension": parameter.dimension,
                    "unit": unit_symbols.get(parameter.canonical_unit_id),
                    "synonyms": [
                        alias.alias_text
                        for alias in parameter_aliases.get(parameter.id, [])
                        if alias.active
                    ],
                }
            )

        return {
            "catalog_version": version.code,
            "parameters": parameters,
            "units": [
                self._dictionary_unit(
                    unit,
                    rule=base_rules.get(unit.code),
                    symbol_by_code=symbol_by_code,
                    synonyms=unit_aliases.get(unit.id, []),
                )
                for unit in units
                if unit.active
            ],
            "materials": [
                {
                    "id": node.id,
                    "code": node.code,
                    "name": node.name,
                    "path": node.path,
                    "parent_id": node.parent_id,
                }
                for node in await self.list_materials(version.id)
                if node.active
            ],
        }

    @staticmethod
    def _dictionary_unit(
        unit: Unit,
        *,
        rule: ConversionRule | None,
        symbol_by_code: dict[str, str],
        synonyms: list[str],
    ) -> dict:
        """Единица словаря вместе с переводом в базовую, если он объявлен."""
        return {
            "id": unit.id,
            "code": unit.code,
            "symbol": unit.symbol,
            "dimension": unit.dimension,
            "synonyms": synonyms,
            "base_unit": symbol_by_code.get(rule.to_unit_code) if rule else None,
            "scale": rule.scale if rule else None,
            "offset": rule.offset_value if rule else None,
        }

    async def _base_rule_map(self, version_id: uuid.UUID) -> dict[str, ConversionRule]:
        """Правило перевода в базовую единицу для каждой исходной единицы.

        Правил из одной единицы в линейном каталоге ожидается одно; если их
        несколько, берётся первое по коду цели — чтобы словарь не менялся от
        запроса к запросу (`list_conversions` сортирует детерминированно).
        """
        base: dict[str, ConversionRule] = {}
        for rule in await self.list_conversions(version_id):
            base.setdefault(rule.from_unit_code, rule)
        return base

    async def _unit_alias_map(
        self, version_id: uuid.UUID
    ) -> dict[uuid.UUID, list[str]]:
        rows = await self._session.scalars(
            select(UnitAlias)
            .where(
                UnitAlias.catalog_version_id == version_id, UnitAlias.active.is_(True)
            )
            .order_by(UnitAlias.priority)
        )
        grouped: dict[uuid.UUID, list[str]] = {}
        for alias in rows.all():
            grouped.setdefault(alias.unit_id, []).append(alias.alias_text)
        return grouped

    async def suggest_synonym(
        self,
        canonical: str,
        synonym: str,
        author_id: uuid.UUID,
        canonical_parameter_id: uuid.UUID | None = None,
    ) -> CatalogSuggestion:
        """Предложение менеджера. Опубликованный каталог не меняется."""
        normalized = self._normalized_or_reject(synonym)
        version = await self.published_version()
        parameter = await self._suggestion_target(
            version, canonical, canonical_parameter_id
        )

        # Дубликат ищется по паре «параметр + нормализованный синоним»: один и
        # тот же текст для разных параметров — разные предложения, а разное
        # написание одного синонима — одно и то же.
        duplicate = await self._pending_duplicate(parameter, normalized)
        if duplicate is not None:
            return duplicate
        if parameter is not None and await self._alias_exists(version, normalized):
            raise ApiError(
                CatalogErrorCode.ALIAS_CONFLICT,
                f"Синоним «{synonym.strip()}» уже есть в опубликованном каталоге",
                status_code=409,
            )

        suggestion = CatalogSuggestion(
            canonical_text=canonical.strip(),
            synonym_text=synonym.strip(),
            canonical_parameter_id=parameter.id if parameter else None,
            author_id=author_id,
            status="PENDING",
        )
        self._session.add(suggestion)
        await self._session.flush()
        return suggestion

    async def _suggestion_target(
        self,
        version: CatalogVersion | None,
        canonical: str,
        canonical_parameter_id: uuid.UUID | None,
    ) -> CanonicalParameter | None:
        """Параметр, к которому относится предложение.

        Идентификатор — основной путь: отображаемое имя не является ключом,
        оно меняется при переименовании и может совпадать у разных параметров.
        Поиск по имени оставлен запасным для клиентов, которые идентификатор
        ещё не передают.
        """
        if version is None:
            return None
        if canonical_parameter_id is not None:
            parameter = await self._session.get(
                CanonicalParameter, canonical_parameter_id
            )
            if parameter is None or parameter.catalog_version_id != version.id:
                raise ApiError(
                    CatalogErrorCode.PARAMETER_NOT_FOUND,
                    "Параметр не найден в опубликованной версии каталога",
                    status_code=404,
                    details={"canonicalParameterId": str(canonical_parameter_id)},
                )
            return parameter
        return await self._session.scalar(
            select(CanonicalParameter).where(
                CanonicalParameter.catalog_version_id == version.id,
                func.lower(CanonicalParameter.display_name) == canonical.lower(),
            )
        )

    async def _pending_duplicate(
        self, parameter: CanonicalParameter | None, normalized: str
    ) -> CatalogSuggestion | None:
        """Ожидающее предложение того же синонима для того же параметра."""
        pending = await self._session.scalars(
            select(CatalogSuggestion).where(CatalogSuggestion.status == "PENDING")
        )
        target = parameter.id if parameter else None
        for suggestion in pending.all():
            if suggestion.canonical_parameter_id != target:
                continue
            if normalize(suggestion.synonym_text) == normalized:
                return suggestion
        return None

    async def _alias_exists(self, version: CatalogVersion, normalized: str) -> bool:
        """Синоним уже опубликован — предлагать его повторно незачем."""
        found = await self._session.scalar(
            select(ParameterAlias.id).where(
                ParameterAlias.catalog_version_id == version.id,
                ParameterAlias.normalized_text == normalized,
            )
        )
        return found is not None

    async def list_suggestions(
        self, status: str | None = None, author_id: uuid.UUID | None = None
    ) -> list[CatalogSuggestion]:
        """Предложения синонимов, свежие сверху.

        `author_id` ограничивает выборку своими: раздел «Мои предложения»
        доступен любому менеджеру, а чужие решения его не касаются.
        """
        query = select(CatalogSuggestion).order_by(CatalogSuggestion.created_at.desc())
        if status:
            query = query.where(CatalogSuggestion.status == status.upper())
        if author_id is not None:
            query = query.where(CatalogSuggestion.author_id == author_id)
        return list((await self._session.scalars(query)).all())

    async def decide_suggestion(
        self, suggestion_id: uuid.UUID, decision: str, actor_id: uuid.UUID
    ) -> tuple[CatalogSuggestion, CatalogVersion | None]:
        """Принимает или отклоняет предложение синонима.

        Принятое предложение попадает не в опубликованный каталог — тот
        неизменяем, — а в черновик: существующий, если он есть, иначе новый,
        склонированный из published. Возвращается вместе с черновиком, чтобы
        администратор знал, что именно ему валидировать и публиковать.

        Раньше решение лишь меняло статус, и синоним приходилось заводить
        руками: кнопка выглядела законченным действием, которым не была.
        """
        suggestion = await self._session.get(CatalogSuggestion, suggestion_id)
        if suggestion is None:
            raise ApiError(
                CatalogErrorCode.SUGGESTION_NOT_FOUND,
                "Предложение синонима не найдено",
                status_code=404,
            )
        if suggestion.is_resolved:
            raise ApiError(
                CatalogErrorCode.SUGGESTION_ALREADY_RESOLVED,
                f"Предложение уже обработано: {suggestion.status}",
                status_code=409,
            )

        draft: CatalogVersion | None = None
        if decision == "accepted":
            draft = await self._apply_suggestion(suggestion, actor_id)

        suggestion.status = "ACCEPTED" if decision == "accepted" else "REJECTED"
        suggestion.decided_at = datetime.now(UTC)
        suggestion.decided_by = actor_id
        await self._session.flush()
        return suggestion, draft

    async def _apply_suggestion(
        self, suggestion: CatalogSuggestion, actor_id: uuid.UUID
    ) -> CatalogVersion:
        """Заводит синоним в черновике каталога."""
        if suggestion.canonical_parameter_id is None:
            raise ApiError(
                CatalogErrorCode.PARAMETER_NOT_FOUND,
                "Предложение не привязано к параметру каталога:"
                " принять его нечем",
                status_code=422,
                details={"suggestionId": str(suggestion.id)},
            )

        draft = await self._draft_for_edit(actor_id)
        # Параметр предложения относится к опубликованной версии, а alias нужно
        # завести в черновике: у клона тот же `code`, но другой идентификатор.
        published_parameter = await self._session.get(
            CanonicalParameter, suggestion.canonical_parameter_id
        )
        parameter = (
            await self._session.scalar(
                select(CanonicalParameter).where(
                    CanonicalParameter.catalog_version_id == draft.id,
                    CanonicalParameter.code == published_parameter.code,
                )
            )
            if published_parameter is not None
            else None
        )
        if parameter is None:
            raise ApiError(
                CatalogErrorCode.PARAMETER_NOT_FOUND,
                "В черновике каталога нет параметра, к которому относится"
                " предложение",
                status_code=422,
                details={"draftVersionId": str(draft.id)},
            )

        normalized = normalize(suggestion.synonym_text)
        existing = await self._session.scalar(
            select(ParameterAlias.id).where(
                ParameterAlias.catalog_version_id == draft.id,
                ParameterAlias.normalized_text == normalized,
            )
        )
        # Повторное принятие не должно падать на уникальности: если синоним в
        # черновике уже есть, работа считается сделанной.
        if existing is None:
            await self.create_alias(
                draft.id,
                AliasCreate(
                    canonical_parameter_id=parameter.id,
                    alias_text=suggestion.synonym_text,
                ),
            )
        return draft

    async def _draft_for_edit(self, actor_id: uuid.UUID) -> CatalogVersion:
        """Черновик, в который складываются принятые предложения.

        Берётся самый свежий существующий: собирать по черновику на каждый
        синоним значило бы плодить версии, которые потом некому публиковать.
        """
        draft = await self._session.scalar(
            select(CatalogVersion)
            .where(CatalogVersion.status.in_(("DRAFT", "VALID", "INVALID")))
            .order_by(CatalogVersion.created_at.desc())
            .limit(1)
        )
        if draft is not None:
            return draft

        published = await self.published_version()
        if published is None:
            raise ApiError(
                CatalogErrorCode.NO_PUBLISHED_CATALOG,
                "Нет опубликованной версии каталога: черновик не из чего"
                " создать",
                status_code=409,
            )
        return await self.create_version(
            VersionCreate(code=self._next_draft_code(published.code)), actor_id
        )

    @staticmethod
    def _next_draft_code(published_code: str) -> str:
        """Код черновика рядом с опубликованным: `catalog-1.0.0+aliases-<n>`."""
        suffix = uuid.uuid4().hex[:8]
        base = published_code[:80]
        return f"{base}-aliases-{suffix}"

    # --- валидация ------------------------------------------------------

    async def _collect_issues(
        self, version_id: uuid.UUID
    ) -> tuple[list[Issue], list[Issue]]:
        """Проверки, которые нельзя выразить ограничениями схемы."""
        errors: list[Issue] = []
        warnings: list[Issue] = []

        parameters = await self.list_parameters(version_id)
        units = {unit.id: unit for unit in await self.list_units(version_id)}

        for parameter in parameters:
            if parameter.canonical_unit_id is None:
                if parameter.value_kind in ("NUMBER", "RANGE"):
                    warnings.append(
                        Issue(
                            "MISSING_CANONICAL_UNIT",
                            f"parameters/{parameter.id}",
                            f"У числового параметра {parameter.code} нет"
                            " канонической единицы",
                        )
                    )
                continue
            unit = units.get(parameter.canonical_unit_id)
            if unit and parameter.dimension and unit.dimension != parameter.dimension:
                errors.append(
                    Issue(
                        CatalogErrorCode.UNIT_DIMENSION_MISMATCH,
                        f"parameters/{parameter.id}",
                        f"Параметр {parameter.code}: размерность {parameter.dimension}"
                        f" не совпадает с единицей {unit.code} ({unit.dimension})",
                    )
                )

        errors.extend(await self._alias_conflicts(version_id))
        errors.extend(await self._material_cycles(version_id))
        errors.extend(await self._template_conflicts(version_id, parameters))
        warnings.extend(await self._types_without_templates(version_id))
        return errors, warnings

    async def _alias_conflicts(self, version_id: uuid.UUID) -> list[Issue]:
        """Активные синонимы с одинаковым текстом и scope у разных параметров.

        Уникальный индекс покрывает пару (normalized, scope) целиком, но данные
        могли прийти клонированием из версии, где ограничение уже нарушалось.
        """
        rows = await self._session.execute(
            select(
                ParameterAlias.normalized_text,
                ParameterAlias.scope,
                func.count(func.distinct(ParameterAlias.canonical_parameter_id)),
            )
            .where(
                ParameterAlias.catalog_version_id == version_id,
                ParameterAlias.active.is_(True),
            )
            .group_by(ParameterAlias.normalized_text, ParameterAlias.scope)
            .having(
                func.count(func.distinct(ParameterAlias.canonical_parameter_id)) > 1
            )
        )
        return [
            Issue(
                CatalogErrorCode.ALIAS_CONFLICT,
                f"aliases/{normalized}",
                f"Синоним «{normalized}» в области {scope} ведёт к {count} параметрам",
            )
            for normalized, scope, count in rows.all()
        ]

    async def _material_cycles(self, version_id: uuid.UUID) -> list[Issue]:
        """Цикл в иерархии материалов: путь узла не содержит его собственный код."""
        nodes = await self.list_materials(version_id)
        by_id = {node.id: node for node in nodes}
        issues: list[Issue] = []
        for node in nodes:
            seen: set[uuid.UUID] = {node.id}
            current = node
            while current.parent_id is not None:
                if current.parent_id in seen:
                    issues.append(
                        Issue(
                            CatalogErrorCode.MATERIAL_HIERARCHY_CYCLE,
                            f"materials/{node.id}",
                            f"Материал {node.code} входит в цикл иерархии",
                        )
                    )
                    break
                seen.add(current.parent_id)
                parent = by_id.get(current.parent_id)
                if parent is None:
                    break
                current = parent
        return issues

    async def _template_conflicts(
        self, version_id: uuid.UUID, parameters: list[CanonicalParameter]
    ) -> list[Issue]:
        kinds = {parameter.id: parameter for parameter in parameters}
        templates = (
            await self._session.scalars(
                select(ParameterTemplate).where(
                    ParameterTemplate.catalog_version_id == version_id
                )
            )
        ).all()

        issues: list[Issue] = []
        for template in templates:
            parameter = kinds.get(template.canonical_parameter_id)
            if parameter is None or template.operator is None:
                continue
            allowed = OPERATORS_BY_VALUE_KIND.get(parameter.value_kind, ())
            if template.operator not in allowed:
                issues.append(
                    Issue(
                        CatalogErrorCode.TEMPLATE_OPERATOR_INCOMPATIBLE,
                        f"templates/{template.id}",
                        f"Оператор {template.operator} не применим к"
                        f" {parameter.value_kind} (параметр {parameter.code})",
                    )
                )
        return issues

    async def _types_without_templates(self, version_id: uuid.UUID) -> list[Issue]:
        rows = await self._session.execute(
            select(EquipmentType.id, EquipmentType.code)
            .outerjoin(
                ParameterTemplate,
                ParameterTemplate.equipment_type_id == EquipmentType.id,
            )
            .where(
                EquipmentType.catalog_version_id == version_id,
                EquipmentType.active.is_(True),
            )
            .group_by(EquipmentType.id, EquipmentType.code)
            .having(func.count(ParameterTemplate.id) == 0)
        )
        return [
            Issue(
                "MISSING_TEMPLATE_PARAMETER",
                f"equipment-types/{type_id}",
                f"У типа {code} нет ни одного шаблона параметров",
            )
            for type_id, code in rows.all()
        ]

    # --- вспомогательное ------------------------------------------------

    async def _clone_content(self, source_id: uuid.UUID, target_id: uuid.UUID) -> None:
        """Копирует содержимое версии в новый draft.

        Порядок важен: единицы до параметров (составной FK), типы до шаблонов.
        Идентификаторы новые, поэтому ссылки пересобираются по картам старый → новый.
        """
        unit_map: dict[uuid.UUID, uuid.UUID] = {}
        for unit in await self.list_units(source_id):
            clone = Unit(
                catalog_version_id=target_id,
                code=unit.code,
                symbol=unit.symbol,
                dimension=unit.dimension,
                active=unit.active,
            )
            self._session.add(clone)
            await self._session.flush()
            unit_map[unit.id] = clone.id

        for alias in (
            await self._session.scalars(
                select(UnitAlias).where(UnitAlias.catalog_version_id == source_id)
            )
        ).all():
            self._session.add(
                UnitAlias(
                    catalog_version_id=target_id,
                    unit_id=unit_map[alias.unit_id],
                    alias_text=alias.alias_text,
                    normalized_text=alias.normalized_text,
                    priority=alias.priority,
                    active=alias.active,
                )
            )

        parameter_map: dict[uuid.UUID, uuid.UUID] = {}
        for parameter in await self.list_parameters(source_id):
            clone = CanonicalParameter(
                catalog_version_id=target_id,
                code=parameter.code,
                display_name=parameter.display_name,
                value_kind=parameter.value_kind,
                dimension=parameter.dimension,
                canonical_unit_id=unit_map.get(parameter.canonical_unit_id),
                active=parameter.active,
            )
            self._session.add(clone)
            await self._session.flush()
            parameter_map[parameter.id] = clone.id

        for alias in await self.list_aliases(source_id):
            self._session.add(
                ParameterAlias(
                    catalog_version_id=target_id,
                    canonical_parameter_id=parameter_map[alias.canonical_parameter_id],
                    alias_text=alias.alias_text,
                    normalized_text=alias.normalized_text,
                    scope=alias.scope,
                    priority=alias.priority,
                    active=alias.active,
                )
            )

        for rule in await self.list_conversions(source_id):
            self._session.add(
                ConversionRule(
                    catalog_version_id=target_id,
                    from_unit_code=rule.from_unit_code,
                    to_unit_code=rule.to_unit_code,
                    dimension=rule.dimension,
                    formula_type=rule.formula_type,
                    scale=rule.scale,
                    offset_value=rule.offset_value,
                )
            )

        material_map: dict[uuid.UUID, uuid.UUID] = {}
        # Сортировка по пути гарантирует, что родитель скопирован раньше ребёнка.
        for node in await self.list_materials(source_id):
            clone = MaterialNode(
                catalog_version_id=target_id,
                parent_id=material_map.get(node.parent_id) if node.parent_id else None,
                code=node.code,
                path=node.path,
                name=node.name,
                active=node.active,
            )
            self._session.add(clone)
            await self._session.flush()
            material_map[node.id] = clone.id

        type_map: dict[uuid.UUID, uuid.UUID] = {}
        for equipment_type in await self.list_equipment_types(source_id):
            clone = EquipmentType(
                catalog_version_id=target_id,
                parent_id=None,
                code=equipment_type.code,
                name=equipment_type.name,
                classifier_labels=equipment_type.classifier_labels,
                active=equipment_type.active,
            )
            self._session.add(clone)
            await self._session.flush()
            type_map[equipment_type.id] = clone.id

        # Родители типов проставляются вторым проходом: порядок по коду не
        # гарантирует, что родитель встретился раньше ребёнка.
        for equipment_type in await self.list_equipment_types(source_id):
            if equipment_type.parent_id:
                clone_id = type_map[equipment_type.id]
                clone = await self._session.get(EquipmentType, clone_id)
                if clone is not None:
                    clone.parent_id = type_map.get(equipment_type.parent_id)

        for template in (
            await self._session.scalars(
                select(ParameterTemplate).where(
                    ParameterTemplate.catalog_version_id == source_id
                )
            )
        ).all():
            self._session.add(
                ParameterTemplate(
                    catalog_version_id=target_id,
                    equipment_type_id=type_map[template.equipment_type_id],
                    canonical_parameter_id=parameter_map[
                        template.canonical_parameter_id
                    ],
                    required=template.required,
                    operator=template.operator,
                    tolerance=template.tolerance,
                    priority=template.priority,
                )
            )
        await self._session.flush()

    async def _code_taken(self, code: str) -> bool:
        return bool(
            await self._session.scalar(
                select(CatalogVersion.id).where(CatalogVersion.code == code)
            )
        )

    async def _editable_version(self, version_id: uuid.UUID) -> CatalogVersion:
        version = await self.get_version(version_id)
        self._assert_editable(version)
        return version

    @staticmethod
    def _assert_editable(version: CatalogVersion) -> None:
        if not version.is_editable:
            raise ApiError(
                CatalogErrorCode.CATALOG_VERSION_IMMUTABLE,
                f"Версия в статусе {version.status} неизменяема: создайте новый draft",
                status_code=409,
            )

    @staticmethod
    def _assert_transition(version: CatalogVersion, target: str) -> None:
        if target not in ALLOWED_TRANSITIONS.get(version.status, ()):
            raise ApiError(
                CatalogErrorCode.INVALID_STATE_TRANSITION,
                f"Переход {version.status} → {target} не разрешён",
                status_code=409,
            )

    @staticmethod
    def _assert_version_match(version: CatalogVersion, expected: int) -> None:
        if version.version != expected:
            raise ApiError(
                CatalogErrorCode.EDIT_CONFLICT,
                "Версия каталога изменена другим пользователем",
                status_code=409,
                details={
                    "currentVersion": version.version,
                    "expectedVersion": expected,
                },
            )

    async def _exists(self, model, version_id: uuid.UUID, *conditions) -> bool:
        return bool(
            await self._session.scalar(
                select(model.id).where(
                    model.catalog_version_id == version_id, *conditions
                )
            )
        )

    async def _child(
        self,
        model,
        version_id: uuid.UUID,
        child_id: uuid.UUID,
        code: str,
    ):
        """Объект, принадлежащий именно этой версии каталога.

        Проверка версии обязательна: без неё можно было бы править содержимое
        одной версии, обращаясь по адресу другой.
        """
        entity = await self._session.scalar(
            select(model).where(
                model.id == child_id, model.catalog_version_id == version_id
            )
        )
        if entity is None:
            raise NotFoundError(
                "Объект каталога не найден",
                details={"id": str(child_id), "expected": code},
            )
        return entity


__all__ = ["CatalogsService", "CatalogErrorCode", "GLOBAL_SCOPE", "Issue"]
