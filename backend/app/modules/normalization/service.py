"""Deterministic catalog-aware normalization of verified raw parameters."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalogs.models import (
    CanonicalParameter,
    CatalogVersion,
    ConversionRule,
    ParameterAlias,
    Unit,
    UnitAlias,
)
from app.modules.catalogs.normalize import normalize as normalize_alias
from app.modules.extraction.models import RawParameter
from app.modules.normalization.models import NormalizationIssue, NormalizedParameter

_NUMBER = re.compile(r"^\s*(<=|>=|<|>)?\s*([-+]?\d+(?:[.,]\d+)?)\s*$")
_RANGE = re.compile(
    r"^\s*([-+]?\d+(?:[.,]\d+)?)\s*(?:\.\.|…|–|-)\s*"
    r"([-+]?\d+(?:[.,]\d+)?)\s*$"
)
_TRUE = {"true", "yes", "да", "1", "есть"}
_FALSE = {"false", "no", "нет", "0", "отсутствует"}


@dataclass(frozen=True)
class NormalizationStats:
    catalog_version_id: uuid.UUID | None
    normalized: int
    rejected: int


@dataclass(frozen=True)
class _Issue:
    kind: str
    description: str
    severity: str = "ERROR"


class ParameterNormalizer:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def normalize(
        self,
        parameters: list[RawParameter],
        *,
        catalog_version_id: uuid.UUID | None = None,
    ) -> NormalizationStats:
        """Нормализует параметры по каталогу.

        `catalog_version_id` задаёт версию явно: прогон pipeline фиксирует её при
        запуске, и воспроизводимость требует нормализовать именно по ней, а не по
        той, что опубликована сейчас. Без параметра берётся версия по умолчанию —
        так работает библиотека сертификатов, где прогон ни к чему не привязан.
        """
        if not parameters:
            return NormalizationStats(None, 0, 0)
        if catalog_version_id is not None:
            catalog = await self._session.get(CatalogVersion, catalog_version_id)
        else:
            catalog = await self._session.scalar(
                select(CatalogVersion)
                .where(CatalogVersion.status == "PUBLISHED")
                .order_by(
                    CatalogVersion.is_default.desc(),
                    CatalogVersion.published_at.desc(),
                    CatalogVersion.created_at.desc(),
                )
                .limit(1)
            )
        if catalog is None:
            return NormalizationStats(None, 0, 0)

        canonical = list(
            (
                await self._session.scalars(
                    select(CanonicalParameter).where(
                        CanonicalParameter.catalog_version_id == catalog.id,
                        CanonicalParameter.active.is_(True),
                    )
                )
            ).all()
        )
        aliases = list(
            (
                await self._session.scalars(
                    select(ParameterAlias).where(
                        ParameterAlias.catalog_version_id == catalog.id,
                        ParameterAlias.active.is_(True),
                    )
                )
            ).all()
        )
        units = list(
            (
                await self._session.scalars(
                    select(Unit).where(
                        Unit.catalog_version_id == catalog.id,
                        Unit.active.is_(True),
                    )
                )
            ).all()
        )
        unit_aliases = list(
            (
                await self._session.scalars(
                    select(UnitAlias).where(
                        UnitAlias.catalog_version_id == catalog.id,
                        UnitAlias.active.is_(True),
                    )
                )
            ).all()
        )
        conversions = list(
            (
                await self._session.scalars(
                    select(ConversionRule).where(
                        ConversionRule.catalog_version_id == catalog.id
                    )
                )
            ).all()
        )

        canonical_by_id = {item.id: item for item in canonical}
        parameter_names: dict[str, set[uuid.UUID]] = {}
        for item in canonical:
            parameter_names.setdefault(normalize_alias(item.display_name), set()).add(
                item.id
            )
        for alias in aliases:
            parameter_names.setdefault(alias.normalized_text, set()).add(
                alias.canonical_parameter_id
            )

        units_by_id = {item.id: item for item in units}
        unit_names: dict[str, set[uuid.UUID]] = {}
        for unit in units:
            for value in (unit.code, unit.symbol):
                unit_names.setdefault(normalize_alias(value), set()).add(unit.id)
        for alias in unit_aliases:
            unit_names.setdefault(alias.normalized_text, set()).add(alias.unit_id)

        conversion_map = {
            (item.from_unit_code, item.to_unit_code): item for item in conversions
        }
        normalized_count = 0
        rejected_count = 0
        for raw in parameters:
            issues: list[_Issue] = []
            matches = parameter_names.get(normalize_alias(raw.name_text), set())
            parameter = (
                canonical_by_id[next(iter(matches))] if len(matches) == 1 else None
            )
            if not matches:
                issues.append(
                    _Issue(
                        "UNKNOWN_PARAMETER",
                        "Название не найдено в опубликованной версии каталога",
                    )
                )
            elif len(matches) > 1:
                issues.append(
                    _Issue(
                        "AMBIGUOUS_PARAMETER_ALIAS",
                        "Название соответствует нескольким параметрам каталога",
                    )
                )

            source_unit = None
            if raw.unit_text:
                unit_matches = unit_names.get(normalize_alias(raw.unit_text), set())
                if len(unit_matches) == 1:
                    source_unit = units_by_id[next(iter(unit_matches))]
                elif not unit_matches:
                    issues.append(
                        _Issue(
                            "UNKNOWN_UNIT",
                            "Единица не найдена в опубликованной версии каталога",
                        )
                    )
                else:
                    issues.append(
                        _Issue(
                            "AMBIGUOUS_UNIT",
                            "Единица соответствует нескольким записям каталога",
                        )
                    )

            value_kind = parameter.value_kind if parameter else "UNKNOWN"
            typed_value, parse_issue = _parse_value(value_kind, raw.value_text)
            if parse_issue:
                issues.append(parse_issue)

            target_unit = (
                units_by_id.get(parameter.canonical_unit_id)
                if parameter and parameter.canonical_unit_id
                else None
            )
            if parameter and parameter.dimension and raw.unit_text is None:
                issues.append(
                    _Issue(
                        "MISSING_UNIT",
                        "Для размерного параметра отсутствует единица",
                    )
                )
            if (
                parameter
                and parameter.dimension
                and source_unit
                and source_unit.dimension != parameter.dimension
            ):
                issues.append(
                    _Issue(
                        "UNIT_DIMENSION_MISMATCH",
                        "Размерность единицы не совпадает с параметром",
                    )
                )
            if (
                typed_value
                and source_unit
                and target_unit
                and source_unit.id != target_unit.id
                and not any(issue.severity == "ERROR" for issue in issues)
            ):
                rule = conversion_map.get((source_unit.code, target_unit.code))
                if rule is None:
                    issues.append(
                        _Issue(
                            "CONVERSION_NOT_FOUND",
                            "Нет правила перевода в каноническую единицу",
                        )
                    )
                else:
                    typed_value = _convert(typed_value, rule.scale, rule.offset_value)

            if typed_value is not None:
                typed_value["sourceUnit"] = source_unit.code if source_unit else None
                typed_value["canonicalUnit"] = (
                    target_unit.code
                    if target_unit
                    else source_unit.code
                    if source_unit
                    else None
                )

            ok = parameter is not None and not any(
                issue.severity == "ERROR" for issue in issues
            )
            normalized = await self._session.scalar(
                select(NormalizedParameter).where(
                    NormalizedParameter.raw_parameter_id == raw.id
                )
            )
            if normalized is None:
                normalized = NormalizedParameter(
                    id=uuid.uuid4(),
                    raw_parameter_id=raw.id,
                    version=1,
                )
                self._session.add(normalized)
            else:
                normalized.version += 1
                await self._session.execute(
                    delete(NormalizationIssue).where(
                        NormalizationIssue.normalized_parameter_id == normalized.id
                    )
                )

            normalized.catalog_version_id = catalog.id
            normalized.canonical_parameter_id = parameter.id if parameter else None
            normalized.canonical_name = (
                parameter.display_name if parameter else raw.name_text
            )
            normalized.value_kind = value_kind
            normalized.typed_value = typed_value
            normalized.canonical_unit_id = (
                target_unit.id
                if target_unit
                else source_unit.id
                if source_unit
                else None
            )
            normalized.normalized_ok = ok
            self._session.add(normalized)
            await self._session.flush()
            for issue in issues:
                self._session.add(
                    NormalizationIssue(
                        normalized_parameter_id=normalized.id,
                        issue_type=issue.kind,
                        description=issue.description,
                        severity=issue.severity,
                    )
                )
            if ok:
                normalized_count += 1
            else:
                rejected_count += 1

        return NormalizationStats(catalog.id, normalized_count, rejected_count)


def _parse_value(
    value_kind: str, value: str | None
) -> tuple[dict | None, _Issue | None]:
    source = (value or "").strip()
    if value_kind == "NUMBER":
        match = _NUMBER.fullmatch(source)
        if match is None:
            return None, _Issue("NUMBER_PARSE_FAILED", "Число не удалось разобрать")
        return {
            "kind": "NUMBER",
            "value": _decimal_text(match.group(2)),
            "operator": match.group(1),
        }, None
    if value_kind == "RANGE":
        match = _RANGE.fullmatch(source)
        if match is None:
            return None, _Issue("RANGE_PARSE_FAILED", "Диапазон не удалось разобрать")
        lower = Decimal(match.group(1).replace(",", "."))
        upper = Decimal(match.group(2).replace(",", "."))
        if lower > upper:
            return None, _Issue(
                "RANGE_BOUNDS_REVERSED",
                "Нижняя граница диапазона больше верхней",
            )
        return {
            "kind": "RANGE",
            "min": _decimal_text(str(lower)),
            "max": _decimal_text(str(upper)),
        }, None
    if value_kind == "BOOLEAN":
        folded = source.casefold()
        if folded in _TRUE:
            return {"kind": "BOOLEAN", "value": True}, None
        if folded in _FALSE:
            return {"kind": "BOOLEAN", "value": False}, None
        return None, _Issue(
            "BOOLEAN_PARSE_FAILED", "Логическое значение не удалось разобрать"
        )
    if value_kind == "SET":
        values = [item.strip() for item in re.split(r"[,;]", source) if item.strip()]
        return {"kind": "SET", "values": values}, None
    if value_kind in {"TEXT", "MATERIAL", "UNKNOWN"}:
        return {"kind": value_kind, "value": source}, None
    return None, _Issue("VALUE_KIND_UNSUPPORTED", "Тип значения не поддерживается")


def _convert(typed: dict, scale: Decimal, offset: Decimal) -> dict:
    converted = dict(typed)
    if typed["kind"] == "NUMBER":
        converted["value"] = _decimal_text(
            str(Decimal(typed["value"]) * scale + offset)
        )
    elif typed["kind"] == "RANGE":
        converted["min"] = _decimal_text(str(Decimal(typed["min"]) * scale + offset))
        converted["max"] = _decimal_text(str(Decimal(typed["max"]) * scale + offset))
    return converted


def _decimal_text(value: str) -> str:
    try:
        parsed = Decimal(value.replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc
    return format(parsed.normalize(), "f")
