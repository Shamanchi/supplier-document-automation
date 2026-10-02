"""P0 regressions for deterministic header boundaries and header bands."""

from __future__ import annotations

from app.parsing.grid import Cell, SheetGrid
from app.parsing.mapping import build_auto_mapping
from app.parsing.normalize import currency_from_number_format
from app.parsing.pipeline import parse_grids


def _row(*values: object | None) -> list[Cell]:
    return [Cell(value=value) for value in values]


def _price_errors(result) -> list:
    return [issue for issue in result.issues if issue.code == "PRICE_PARSE_ERROR"]


def test_headerless_without_preamble_starts_at_first_product() -> None:
    grid = SheetGrid(
        name="Simple",
        rows=[
            _row("Товар А", "шт", "100"),
            _row("Товар Б", "шт", "200"),
        ],
    )

    decision = build_auto_mapping([grid])
    result = parse_grids([grid], decision)

    assert decision.tables[0].start_row_index == 0
    assert [item.source_name for item in result.items] == ["Товар А", "Товар Б"]
    assert [value.amount for item in result.items for value in item.values] == [100, 200]
    assert not _price_errors(result)


def test_headerless_supplier_preamble_is_outside_product_body() -> None:
    grid = SheetGrid(
        name="With preamble",
        rows=[
            _row("Поставщик противопожарного оборудования", "+7 000 000-00-00", "город, улица, дом"),
            _row("Перечень оборудования", None, None),
            _row("Действует с текущей даты", None, None),
            _row("Наименование продукции", "Изм.", "Расч."),
            _row("Раздел оборудования", None, None),
            _row("Товар А", "шт", "4805"),
            _row("Товар Б", "шт", "5205"),
            _row("Товар В", "шт", "6100"),
        ],
    )

    decision = build_auto_mapping([grid])
    result = parse_grids([grid], decision)

    assert decision.tables[0].header_row_index is None
    assert decision.tables[0].start_row_index == 5
    assert len(result.items) == 3
    assert not _price_errors(result)


def test_invalid_price_inside_proven_product_body_remains_error() -> None:
    grid = SheetGrid(
        name="Invalid body price",
        rows=[
            _row("Товар А", "шт", "100"),
            _row("Товар Б", "шт", "200"),
            _row("Товар В", "шт", "не цена"),
            _row("Товар Г", "шт", "400"),
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert len(_price_errors(result)) == 1
    assert _price_errors(result)[0].source_value == "не цена"


def _multiline_price_grid(*, with_money: bool = True) -> SheetGrid:
    return SheetGrid(
        name="Five columns",
        rows=[
            _row("Наименование", "Тип исполнения", "Вид", "Размеры мм", "Индивидуальный"),
            _row(None, None, None, None, "руб. с НДС"),
            _row("Раздел продукции", None, None, None, None),
            _row("Подраздел", None, None, None, None),
            _row("Товар 1", "навесной", "закрытый", "500x600x200", "1000" if with_money else "уточняется"),
            _row("Товар 2", "встроенный", "открытый", "600x700x250", "2000" if with_money else "не применяется"),
        ],
    )


def test_multiline_price_header_combines_currency_subheader() -> None:
    grid = SheetGrid(
        name="Header band",
        rows=[
            _row("Наименование", "Индивидуальный"),
            _row(None, "руб. с НДС"),
            _row("Товар 1", "1000"),
            _row("Товар 2", "2000"),
        ],
    )

    decision = build_auto_mapping([grid])
    result = parse_grids([grid], decision)

    table = decision.tables[0]
    assert table.header_row_index == 1
    assert [(column.index, column.role) for column in table.columns] == [
        (0, "name"),
        (1, "price"),
    ]
    assert [value.amount for item in result.items for value in item.values] == [1000, 2000]


def test_multiline_five_column_header_maps_only_last_column_as_price() -> None:
    grid = _multiline_price_grid()

    decision = build_auto_mapping([grid])
    result = parse_grids([grid], decision)

    assert grid.name not in decision.non_price_sheets
    table = decision.tables[0]
    roles = {column.index: column.role for column in table.columns}
    assert roles[0] == "name"
    assert roles[4] == "price"
    assert all(roles[index] != "price" for index in (1, 2, 3))
    assert len(result.items) == 2
    assert [value.source_column_index for item in result.items for value in item.values] == [4, 4]
    assert [value.amount for item in result.items for value in item.values] == [1000, 2000]
    assert not any(issue.code == "NON_PRICE_SHEET" for issue in result.issues)


def test_multiline_money_words_without_money_data_stays_non_price() -> None:
    grid = _multiline_price_grid(with_money=False)

    decision = build_auto_mapping([grid])
    result = parse_grids([grid], decision)

    assert not decision.tables
    assert grid.name in decision.non_price_sheets
    assert any(issue.code == "NON_PRICE_SHEET" for issue in result.issues)


def test_locale_qualified_ruble_number_format_is_not_usd() -> None:
    assert currency_from_number_format(r"#,##0\ [$р.-419];\-#,##0\ [$р.-419]") == "RUB"
