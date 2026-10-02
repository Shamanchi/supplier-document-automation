"""Regression coverage for the Rusarsenal multi-block XLS price layout."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.category_service import looks_like_article_only
from app.parsing.loaders import detect_loader, load_grids_for
from app.parsing.grid import SheetGrid
from app.parsing.rules import validation_metrics
from app.parsing.workbook_layout import (
    parse_workbook_layout,
    recipe_matches_workbook_layout,
)
from app.review_validation import parse_review_draft


SOURCE = (
    Path(__file__).parents[4]
    / "original"
    / "datasets"
    / "прайсы"
    / "ПРАЙС Supplier B (3) (3).xls"
)

PRICE_NEW_SOURCE = (
    Path(__file__).parents[4]
    / "original"
    / "datasets"
    / "прайсы"
    / "price_list_sample.xlsx"
)


def _item(result, name_part: str):
    return next(item for item in result.items if name_part in item.source_name)


def _source_item(result, sheet: str, row: int, panel: int = 0):
    return next(
        item
        for item in result.items
        if item.source_sheet == sheet
        and item.source_row == row
        and item.source_panel_index == panel
    )


def test_generated_matrix_rule_matches_its_source_sheet_before_axis_validation() -> None:
    recipe = {
        "format": "workbook_layout_v1",
        "profile": "generated_matrix_v1",
        "sheets": [],
        "matrix_regions": [{
            "format": "matrix-unfold-v1",
            "match": {"name_pattern": "Matrix", "or_index": 0},
            "blocks": [{}],
        }],
    }

    assert recipe_matches_workbook_layout([SheetGrid(name="Matrix")], recipe) is True
    assert recipe_matches_workbook_layout([SheetGrid(name="Other")], recipe) is False


def _seeded_recipe(loader: str) -> dict | None:
    """The shipped workbook profile, matched the way the service matches it.

    The profile used to be a constant in ``workbook_layout.py``; it now lives in
    ``seeds/parse_rules_builtin.json`` like every other rule, and selection goes
    through the same structural evidence check the DB lookup uses.
    """
    seed = json.loads(
        (Path(__file__).resolve().parents[1] / "seeds" / "parse_rules_builtin.json")
        .read_text("utf-8")
    )
    return [row["recipe"] for row in seed if row["loader"] == loader] or None


def test_seeded_workbook_layout_splits_parallel_price_blocks() -> None:
    data = SOURCE.read_bytes()
    loader = detect_loader(data, "renamed-upload.xls")
    grids = load_grids_for(loader, data, "renamed-upload.xls")

    candidates = _seeded_recipe(loader)
    assert candidates is not None
    recipe = next(
        (r for r in candidates if recipe_matches_workbook_layout(grids, r)), None
    )

    assert recipe is not None
    result = parse_workbook_layout(grids, recipe)
    assert not result.needs_mapping
    assert validation_metrics(result)["passed"] is True
    assert any(
        value.amount == Decimal("879.50")
        for value in _item(result, 'ОП-2 (з)  АВСЕ  "Премиум" Красный').values
    )
    assert any(
        value.amount == Decimal("288.10")
        for value in _item(result, "ОП-1 (з)  ВСЕ   ЗПУ РФ").values
    )
    assert any(
        value.amount == Decimal("4921.00")
        for value in _item(result, "Пожарная подставка фланцевая ППФД").values
    )
    assert any(
        value.amount == Decimal("13.50")
        for value in _item(result, "Знаки самоклеящиеся пленка  (50х100)").values
    )

    # Article-only positions must inherit the meaningful worksheet title as a
    # fallback category; otherwise the category-context gate blocks them with
    # ARTICLE_ONLY_CATEGORY_MISSING even though the sheet itself is the section.
    assert not [
        item
        for item in result.items
        if looks_like_article_only(item.source_name) and item.category_path is None
    ]
    assert _item(result, "КН-50").category_path == "Запчасти,Заряды"
    assert _item(result, "ГПС-100").category_path == "Снаряж.Водопен"
    assert _item(result, "РС-25").category_path == "Стволы,Головки"

    # Raw source evidence is captured so the UI can show what the parser read.
    evidence = _item(result, "КН-50")
    assert evidence.source_header_cells
    header_text = " ".join(str(value) for _index, value in evidence.source_header_cells)
    assert "Наименование" in header_text
    assert evidence.source_data_cells
    data_text = " ".join(str(value) for _index, value in evidence.source_data_cells)
    assert "КН-50" in data_text
    assert "Запчасти,Заряды" in data_text


def test_price_new_workbook_is_not_matched_by_a_seeded_supplier_recipe() -> None:
    data = PRICE_NEW_SOURCE.read_bytes()
    loader = detect_loader(data, "renamed-upload.xlsx")
    grids = load_grids_for(loader, data, "renamed-upload.xlsx")

    for recipe in _seeded_recipe("xls") or []:
        assert recipe_matches_workbook_layout(grids, recipe) is False


def test_review_draft_requires_an_explicit_rule_for_matrix_workbook() -> None:
    with pytest.raises(HTTPException) as exc_info:
        parse_review_draft(
            data=PRICE_NEW_SOURCE.read_bytes(),
            filename="renamed-upload.xlsx",
            mapping=None,
            recipe=None,
        )

    assert exc_info.value.status_code == 409
