"""Regression tests for deterministic text-layout PDF recipes.

The source PDFs are native text documents from the mandatory corpus.  These
tests deliberately use a renamed in-memory byte stream: a layout profile must
recognise stable document evidence, never a supplier filename.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.category_service import looks_like_article_only
from app.parsing.pdf_layout import (
    find_builtin_pdf_layout_recipe,
    parse_pdf_layout,
)
from app.parsing.rules import validation_metrics


CORPUS = (
    Path(__file__).parents[4]
    / "original"
    / "datasets"
    / "прайсы"
)


def _item(result, name_part: str):
    return next(item for item in result.items if name_part in item.source_name)


@pytest.mark.parametrize(
    ("filename", "expected_rows", "name_part", "amounts"),
    [
        ("КП опт (3).pdf", 21, "ОП1(з) АВСЕ", {Decimal("266.90")}),
        ("прайс опт 20.01.26.pdf", 41, "ЯП 0,1", {Decimal("1700.00")}),
        (
            "0) Прайс-лист_ Россия_с 01-01-2026.pdf",
            0,  # exact total is asserted by the profile-specific test below
            "МППВК-17-ГЗ",
            {Decimal("45070.00"), Decimal("41080.00")},
        ),
    ],
)
def test_builtin_layout_recipe_parses_native_text_pdf_without_filename_match(
    filename: str,
    expected_rows: int,
    name_part: str,
    amounts: set[Decimal],
) -> None:
    data = (CORPUS / filename).read_bytes()

    # A profile is selected from stable text/layout evidence, not a filename.
    recipe = find_builtin_pdf_layout_recipe(data, "renamed-upload.pdf")

    assert recipe is not None
    result = parse_pdf_layout(data, recipe)
    assert not result.needs_mapping
    assert validation_metrics(result)["passed"] is True
    if expected_rows:
        assert result.rows_accepted == expected_rows
    assert {value.amount for value in _item(result, name_part).values} == amounts


def test_layout_recipe_keeps_wrapped_names_and_on_request_prices() -> None:
    data = (CORPUS / "прайс опт 20.01.26.pdf").read_bytes()
    recipe = find_builtin_pdf_layout_recipe(data, "upload.pdf")

    result = parse_pdf_layout(data, recipe)

    wrapped = _item(result, "Стенд (бункер) с переворачивающимся ящиком")
    assert [value.amount for value in wrapped.values] == [Decimal("6900.00")]
    on_request = [
        value
        for item in result.items
        if "Контейнер 750л" in item.source_name
        for value in item.values
    ]
    assert len(on_request) == 2
    assert all(value.is_on_request and value.amount is None for value in on_request)


def test_layout_recipe_skips_tungus_category_rows() -> None:
    data = (CORPUS / "0) Прайс-лист_ Россия_с 01-01-2026.pdf").read_bytes()
    recipe = find_builtin_pdf_layout_recipe(data, "upload.pdf")

    result = parse_pdf_layout(data, recipe)

    assert result.rows_accepted == 154
    assert sum(len(item.values) for item in result.items) == 307
    assert not any(
        item.source_name.startswith("НОВИНКА")
        or item.source_name.startswith("МОДУЛИ ПОЖАРОТУШЕНИЯ")
        for item in result.items
    )
    on_request = _item(result, "ИСТО-1")
    assert on_request.source_name.startswith("ИСТО-1")
    assert [value.amount for value in on_request.values] == [None]
    assert all(value.is_on_request for value in on_request.values)

    # Numbered no-price captions are section headers: they must not become
    # items, but they must give article-only positions a category context so
    # the category gate does not block them with ARTICLE_ONLY_CATEGORY_MISSING.
    assert not [
        item
        for item in result.items
        if looks_like_article_only(item.source_name) and item.category_path is None
    ]
    assert _item(result, "МПП(Н-Взр)-2(п)-И-ГЭ-У2").category_path.startswith(
        'МПП "ТУНГУС" взрывозащищенного исполнения'
    )
    assert _item(result, "МПП(Н-Т)-2(п)-И-ГЭ-У2").category_path.startswith(
        'МПП "ТУНГУС" термостойкого исполнения'
    )

    # Raw source evidence is captured so the UI can show what the parser read.
    evidence_item = _item(result, "МПП(Н-Взр)-2(п)-И-ГЭ-У2")
    assert evidence_item.source_header_cells
    header_text = " ".join(str(value) for _index, value in evidence_item.source_header_cells)
    assert "до 50 шт." in header_text
    assert "свыше 50 шт." in header_text
    assert evidence_item.source_data_cells
    data_text = " ".join(str(value) for _index, value in evidence_item.source_data_cells)
    assert evidence_item.source_name in data_text
    assert evidence_item.source_category_cells

    # The two price columns are quantity tiers, not "Цена 1/Цена 2": the group
    # header "Отпускная цена, руб. (включая НДС)" supplies currency and VAT,
    # while each column label becomes an inclusive quantity bound.
    tiered = _item(result, "МППВК-17-ГЗ")
    by_label = {value.source_label: value for value in tiered.values}
    assert set(by_label) == {"до 50 шт.", "свыше 50 шт."}
    low = by_label["до 50 шт."]
    high = by_label["свыше 50 шт."]
    assert low.amount == Decimal("45070")
    assert low.min_quantity is None and low.max_quantity == Decimal("50")
    assert high.amount == Decimal("41080")
    assert high.min_quantity == Decimal("51") and high.max_quantity is None
    for value in tiered.values:
        assert value.currency == "RUB"
        assert value.currency_source == "column"
        assert value.vat_mode == "included"
