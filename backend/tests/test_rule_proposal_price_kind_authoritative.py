"""Compiler regression units for P0-1: deterministic price_kind wins.

An LLM structured descriptor must not invert the financial semantics of an
explicit source price header.  When the deterministic header observation is
already a price with a concrete kind (wholesale/retail/dealer/purchase), a
contradicting model kind is normalised to the deterministic one and the
compiler records a normalisation warning.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.parsing.grid import Cell, SheetGrid
from app.parsing.mapping import MappingDecision
from app.parsing.rule_proposal import (
    build_tabular_section_contexts,
    compile_section_proposal,
    dry_run_decision,
    parse_proposal,
)


def _row(*values: object | None) -> list[Cell]:
    return [Cell(value=value) for value in values]


def _model_kind_payload(context: dict, grid: SheetGrid, model_kind: str) -> dict:
    return {
        "schema_version": "price-rule-proposal-v1",
        "decision": "propose",
        "rule_name": "Детерминированный price_kind",
        "loader": "xlsx",
        "source_section_id": context["section_id"],
        "mapping": {
            "sheet_name": grid.name,
            "header_row_index": 0,
            "name_fallback_column_indexes": [],
            "columns": [
                {
                    "index": 0,
                    "role": "name",
                    "price_descriptor": None,
                    "attribute_code": None,
                    "attribute_value_kind": None,
                    "canonical_unit": None,
                },
                {
                    "index": 1,
                    "role": "price",
                    "price_descriptor": {"kind": model_kind, "vat_mode": "unknown"},
                    "attribute_code": None,
                    "attribute_value_kind": None,
                    "canonical_unit": None,
                },
            ],
        },
        "discovered_attributes": [],
        "evidence": [
            {"row_index": 0, "column_index": index, "note": "шапка"}
            for index in range(2)
        ],
        "unresolved": [],
        "confidence": 0.95,
        "requires_manual_review": True,
    }


def _compile_price_header(price_header: str, model_kind: str):
    grid = SheetGrid(
        name="Прайс",
        rows=[
            _row("Наименование", price_header),
            _row("Клапан", "1200"),
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    payload = _model_kind_payload(context, grid, model_kind)
    return compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )


def _price_kind_of(compiled) -> str:
    price = next(
        column for column in compiled.table.columns if column.role == "price"
    )
    return price.price_descriptor["kind"]


@pytest.mark.parametrize(
    ("price_header", "model_kind", "expected_kind"),
    [
        ("Цена ОПТ", "retail", "wholesale"),
        ("Цена Розница", "wholesale", "retail"),
        ("ОПТ", "purchase", "wholesale"),
    ],
)
def test_conflicting_model_kind_is_normalised_to_deterministic_kind(
    price_header: str, model_kind: str, expected_kind: str
) -> None:
    compiled = _compile_price_header(price_header, model_kind)

    assert _price_kind_of(compiled) == expected_kind
    warnings = "\n".join(compiled.validation["normalisation_warnings"])
    assert f"price_kind модели {model_kind}" in warnings
    assert f"детерминированным {expected_kind}" in warnings


def test_matching_model_kind_is_accepted_without_price_kind_warning() -> None:
    compiled = _compile_price_header("Цена ОПТ", "wholesale")

    assert _price_kind_of(compiled) == "wholesale"
    assert not any(
        "price_kind" in warning
        for warning in compiled.validation["normalisation_warnings"]
    )


def test_inverted_opt_roznitsa_pair_follows_source_headers() -> None:
    grid = SheetGrid(
        name="Прайс",
        rows=[
            _row(
                "Наименование",
                "Цена ОПТ",
                "Цена Розница",
            ),
            _row("Клапан", "1200", "1500"),
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    columns = [
        {
            "index": 0,
            "role": "name",
            "price_descriptor": None,
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        },
        {
            "index": 1,
            "role": "price",
            "price_descriptor": {"kind": "retail", "vat_mode": "unknown"},
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        },
        {
            "index": 2,
            "role": "price",
            "price_descriptor": {"kind": "wholesale", "vat_mode": "unknown"},
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        },
    ]
    payload = {
        "schema_version": "price-rule-proposal-v1",
        "decision": "propose",
        "rule_name": "Инверсия пары цен",
        "loader": "xlsx",
        "source_section_id": context["section_id"],
        "mapping": {
            "sheet_name": grid.name,
            "header_row_index": 0,
            "name_fallback_column_indexes": [],
            "columns": columns,
        },
        "discovered_attributes": [],
        "evidence": [
            {"row_index": 0, "column_index": index, "note": "шапка"}
            for index in range(3)
        ],
        "unresolved": [],
        "confidence": 0.9,
        "requires_manual_review": True,
    }
    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )

    kinds = {
        column.index: column.price_descriptor["kind"]
        for column in compiled.table.columns
        if column.role == "price"
    }
    assert kinds == {1: "wholesale", 2: "retail"}
    assert len(compiled.validation["normalisation_warnings"]) == 2


def test_generic_price_header_stays_model_decidable() -> None:
    """A bare ``Цена`` resolves to kind other, so the model
    may still choose the concrete kind for an ambiguous supplier heading."""
    compiled = _compile_price_header("Цена", "retail")

    assert _price_kind_of(compiled) == "retail"
    assert not any(
        "price_kind" in warning
        for warning in compiled.validation["normalisation_warnings"]
    )


def test_dry_run_values_follow_normalised_deterministic_kinds() -> None:
    """Report control example: 618,40 under ``Цена ОПТ``
    parses as wholesale and 834,84 under ``Цена Розница``
    as retail even when the model inverts both structured kinds."""
    grid = SheetGrid(
        name="Рукава ПК",
        rows=[
            _row(
                "Наименование",
                "Цена ОПТ",
                "Цена Розница",
            ),
            _row("Рукав ПК-р", "618,40", "834,84"),
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    columns = [
        {
            "index": 0,
            "role": "name",
            "price_descriptor": None,
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        },
        {
            "index": 1,
            "role": "price",
            "price_descriptor": {"kind": "retail", "vat_mode": "unknown"},
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        },
        {
            "index": 2,
            "role": "price",
            "price_descriptor": {"kind": "wholesale", "vat_mode": "unknown"},
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        },
    ]
    payload = {
        "schema_version": "price-rule-proposal-v1",
        "decision": "propose",
        "rule_name": "Контрольный пример отчёта",
        "loader": "xlsx",
        "source_section_id": context["section_id"],
        "mapping": {
            "sheet_name": grid.name,
            "header_row_index": 0,
            "name_fallback_column_indexes": [],
            "columns": columns,
        },
        "discovered_attributes": [],
        "evidence": [
            {"row_index": 0, "column_index": index, "note": "шапка"}
            for index in range(3)
        ],
        "unresolved": [],
        "confidence": 0.9,
        "requires_manual_review": True,
    }
    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )
    result, validation = dry_run_decision(
        [grid], MappingDecision(tables=[compiled.table], auto_ok=True)
    )

    assert validation["passed"] is True
    parsed = [
        (value.amount, value.kind) for item in result.items for value in item.values
    ]
    assert parsed == [
        (Decimal("618.40"), "wholesale"),
        (Decimal("834.84"), "retail"),
    ]
