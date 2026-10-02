"""Ярусы цены по количеству и имя под объединённой шапкой.

Оба факта проверяются на реальном прайсе: обе ошибки были найдены замером на
корпусе, а не чтением кода, и синтетическая книга их не воспроизводит.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.parsing.pipeline import parse_workbook
from app.parsing.quantity_tiers import is_quantity_tier, quantity_bounds

TEHNO = (
    Path(__file__).parents[4]
    / "original"
    / "datasets"
    / "прайсы"
    / "ПРАЙС_ЛИСТ_дилер_розница_Supplier E_12_01_2026_г_.xlsx"
)


@pytest.fixture(scope="module")
def tehno_items():
    if not TEHNO.exists():
        pytest.skip("корпусный прайс недоступен")
    return parse_workbook(TEHNO.read_bytes(), TEHNO.name).items


@pytest.mark.parametrize(
    "text, expected",
    [
        ("1-30", (1, 30)),
        ("31-60", (31, 60)),
        (">100", (101, None)),
        ("≥100", (100, None)),
        ("100+", (100, None)),
        ("<10", (None, 9)),
        ("шт.", (None, None)),
        ("НЗК/Б", (None, None)),
        ("", (None, None)),
    ],
)
def test_quantity_bounds(text, expected) -> None:
    assert quantity_bounds(text) == expected


def test_a_unit_is_not_a_quantity_tier() -> None:
    """«шт.» — единица измерения, «1-30» — порог партии. Их путать нельзя."""
    assert is_quantity_tier("1-30")
    assert not is_quantity_tier("шт.")
    assert not is_quantity_tier("НЗК/Б")


def test_quantity_tiers_become_price_levels_of_one_position(tehno_items) -> None:
    """Четыре строки с порогами партии — одна позиция с четырьмя уровнями цены.

    Иначе в каталоге появляются четыре неразличимых «Допинг 2.02т» с разной
    ценой, и сопоставление выбирает из них наугад.
    """
    dopping = next(it for it in tehno_items if it.source_name.startswith("Допинг 2.02т"))
    retail = [v for v in dopping.values if (v.source_label or "").startswith("Цена розничная")]
    assert [(v.min_quantity, v.max_quantity) for v in retail] == [
        (Decimal(1), Decimal(30)),
        (Decimal(31), Decimal(60)),
        (Decimal(61), Decimal(100)),
        (Decimal(101), None),
    ]

    # Дилерская цена объединена на весь блок: от партии она не зависит.
    dealer = [v for v in dopping.values if (v.source_label or "").startswith("Цена для дилеров")]
    assert dealer and all(v.min_quantity is None and v.max_quantity is None for v in dealer)

    # Строки ярусов не остаются отдельными позициями.
    assert not [it for it in tehno_items if it.source_row in {9, 10, 11}]


def test_a_merged_heading_does_not_make_the_row_number_the_product_name(tehno_items) -> None:
    """Шапка «Продукция» объединена на номер строки и на название.

    Роль имени доставалась мастеру объединения — номеру, — и в каталог уходили
    позиции с именами «1», «2», «3».
    """
    numeric = [it for it in tehno_items if it.source_name.strip().isdigit()]
    assert not numeric, [it.source_name for it in numeric[:5]]
    assert any("Допинг" in it.source_name for it in tehno_items)


def test_ladders_do_not_mix_price_kinds(tehno_items) -> None:
    """У каждого вида цены своя лестница ступеней.

    Позиция несёт дилерскую цену без ступеней и розничную со ступенями. Общий
    перебор всегда выбирал бы розничную — у неё нижняя граница выше, — и
    дилерскую цену запрос не вернул бы ни при каком количестве.
    """
    from app.pricing import select_price_for_quantity, select_prices_for_quantity

    dopping = next(it for it in tehno_items if it.source_name.startswith("Допинг 2.02т"))

    dealer = select_price_for_quantity(dopping.values, 200, price_kind="dealer")
    assert dealer is not None and dealer.amount == Decimal("1900.00")

    by_kind = select_prices_for_quantity(dopping.values, 200)
    assert {key[0] for key in by_kind} == {"dealer", "retail"}
    assert by_kind[("retail", "excluded")].amount == Decimal("2375.00")
    assert by_kind[("dealer", "excluded")].amount == Decimal("1900.00")


def test_vat_mode_comes_from_the_second_header_tier(tehno_items) -> None:
    """«Цена для дилеров» / «Без учета НДС» — вид цены сверху, НДС снизу.

    Разбор читает шапку одной строкой, поэтому цена с НДС и без НДС были
    неразличимы: обе `unknown`.
    """
    dopping = next(it for it in tehno_items if it.source_name.startswith("Допинг 2.02т"))
    assert {(v.kind, v.vat_mode) for v in dopping.values} == {
        ("dealer", "excluded"),
        ("dealer", "included"),
        ("retail", "excluded"),
        ("retail", "included"),
    }


BEREG = (
    Path(__file__).parents[4]
    / "original"
    / "datasets"
    / "прайсы"
    / "Supplier C ПРАЙС 30.06.2026 (1).xlsx"
)


def test_vat_falls_back_to_the_sheet_statement() -> None:
    """«Цены с НДС» строкой над таблицей относится ко всем её колонкам.

    По замеру базы у 78 % цен режим НДС не определён именно потому, что
    поставщик сказал про него один раз над таблицей, а не в подписи каждой
    колонки.
    """
    if not BEREG.exists():
        pytest.skip("корпусный прайс недоступен")
    items = parse_workbook(BEREG.read_bytes(), BEREG.name).items
    modes = {v.vat_mode for it in items for v in it.values}
    assert "included" in modes, modes


def test_a_contradictory_sheet_statement_is_not_guessed() -> None:
    """Если над таблицей сказано и «с НДС», и «без НДС», угадывать нельзя."""
    from app.parsing.grid import Cell, SheetGrid
    from app.parsing.headers import HeaderDetection, ColumnSuggestion, apply_sheet_vat_statement

    def row(*values):
        return [Cell(value=v) for v in values]

    grid = SheetGrid(
        name="лист",
        rows=[row("Цены с НДС"), row("Цены без НДС"), row("Наименование", "Цена"), row("Товар", 10)],
    )
    detection = HeaderDetection(
        header_row_index=2,
        columns=[
            ColumnSuggestion(index=0, header="Наименование", role="name", confidence=0.9),
            ColumnSuggestion(
                index=1, header="Цена", role="price", confidence=0.9,
                price_descriptor={"kind": "other", "vat_mode": "unknown"},
            ),
        ],
    )
    apply_sheet_vat_statement(grid, detection)
    assert detection.columns[1].price_descriptor["vat_mode"] == "unknown"
