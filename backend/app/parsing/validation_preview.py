"""Compact, deterministic samples for operator review of a parser dry-run."""
from __future__ import annotations

from typing import Any

from app.parsing.pipeline import ParseResult, ParsedItem


DEFAULT_VALIDATION_SAMPLE_LIMIT = 24


def _item_identity(item: ParsedItem) -> tuple[object, ...]:
    return (
        item.source_sheet,
        item.category_path,
        item.source_name,
        item.source_article,
    )


def validation_sample(item: ParsedItem) -> dict[str, Any]:
    return {
        "source_sheet": item.source_sheet,
        "source_row": item.source_row,
        "name": item.source_name,
        "article": item.source_article,
        "unit": item.unit_code,
        "category": item.category_path,
        "attributes": [
            {
                "code": attribute.attribute_code,
                "name": attribute.source_header,
                "raw_value": attribute.raw_value,
                "unit": attribute.canonical_unit,
            }
            for attribute in item.attribute_values
        ],
        "prices": [
            {
                "kind": value.kind,
                "amount": str(value.amount) if value.amount is not None else None,
                "is_on_request": value.is_on_request,
                "vat_mode": value.vat_mode,
                "currency": value.currency,
                "source_column_index": value.source_column_index,
            }
            for value in item.values
        ],
    }


def build_validation_samples(
    result: ParseResult, *, limit: int = DEFAULT_VALIDATION_SAMPLE_LIMIT
) -> list[dict[str, Any]]:
    """Represent source sheets, matrix variants, then distinct identities.

    A workbook may yield hundreds of positions from its first sheet. Selecting
    the first N identities would hide later matrices and special tables from
    the operator, so the first pass takes one item per sheet. The next pass
    adds an attributed item per sheet (notably a matrix cell), then distinct
    identities/categories before filling remaining slots.
    """

    if limit <= 0:
        return []
    selected: list[ParsedItem] = []
    selected_ids: set[int] = set()
    sheets: set[str] = set()
    for item in result.items:
        if item.source_sheet in sheets:
            continue
        sheets.add(item.source_sheet)
        selected.append(item)
        selected_ids.add(id(item))
        if len(selected) >= limit:
            return [validation_sample(value) for value in selected]

    attribute_sheets = {
        item.source_sheet for item in selected if item.attribute_values
    }
    for item in result.items:
        if (
            id(item) in selected_ids
            or not item.attribute_values
            or item.source_sheet in attribute_sheets
        ):
            continue
        attribute_sheets.add(item.source_sheet)
        selected.append(item)
        selected_ids.add(id(item))
        if len(selected) >= limit:
            return [validation_sample(value) for value in selected]

    identities: set[tuple[object, ...]] = set()
    for item in result.items:
        if id(item) in selected_ids:
            identities.add(_item_identity(item))
            continue
        identity = _item_identity(item)
        if identity in identities:
            continue
        identities.add(identity)
        selected.append(item)
        selected_ids.add(id(item))
        if len(selected) >= limit:
            return [validation_sample(value) for value in selected]
    for item in result.items:
        if id(item) in selected_ids:
            continue
        selected.append(item)
        if len(selected) >= limit:
            break
    return [validation_sample(value) for value in selected]
