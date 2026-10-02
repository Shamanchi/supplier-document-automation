from __future__ import annotations

from copy import deepcopy

import pytest

from app.parsing.grid import Cell, SheetGrid
from app.parsing.rule_proposal import build_tabular_rule_generation_prompt
from app.parsing.rule_proposal_handlers import (
    build_rule_section_contexts,
    build_rule_section_specs,
)


def _module():
    from app.parsing import section_diagnostics

    return section_diagnostics


def _row(row_index: int, values: list[object]) -> dict:
    return {
        "row_index": row_index,
        "cells": [
            {"value": None if value is None else str(value), "is_merged_fill": False, "is_bold": False}
            for value in values
        ],
    }


def _complete_context() -> dict:
    return {
        "schema_version": "price-rule-proposal-v1",
        "section_id": "sec_synthetic",
        "loader_scope": "tabular",
        "sheet_index": 0,
        "sheet_name": "Synthetic supplier sheet",
        "sheet_shape": {"rows": 3, "columns": 4},
        "header_row_index": 0,
        "section_start_row_index": 1,
        "section_end_row_index": 3,
        "section_kind": "price_rows",
        "header_values": ["Наименование", "Ед.", "Оптовая цена", "Розничная цена"],
        "header_rows": [_row(0, ["Наименование", "Ед.", "Оптовая цена", "Розничная цена"])],
        "column_header_rows": [_row(0, ["Наименование", "Ед.", "Оптовая цена", "Розничная цена"])],
        "category_rows": [],
        "sample_rows": [_row(1, ["Товар А", "шт", 100, 120]), _row(2, ["Товар Б", "шт", 200, 240])],
        "price_presence": {
            "all_source_columns_inspected": True,
            "inspected_column_indexes": [0, 1, 2, 3],
            "price_column_indexes": [2, 3],
            "candidate_row_indexes": [1, 2],
            "value_counts_by_column": [
                {"index": 0, "nonempty_rows": 2},
                {"index": 1, "nonempty_rows": 2},
                {"index": 2, "nonempty_rows": 2},
                {"index": 3, "nonempty_rows": 2},
            ],
            "candidate_name_rows": 2,
            "rows_with_price": 2,
            "rows_without_price": 0,
        },
        "deterministic_observations": [
            {"index": 0, "header": "Наименование", "suggested_role": "name", "confidence": 0.9, "price_descriptor": None},
            {"index": 1, "header": "Ед.", "suggested_role": "unit", "confidence": 0.9, "price_descriptor": None},
            {"index": 2, "header": "Оптовая цена", "suggested_role": "price", "confidence": 0.9, "price_descriptor": {"kind": "wholesale", "vat_mode": "unknown"}},
            {"index": 3, "header": "Розничная цена", "suggested_role": "price", "confidence": 0.9, "price_descriptor": {"kind": "retail", "vat_mode": "unknown"}},
        ],
        "horizontal_panels": [],
    }


def test_complete_section_is_deterministic_candidate() -> None:
    result = _module().evaluate_section_llm_need(_complete_context(), loader="xlsx")

    assert result["decision"] == "deterministic_candidate"
    assert result["has_name"] is True
    assert result["has_price"] is True
    assert result["unresolved_material_columns"] == []
    assert result["reasons"] == []


def test_missing_price_requires_llm() -> None:
    context = _complete_context()
    context["deterministic_observations"] = context["deterministic_observations"][:2]
    context["price_presence"]["price_column_indexes"] = []

    result = _module().evaluate_section_llm_need(context, loader="xlsx")

    assert result["decision"] == "llm_required"
    assert "missing_price_role" in result["reasons"]


def test_catalog_fragment_requires_semantic_decision_even_with_price_headers() -> None:
    context = _complete_context()
    context["section_kind"] = "name_rows_without_price"

    result = _module().evaluate_section_llm_need(context, loader="xlsx")

    assert result["decision"] == "llm_required"
    assert "catalog_semantic_decision_required" in result["reasons"]
    assert "catalog_vs_sparse_price" in result["structural_ambiguities"]


def test_unresolved_second_money_column_blocks_candidate() -> None:
    context = _complete_context()
    context["deterministic_observations"][2]["confidence"] = 0.4
    context["deterministic_observations"][2]["price_descriptor"] = None

    result = _module().evaluate_section_llm_need(context, loader="xlsx")

    assert result["decision"] == "llm_required"
    assert result["unresolved_material_columns"] == [2]
    assert "unresolved_material_column" in result["reasons"]


def test_unresolved_matrix_axis_requires_llm() -> None:
    context = {
        "schema_version": "matrix-unfold-v2",
        "proposal_contract": "matrix-unfold-v1",
        "section_id": "matrix_synthetic",
        "loader_scope": "matrix",
        "sheet_name": "Matrix",
        "blocks": [{"block_ref": "b1", "kind": "two_axis_matrix"}],
    }

    result = _module().evaluate_section_llm_need(context, loader="xlsx")

    assert result["decision"] == "llm_required"
    assert "ambiguous_matrix_axis" in result["reasons"]


def test_signature_ignores_values_supplier_file_sheet_and_source_rows() -> None:
    first = _complete_context()
    first.update({"supplier_id": "supplier-a", "filename": "a.xlsx"})
    second = deepcopy(first)
    second.update(
        {
            "supplier_id": "supplier-b",
            "filename": "b.xlsx",
            "sheet_name": "Another supplier tab",
            "sheet_index": 9,
            "section_id": "sec_other_uuid_like_value",
            "header_row_index": 40,
            "section_start_row_index": 41,
            "section_end_row_index": 43,
            "sample_rows": [_row(200, ["Совсем иной товар", "шт", 9999, 12000])],
        }
    )

    signature = _module().structural_signature
    assert signature(first, loader="xlsx") == signature(second, loader="xlsx")


@pytest.mark.parametrize("change", ["role", "header", "panel"])
def test_signature_changes_with_material_structure(change: str) -> None:
    first = _complete_context()
    second = deepcopy(first)
    if change == "role":
        second["deterministic_observations"][1]["suggested_role"] = "article"
    elif change == "header":
        second["header_values"][1] = "Код поставщика"
        second["deterministic_observations"][1]["header"] = "Код поставщика"
    else:
        second["horizontal_panels"] = [
            {"start_column_index": 0, "end_column_index": 4, "name_columns": [0], "price_columns": [2, 3]}
        ]

    signature = _module().structural_signature
    assert signature(first, loader="xlsx") != signature(second, loader="xlsx")


def test_prompt_metrics_are_positive_stable_and_explicitly_estimated() -> None:
    prompt = "Проанализируй только эту секцию: {\"товар\": \"Клапан\"}"

    first = _module().measure_prompt(prompt, context=_complete_context())
    second = _module().measure_prompt(prompt, context=_complete_context())

    assert first == second
    assert first["prompt_chars"] == len(prompt)
    assert first["prompt_bytes"] == len(prompt.encode("utf-8"))
    assert first["estimated_input_tokens"] > 0
    assert first["token_estimation_method"] == "utf8_bytes_div_4_ceiling"
    assert first["context_rows"] > 0
    assert first["context_cells"] > 0


def test_batch_metrics_expose_repeated_signatures_and_cost() -> None:
    diagnostics = _module()
    contexts = [_complete_context(), deepcopy(_complete_context()), deepcopy(_complete_context())]
    contexts[2]["deterministic_observations"][2]["confidence"] = 0.4
    rows = [
        diagnostics.build_section_diagnostics(context, loader="xlsx", prompt=f"prompt {index}")
        for index, context in enumerate(contexts)
    ]

    result = diagnostics.summarize_batch_diagnostics(rows)

    assert result["sections_total"] == 3
    assert result["sections_deterministic_candidates"] == 2
    assert result["sections_llm_required"] == 1
    assert result["unique_structural_signatures"] == 1
    assert result["repeated_signature_groups"] == 1
    assert result["max_signature_group_size"] == 3
    assert result["prompt_chars_total"] == sum(len(f"prompt {index}") for index in range(3))
    assert result["estimated_input_tokens_total"] > 0


def test_deterministic_candidate_is_still_emitted_as_llm_attempt_spec() -> None:
    grid = SheetGrid(
        name="Synthetic",
        rows=[
            [Cell("Наименование"), Cell("Ед."), Cell("Оптовая цена"), Cell("Розничная цена")],
            [Cell("Товар А"), Cell("шт"), Cell("100"), Cell("120")],
            [Cell("Товар Б"), Cell("шт"), Cell("200"), Cell("240")],
        ],
    )

    context = build_rule_section_contexts([grid])[0]
    expected_prompt = build_tabular_rule_generation_prompt(
        source_context=context,
        loader="xlsx",
        model="mock-model",
    )
    specs = build_rule_section_specs(
        grids=[grid], loader="xlsx", model="mock-model"
    )

    assert len(specs) == 1
    assert specs[0]["diagnostics"]["llm_need"]["decision"] == "deterministic_candidate"
    assert specs[0]["input_context"] == context
    assert specs[0]["prompt"] == expected_prompt
    assert "section_diagnostics" not in specs[0]["prompt"]
