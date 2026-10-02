"""Relational persistence and coercion for supplier price-list attributes.

Attribute facts deliberately stay scalar: a source cell becomes one row with
``raw_value TEXT`` plus, when conversion is safe, one native database value.
No JSON object is used to carry an int, bool, date, or text characteristic.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    AttributeDefinition,
    AttributeOption,
    SupplierAttributeKey,
    SupplierPriceItem,
    SupplierPriceItemAttributeValue,
)
from app.parsing.headers import normalize_header
from app.parsing.mapping import ATTRIBUTE_DEFINITION_DISPLAY_NAMES
from app.parsing.pipeline import ParsedAttribute


ATTRIBUTE_VALUE_KINDS = frozenset(
    {"text", "integer", "decimal", "boolean", "date", "datetime", "option"}
)

_NUMBER_RE = re.compile(
    r"^\s*([+-]?(?:\d{1,3}(?:[\s\u00a0]\d{3})+|\d+)(?:[,.]\d+)?)\s*[^\d]*$"
)
_TRUE_VALUES = {"1", "true", "yes", "y", "да", "есть", "в наличии"}
_FALSE_VALUES = {"0", "false", "no", "n", "нет", "отсутствует", "нет в наличии"}
_WEIGHT_FACTORS_TO_KG = {
    "mg": Decimal("0.000001"),
    "g": Decimal("0.001"),
    "kg": Decimal("1"),
    "t": Decimal("1000"),
}
_VOLUME_FACTORS_TO_M3 = {
    "mm3": Decimal("0.000000001"),
    "cm3": Decimal("0.000001"),
    "l": Decimal("0.001"),
    "m3": Decimal("1"),
}


@dataclass
class AttributePersistenceContext:
    """Per-session cache; avoids a lookup for every cell in a large price list."""

    definitions_by_code: dict[str, AttributeDefinition] = field(default_factory=dict)
    keys_by_supplier_header: dict[tuple[uuid.UUID, str], SupplierAttributeKey] = field(
        default_factory=dict
    )
    options_by_definition_value: dict[tuple[uuid.UUID, str], AttributeOption] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class AttributePersistenceWarning:
    source_header: str
    source_value: str
    source_column_index: int
    message: str


def _normalized_header(value: str) -> str:
    normalized = normalize_header(value)
    return normalized or "unnamed"


def _source_draft_code(supplier_id: uuid.UUID, normalized_header: str) -> str:
    """Stable, supplier-scoped dictionary code for an unknown source header."""
    digest = hashlib.sha256(
        f"{supplier_id}:{normalized_header}".encode("utf-8")
    ).hexdigest()[:24]
    return f"source_{digest}"


def _option_code(raw_value: str) -> str:
    normalized = normalize_header(raw_value) or raw_value.casefold().strip()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"option_{digest}"


def _valid_kind(value_kind: str | None) -> str:
    return value_kind if value_kind in ATTRIBUTE_VALUE_KINDS else "text"


async def _get_or_create_definition(
    session: AsyncSession,
    *,
    supplier_id: uuid.UUID,
    source_header: str,
    attribute_code: str | None,
    value_kind: str,
    canonical_unit: str | None,
    cache: AttributePersistenceContext,
) -> AttributeDefinition:
    normalized_header = _normalized_header(source_header)
    code = attribute_code or _source_draft_code(supplier_id, normalized_header)
    if code in cache.definitions_by_code:
        return cache.definitions_by_code[code]

    definition = await session.scalar(
        select(AttributeDefinition).where(AttributeDefinition.code == code)
    )
    if definition is None:
        definition = AttributeDefinition(
            id=uuid.uuid4(),
            code=code,
            display_name=ATTRIBUTE_DEFINITION_DISPLAY_NAMES.get(code, source_header),
            value_kind=_valid_kind(value_kind),
            canonical_unit=canonical_unit,
            status="active" if attribute_code else "draft",
        )
        session.add(definition)
    elif attribute_code and canonical_unit and definition.canonical_unit is None:
        # Проверенное правило может уточнить единицу старого определения,
        # созданного до появления канонических единиц.
        definition.canonical_unit = canonical_unit
    cache.definitions_by_code[code] = definition
    return definition


async def _get_or_create_source_key(
    session: AsyncSession,
    *,
    supplier_id: uuid.UUID,
    source_header: str,
    definition: AttributeDefinition,
    explicit_mapping: bool,
    cache: AttributePersistenceContext,
) -> SupplierAttributeKey:
    normalized_header = _normalized_header(source_header)
    key_cache_key = (supplier_id, normalized_header)
    if key_cache_key in cache.keys_by_supplier_header:
        source_key = cache.keys_by_supplier_header[key_cache_key]
    else:
        source_key = await session.scalar(
            select(SupplierAttributeKey)
            .where(
                SupplierAttributeKey.supplier_id == supplier_id,
                SupplierAttributeKey.normalized_header == normalized_header,
            )
            .options(selectinload(SupplierAttributeKey.attribute_definition))
        )
        if source_key is None:
            source_key = SupplierAttributeKey(
                id=uuid.uuid4(),
                supplier_id=supplier_id,
                source_header=source_header,
                normalized_header=normalized_header,
                attribute_definition=definition,
            )
            session.add(source_key)
        cache.keys_by_supplier_header[key_cache_key] = source_key

    # An explicit parser-rule mapping is the only automatic operation allowed
    # to remap a known source header. Historic facts retain their own direct
    # definition snapshot, so the change is safe for future imports only.
    if explicit_mapping and source_key.attribute_definition is not definition:
        source_key.attribute_definition = definition
    return source_key


async def _get_or_create_option(
    session: AsyncSession,
    *,
    definition: AttributeDefinition,
    raw_value: str,
    cache: AttributePersistenceContext,
) -> AttributeOption:
    option_code = _option_code(raw_value)
    cache_key = (definition.id, option_code)
    if cache_key in cache.options_by_definition_value:
        return cache.options_by_definition_value[cache_key]
    option = await session.scalar(
        select(AttributeOption).where(
            AttributeOption.attribute_definition_id == definition.id,
            AttributeOption.code == option_code,
        )
    )
    if option is None:
        option = AttributeOption(
            id=uuid.uuid4(),
            attribute_definition=definition,
            code=option_code,
            display_name=raw_value,
        )
        session.add(option)
    cache.options_by_definition_value[cache_key] = option
    return option


def _parse_decimal(raw_value: str) -> Decimal:
    match = _NUMBER_RE.fullmatch(raw_value)
    if match is None:
        raise ValueError("ожидалось числовое значение")
    normalized = match.group(1).replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:  # defensive; regex above should prevent it
        raise ValueError("ожидалось числовое значение") from exc


def _weight_source_unit(*source_texts: str) -> str | None:
    """Infer a unit explicitly stated in a source header or value.

    The canonical unit of the shared ``weight`` definition is kg.  A header
    such as «Вес, грамм» must therefore convert 70 to 0.07 rather than merely
    relabel 70 as kilograms.
    """
    tokens = set()
    for source_text in source_texts:
        tokens.update(normalize_header(source_text).split())
    if "мг" in tokens or any(token.startswith("миллиграм") for token in tokens):
        return "mg"
    if "кг" in tokens or any(token.startswith("килограмм") for token in tokens):
        return "kg"
    if "т" in tokens or any(token.startswith("тонн") for token in tokens):
        return "t"
    if {"г", "гр"} & tokens or any(token.startswith("грамм") for token in tokens):
        return "g"
    return None


def _volume_source_unit(*source_texts: str) -> str | None:
    source = " ".join(source_texts).casefold().replace("³", "3")
    normalized = normalize_header(source)
    if re.search(r"\bмм\s*3\b|\bmm\s*3\b", normalized):
        return "mm3"
    if re.search(r"\bсм\s*3\b|\bcm\s*3\b|куб\w*\s+сантим", normalized):
        return "cm3"
    if re.search(r"\bм\s*3\b|\bm\s*3\b|куб\w*\s+метр", normalized):
        return "m3"
    if re.search(r"\bл\b|\bl\b|литр", normalized):
        return "l"
    return None


def _normalized_decimal_value(
    raw_value: str, *, definition: AttributeDefinition, source_header: str
) -> Decimal:
    value = _parse_decimal(raw_value)
    if definition.code == "weight" and definition.canonical_unit == "kg":
        source_unit = _weight_source_unit(source_header, raw_value)
        if source_unit is not None:
            return value * _WEIGHT_FACTORS_TO_KG[source_unit]
    if definition.code == "volume" and definition.canonical_unit == "m3":
        source_unit = _volume_source_unit(source_header, raw_value)
        if source_unit is not None:
            return value * _VOLUME_FACTORS_TO_M3[source_unit]
    return value


def _parse_boolean(raw_value: str) -> bool:
    normalized = " ".join(raw_value.casefold().split())
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError("ожидалось логическое значение («да»/«нет»)")


def _parse_date(raw_value: str) -> date:
    normalized = raw_value.strip()
    for parser in (date.fromisoformat, lambda value: datetime.strptime(value, "%d.%m.%Y").date()):
        try:
            return parser(normalized)
        except ValueError:
            pass
    raise ValueError("ожидалась дата в формате YYYY-MM-DD или DD.MM.YYYY")


def _parse_datetime(raw_value: str) -> datetime:
    normalized = raw_value.strip().replace("Z", "+00:00")
    parsers = (
        datetime.fromisoformat,
        lambda value: datetime.strptime(value, "%d.%m.%Y %H:%M"),
        lambda value: datetime.strptime(value, "%d.%m.%Y %H:%M:%S"),
    )
    for parser in parsers:
        try:
            result = parser(normalized)
            return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise ValueError("ожидались дата и время в ISO-формате или DD.MM.YYYY HH:MM")


async def persist_attribute_value(
    session: AsyncSession,
    *,
    item: SupplierPriceItem,
    supplier_id: uuid.UUID,
    parsed: ParsedAttribute,
    cache: AttributePersistenceContext,
) -> AttributePersistenceWarning | None:
    """Attach one parsed source fact to an item and retain conversion errors.

    The definition's already stored type wins over a conflicting rule hint. It
    prevents a previously approved canonical attribute from being silently
    retagged just because a later supplier file contains malformed data.
    """
    definition = await _get_or_create_definition(
        session,
        supplier_id=supplier_id,
        source_header=parsed.source_header,
        attribute_code=parsed.attribute_code,
        value_kind=parsed.value_kind,
        canonical_unit=parsed.canonical_unit,
        cache=cache,
    )
    source_key = await _get_or_create_source_key(
        session,
        supplier_id=supplier_id,
        source_header=parsed.source_header,
        definition=definition,
        explicit_mapping=parsed.attribute_code is not None,
        cache=cache,
    )

    # A key can already point at a reviewed canonical definition.  When the
    # parser did not explicitly override it, use that mapping for new facts.
    if parsed.attribute_code is None and source_key.attribute_definition is not None:
        definition = source_key.attribute_definition

    fact = SupplierPriceItemAttributeValue(
        id=uuid.uuid4(),
        item=item,
        source_key=source_key,
        attribute_definition=definition,
        value_kind=definition.value_kind,
        ordinal=parsed.ordinal,
        source_column_index=parsed.source_column_index,
        raw_value=parsed.raw_value,
    )

    try:
        if definition.value_kind == "text":
            fact.value_text = parsed.raw_value
        elif definition.value_kind == "integer":
            parsed_number = _parse_decimal(parsed.raw_value)
            if parsed_number != parsed_number.to_integral_value():
                raise ValueError("ожидалось целое число")
            fact.value_integer = int(parsed_number)
        elif definition.value_kind == "decimal":
            fact.value_decimal = _normalized_decimal_value(
                parsed.raw_value, definition=definition, source_header=parsed.source_header
            )
        elif definition.value_kind == "boolean":
            fact.value_boolean = _parse_boolean(parsed.raw_value)
        elif definition.value_kind == "date":
            fact.value_date = _parse_date(parsed.raw_value)
        elif definition.value_kind == "datetime":
            fact.value_timestamp = _parse_datetime(parsed.raw_value)
        elif definition.value_kind == "option":
            fact.option = await _get_or_create_option(
                session, definition=definition, raw_value=parsed.raw_value, cache=cache
            )
        else:  # should be protected by model CHECK and _valid_kind
            raise ValueError(f"неподдерживаемый тип характеристики: {definition.value_kind}")
    except ValueError as exc:
        fact.parse_status = "invalid"
        fact.parse_error = str(exc)
        session.add(fact)
        return AttributePersistenceWarning(
            source_header=parsed.source_header,
            source_value=parsed.raw_value,
            source_column_index=parsed.source_column_index,
            message=(
                f"Характеристика «{parsed.source_header}» сохранена как исходное значение, "
                f"но не приведена к типу {definition.value_kind}: {exc}"
            ),
        )

    session.add(fact)
    return None


def attribute_scalar_value(value: SupplierPriceItemAttributeValue):
    """Return the single display/API scalar without exposing a JSON object."""
    if value.parse_status == "invalid":
        return value.raw_value
    if value.value_kind == "text":
        return value.value_text
    if value.value_kind == "integer":
        return value.value_integer
    if value.value_kind == "decimal":
        # JSON has no decimal primitive.  A non-exponential string keeps the
        # value exact without leaking the database column's fixed scale
        # (``42.000000``) into the product-facing API.
        if value.value_decimal is None:
            return None
        normalized = format(value.value_decimal.normalize(), "f")
        return "0" if normalized == "-0" else normalized
    if value.value_kind == "boolean":
        return value.value_boolean
    if value.value_kind == "date":
        return value.value_date.isoformat() if value.value_date is not None else None
    if value.value_kind == "datetime":
        return value.value_timestamp.isoformat() if value.value_timestamp is not None else None
    if value.value_kind == "option":
        return value.option.display_name if value.option is not None else value.raw_value
    return value.raw_value
