"""Closed, deterministic DSL for attributes embedded in item text."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.parse_rules import _validate_recipe_format
from app.parsing.grid import Cell, SheetGrid
from app.parsing.mapping import (
    ColumnMapping,
    InlineAttributeMapping,
    MappingDecision,
    TableMapping,
)
from app.parsing.pipeline import parse_grids
from app.parsing.rules import recipe_from_decision, recipe_to_decision


def _decision() -> MappingDecision:
    return MappingDecision(
        auto_ok=True,
        tables=[
            TableMapping(
                sheet_name="Прайс",
                header_row_index=0,
                columns=[
                    ColumnMapping(index=0, role="name", header="Наименование"),
                    ColumnMapping(index=1, role="price", header="Цена"),
                ],
                inline_attributes=[
                    InlineAttributeMapping(
                        source_column_index=0,
                        attribute_code="rated_voltage",
                        display_name="Номинальное напряжение",
                        value_kind="decimal",
                        canonical_unit="V",
                        extractor_kind="number_with_unit",
                    ),
                    InlineAttributeMapping(
                        source_column_index=0,
                        attribute_code="ingress_protection_rating",
                        display_name="Степень защиты IP",
                        value_kind="option",
                        canonical_unit=None,
                        extractor_kind="ip_code",
                    ),
                ],
            )
        ],
    )


def test_closed_inline_dsl_extracts_typed_facts_from_item_name() -> None:
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [Cell("Наименование"), Cell("Цена")],
            [Cell("Насос 220 В IP54"), Cell("3400")],
        ],
    )

    result = parse_grids([grid], _decision())

    assert [
        (
            value.attribute_code,
            value.raw_value,
            value.value_kind,
            value.canonical_unit,
            value.source_column_index,
        )
        for value in result.items[0].attribute_values
    ] == [
        ("rated_voltage", "220", "decimal", "V", 0),
        ("ingress_protection_rating", "IP54", "option", None, 0),
    ]


def test_inline_dsl_survives_recipe_round_trip_without_regex() -> None:
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [Cell("Наименование"), Cell("Цена")],
            [Cell("Насос 220 В IP54"), Cell("3400")],
        ],
    )

    recipe = recipe_from_decision(_decision(), [grid])
    restored = recipe_to_decision(recipe, [grid])

    assert recipe["sheets"][0]["inline_attributes"][0] == {
        "source_column_index": 0,
        "attribute_code": "rated_voltage",
        "display_name": "Номинальное напряжение",
        "value_kind": "decimal",
        "canonical_unit": "V",
        "extractor_kind": "number_with_unit",
    }
    assert restored.tables[0].inline_attributes == _decision().tables[0].inline_attributes


def test_recipe_api_rejects_arbitrary_inline_regex_or_unknown_extractor() -> None:
    recipe = {
        "sheets": [
            {
                "inline_attributes": [
                    {
                        "source_column_index": 0,
                        "attribute_code": "danger",
                        "display_name": "Danger",
                        "value_kind": "text",
                        "canonical_unit": None,
                        "extractor_kind": "regex",
                        "pattern": ".*",
                    }
                ]
            }
        ]
    }

    with pytest.raises(HTTPException) as error:
        _validate_recipe_format(recipe)

    assert error.value.status_code == 422
    assert "inline extractor" in str(error.value.detail)

