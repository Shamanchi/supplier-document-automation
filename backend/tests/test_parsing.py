"""Parser pipeline tests against real corpus files (no DB / MinIO needed)."""
from decimal import Decimal
from pathlib import Path

import pytest

from app.parsing.headers import (
    count_header_sections,
    detect_header_candidates,
    normalize_header,
    suggest_role,
)
from app.parsing.grid import Cell, SheetGrid
from app.parsing.mapping import (
    ColumnMapping,
    MappingDecision,
    TableMapping,
    build_auto_mapping,
    stored_mapping_to_tables,
)
from app.parsing.normalize import (
    MAX_INDEXED_NORMALIZED_TEXT_CHARS,
    is_price_evidence,
    normalize_article,
    normalize_name,
    normalize_unit,
    parse_price,
    parse_quantity_tier,
)
from app.parsing.pipeline import parse_grids, parse_workbook

CORPUS_ROOT = Path("/home/nstepankov/Projects/supplier-document-automation/Nikolay_Stepankov/external_price_corpus")
CORPUS = CORPUS_ROOT / "xlsx"
SSD_FILE = CORPUS / "103_ССД_противопожарное_оборудование.xlsx"
POSEIDON_FILE = CORPUS / "015_Poseidon.xlsx"
DOC_FILE = CORPUS_ROOT / "doc" / "002_PKF_Техсервис.doc"
DOCX_FILE = CORPUS_ROOT / "docx" / "001_Centrfire_Челябинск.docx"
PDF_FILE = CORPUS_ROOT / "pdf" / "001_01161_Ростов.pdf"
ZEL_PDF_FILE = CORPUS_ROOT / "pdf" / "002_01ЗЕЛ.pdf"
PB01_XLS_FILE = CORPUS_ROOT / "xls" / "011_PB-01_Москва.xls"
CONSOL_XLS_FILE = CORPUS_ROOT / "xls" / "001_Consol.xls"

requires_corpus = pytest.mark.skipif(
    not CORPUS_ROOT.is_dir(), reason="external price corpus is not available on this machine"
)


# --- unit-level checks -------------------------------------------------------

def test_normalize_header():
    assert normalize_header("Ед. изм.") == "ед изм"
    assert normalize_header("Розница с НДС ") == "розница с ндс"
    assert normalize_header("Оптовая скидка, %") == "оптовая скидка"


def test_suggest_roles():
    assert suggest_role("Наименование").role == "name"
    assert suggest_role("Код").role == "article"
    s = suggest_role("Розница с НДС")
    assert s.role == "price"
    assert s.price_descriptor == {"kind": "retail", "vat_mode": "included"}
    s = suggest_role("Оптовая цена с НДС")
    assert s.price_descriptor == {"kind": "wholesale", "vat_mode": "included"}
    s = suggest_role("Розничная цена ()")
    assert s.role == "price"
    assert s.price_descriptor["kind"] == "retail"
    assert suggest_role("Оптовое кол-во").role == "quantity_threshold"
    assert suggest_role("№").role == "row_number"
    assert suggest_role("Цена без НДС").price_descriptor["vat_mode"] == "excluded"
    assert suggest_role("Дилер").price_descriptor == {
        "kind": "dealer",
        "vat_mode": "unknown",
    }
    assert suggest_role("Номенклатура / код").role == "article"
    assert suggest_role("Изм.").role == "unit"
    assert suggest_role("Категория").role == "category"
    assert suggest_role("Краткая характеристика").role == "description"


def test_compound_dimensions_header_is_not_description():
    assert suggest_role("габариты мм характеристики дхшхв").role == "dimensions"
    assert suggest_role("Габариты, мм").role == "dimensions"
    assert suggest_role("Характеристики ДхШхВ").role == "dimensions"
    assert suggest_role("Технические характеристики").role == "description"


def test_factory_price_header_is_not_misclassified_as_manufacturer():
    suggested = suggest_role("Новая заводская цена 12.01.2026")

    assert suggested.role == "price"
    assert suggested.price_descriptor == {"kind": "other", "vat_mode": "unknown"}

    wholesale = suggest_role("Цена завода опт")
    assert wholesale.role == "price"
    assert wholesale.price_descriptor["kind"] == "wholesale"


def test_price_notice_does_not_become_category_context() -> None:
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [Cell(value="Наименование"), Cell(value="Цена")],
            [Cell(value="Раздел"), Cell(value=None)],
            [Cell(value="Цены партнёров можно посмотреть на сайте"), Cell(value=None)],
            [Cell(value="Товар"), Cell(value="100")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert len(result.items) == 1
    assert result.items[0].category_path == "Раздел"


def test_data_row_with_two_product_names_is_not_a_second_header():
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [
                Cell(value="Наименование"),
                Cell(value="Цена"),
                Cell(value="Наименование"),
                Cell(value="Цена"),
            ],
            [Cell(value="Товар А"), Cell(value="100"), Cell(value="Товар Б"), Cell(value="200")],
        ],
    )

    assert [detection.header_row_index for detection in detect_header_candidates(grid)] == [0]


def test_merged_header_fills_do_not_create_fake_price_or_name_columns():
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [
                Cell(value="Наименование"),
                Cell(value="Наименование", is_merged_fill=True),
                Cell(value="Цена"),
                Cell(value="Цена", is_merged_fill=True),
                Cell(value="Наименование"),
                Cell(value="Наименование", is_merged_fill=True),
                Cell(value="Цена"),
                Cell(value="Цена", is_merged_fill=True),
            ],
            [
                Cell(value="Товар А"),
                Cell(value="Товар А", is_merged_fill=True),
                Cell(value="100"),
                Cell(value="100", is_merged_fill=True),
                Cell(value="Товар Б"),
                Cell(value="Товар Б", is_merged_fill=True),
                Cell(value="200"),
                Cell(value="200", is_merged_fill=True),
            ],
        ],
    )

    decision = build_auto_mapping([grid])
    result = parse_grids([grid], decision)

    assert [(column.index, column.role) for column in decision.tables[0].columns if column.role != "ignore"] == [
        (0, "name"),
        (2, "price"),
        (4, "name"),
        (6, "price"),
    ]
    assert [(item.source_name, item.values[0].amount) for item in result.items] == [
        ("Товар А", Decimal("100.00")),
        ("Товар Б", Decimal("200.00")),
    ]


def _merged_caption(text: str, width: int) -> list[Cell]:
    return [Cell(value=text, is_bold=True, fill_type="solid")] + [
        Cell(value=text, is_merged_fill=True, is_bold=True, fill_type="solid")
        for _ in range(width - 1)
    ]


def _headerless_product_row(name: str, price: str, width: int = 8) -> list[Cell]:
    return (
        [Cell(value=name)]
        + [Cell(value=name, is_merged_fill=True) for _ in range(width - 3)]
        + [Cell(value="шт"), Cell(value=price, is_bold=True)]
    )


def test_headerless_price_sheet_is_detected_without_header_labels() -> None:
    grid = SheetGrid(
        name="Услуги",
        rows=[
            _merged_caption("Обслуживание огнетушителей", 8),
            _headerless_product_row("Станция зарядная порошковая", "453718"),
            _headerless_product_row("Установка для гидроиспытаний баллонов", "112371"),
            _headerless_product_row("Стенд для осушки баллонов", "246293"),
            _merged_caption("Полиграфическая продукция", 8),
            _headerless_product_row("Плакат инструкция по применению", "141.2"),
            _headerless_product_row("Журналы", "по запросу"),
        ],
    )

    decision = build_auto_mapping([grid])

    assert decision.auto_ok is True
    assert decision.non_price_sheets == []
    assert len(decision.tables) == 1
    table = decision.tables[0]
    assert table.header_row_index is None
    assert [
        (column.index, column.role) for column in table.columns if column.role != "ignore"
    ] == [(0, "name"), (6, "unit"), (7, "price")]

    result = parse_grids([grid], decision)
    assert [item.source_name for item in result.items] == [
        "Станция зарядная порошковая",
        "Установка для гидроиспытаний баллонов",
        "Стенд для осушки баллонов",
        "Плакат инструкция по применению",
        "Журналы",
    ]
    assert result.items[0].category_path == "Обслуживание огнетушителей"
    assert result.items[0].values[0].amount == Decimal("453718.00")
    assert result.items[3].category_path == "Полиграфическая продукция"
    assert result.items[4].values[0].qualifier == "on_request"


def test_headerless_detection_ignores_sheets_without_prices() -> None:
    grid = SheetGrid(
        name="Контакты",
        rows=[
            [Cell(value="Иван Петров"), Cell(value="+7 912 345-67-89"), Cell(value="ivan@example.com")],
            [Cell(value="Мария Сидорова"), Cell(value="+7 921 765-43-21"), Cell(value="maria@example.com")],
            [Cell(value="Пётр Иванов"), Cell(value="+7 905 111-22-33"), Cell(value="petr@example.com")],
            [Cell(value="Анна Кузнецова"), Cell(value="+7 911 444-55-66"), Cell(value="anna@example.com")],
        ],
    )

    decision = build_auto_mapping([grid])

    assert decision.auto_ok is False
    assert decision.tables == []
    assert decision.non_price_sheets == ["Контакты"]


BRANDMEISTER_FILE = CORPUS_ROOT / "xls" / "021_Брандмейстер.xls"
ASEISTEM_FILE = CORPUS_ROOT / "xls" / "019_АСистем_Сегнетикс.xls"


@pytest.mark.skipif(
    not BRANDMEISTER_FILE.is_file(), reason="external price corpus is not available"
)
def test_headerless_detection_rejects_contents_page_with_page_numbers() -> None:
    """The «оглавление» sheet lists section titles with page numbers 1-6 in the
    price position; pagination must not become a price table."""
    from app.parsing import loaders

    data = BRANDMEISTER_FILE.read_bytes()
    grids = loaders.load_grids_for(loaders.detect_loader(data, BRANDMEISTER_FILE.name), data, BRANDMEISTER_FILE.name)

    decision = build_auto_mapping(grids)

    assert "оглавление" in decision.non_price_sheets
    assert not [table for table in decision.tables if table.sheet_name == "оглавление"]


@pytest.mark.skipif(
    not ASEISTEM_FILE.is_file(), reason="external price corpus is not available"
)
def test_headerless_detection_rejects_spec_sheet_with_zero_values() -> None:
    """FMR_new is a product configurator: characteristic counts (16/0/0/8...)
    where most values are zero are not published prices."""
    from app.parsing import loaders

    data = ASEISTEM_FILE.read_bytes()
    grids = loaders.load_grids_for(loaders.detect_loader(data, ASEISTEM_FILE.name), data, ASEISTEM_FILE.name)

    decision = build_auto_mapping(grids)

    for sheet_name in ("FMR_new", "MRL"):
        assert sheet_name in decision.non_price_sheets
        assert not [table for table in decision.tables if table.sheet_name == sheet_name]


def test_headerless_detection_uses_unrecognized_label_row_as_header() -> None:
    grid = SheetGrid(
        name="Смета",
        rows=[
            [Cell(value="Наименование"), Cell(value="Кол-во"), Cell(value="Сумма")],
            [Cell(value="Товар А"), Cell(value="2"), Cell(value="200")],
            [Cell(value="Товар Б"), Cell(value="1"), Cell(value="100")],
            [Cell(value="Товар В"), Cell(value="3"), Cell(value="300")],
        ],
    )

    decision = build_auto_mapping([grid])

    assert len(decision.tables) == 1
    table = decision.tables[0]
    assert table.header_row_index == 0
    assert [
        (column.index, column.role) for column in table.columns if column.role != "ignore"
    ] == [(0, "name"), (2, "price")]

    result = parse_grids([grid], decision)
    assert [(item.source_name, item.values[0].amount) for item in result.items] == [
        ("Товар А", Decimal("200.00")),
        ("Товар Б", Decimal("100.00")),
        ("Товар В", Decimal("300.00")),
    ]


PRICE_NEW_FILE = (
    Path(__file__).parents[4]
    / "original"
    / "datasets"
    / "прайсы"
    / "price_list_sample.xlsx"
)

requires_price_new = pytest.mark.skipif(
    not PRICE_NEW_FILE.is_file(), reason="price_new corpus file is not available"
)


@requires_price_new
def test_price_new_headerless_service_sheet_corpus_regression() -> None:
    """The «Обслуживание» sheet has no header row at all: sections open with a
    merged caption, then product rows repeat name(A:F merged) | unit(G) |
    price(H). Missed coordinates: source rows 5-47 (1-based), 40 positions."""
    from app.parsing import loaders
    from app.parsing.rules import recipe_from_decision, recipe_to_decision

    data = PRICE_NEW_FILE.read_bytes()
    loader = loaders.detect_loader(data, PRICE_NEW_FILE.name)
    grids = loaders.load_grids_for(loader, data, PRICE_NEW_FILE.name)

    decision = build_auto_mapping(grids)

    assert "Обслуживание" not in decision.non_price_sheets
    tables = [table for table in decision.tables if table.sheet_name == "Обслуживание"]
    assert len(tables) == 1
    table = tables[0]
    assert table.header_row_index is None
    assert [
        (column.index, column.role) for column in table.columns if column.role != "ignore"
    ] == [(0, "name"), (6, "unit"), (7, "price")]

    result = parse_grids(grids, decision)
    items = [item for item in result.items if item.source_sheet == "Обслуживание"]
    assert len(items) == 40
    by_row = {item.source_row: item for item in items}
    assert by_row[5].source_name.startswith("СЗП-02ГУ")
    assert by_row[5].values[0].amount == Decimal("453718.00")
    assert by_row[5].category_path == "Обслуживание / Обслуживание порошковых огнетушителей"
    assert by_row[47].source_name == "Журналы"
    assert by_row[47].values[0].qualifier == "on_request"

    recipe = recipe_from_decision(decision, grids)
    for sheet in recipe["sheets"]:
        assert sheet["default_category_source"] == "sheet_name"
        assert sheet["default_category_path"] is None
    headerless_sheets = [sheet for sheet in recipe["sheets"] if sheet.get("headerless")]
    assert [sheet["match"]["name_pattern"] for sheet in headerless_sheets] == ["Обслуживание"]
    replay = recipe_to_decision(recipe, grids)
    replayed = parse_grids(grids, replay)
    replayed_items = [item for item in replayed.items if item.source_sheet == "Обслуживание"]
    assert len(replayed_items) == 40


def test_suggest_role_recognizes_compact_dimensions_header():
    assert suggest_role("Шир*Выс*Гл").role == "dimensions"


def test_suggest_role_recognizes_material_thickness_as_technical_attribute():
    assert suggest_role("Толщина\nметалла").role == "attribute"


def test_suggest_role_recognizes_dimension_axes_header():
    assert suggest_role("Длина/глубина/высота, мм").role == "dimensions"


def test_parse_price_formats():
    assert parse_price("2 340,36").amount == Decimal("2340.36")
    assert parse_price("2\xa0340,36").amount == Decimal("2340.36")
    assert parse_price(201.3).amount == Decimal("201.30")
    assert parse_price(1500).amount == Decimal("1500.00")
    assert parse_price("1 234.50 руб.").amount == Decimal("1234.50")
    assert parse_price("по запросу").status == "on_request"
    assert parse_price("Договорная").status == "on_request"
    assert parse_price("уточняйте").status == "on_request"
    assert parse_price("Цена договорная").status == "on_request"
    assert parse_price("Смета").status == "on_request"
    assert parse_price("50 тенге за 1 км", allow_qualified=True).amount == Decimal("50.00")
    ranged = parse_price("130-300", allow_qualified=True)
    assert ranged.status == "ok"
    assert ranged.qualifier == "range"
    assert ranged.amount_from == Decimal("130.00")
    assert ranged.amount_to == Decimal("300.00")
    qualified = parse_price("50 тенге за 1 км", allow_qualified=True)
    assert qualified.currency == "KZT"
    assert qualified.basis_quantity == Decimal("1")
    assert qualified.basis_unit == "км"
    lower_bound = parse_price("от 1 000,00 р", allow_qualified=True)
    assert lower_bound.status == "ok"
    assert lower_bound.qualifier == "from"
    assert lower_bound.amount_from == Decimal("1000.00")
    assert parse_price("от 100 руб за единицу", allow_qualified=True).qualifier == "from"
    assert parse_price("от 2500,00 рублей", allow_qualified=True).qualifier == "from"
    assert parse_price("480/390", allow_qualified=True).status == "on_request"
    assert parse_price("8500//8900", allow_qualified=True).status == "on_request"
    assert parse_price(r"3700\4300", allow_qualified=True).status == "on_request"
    assert parse_price("390/450/1330", allow_qualified=True).status == "on_request"
    assert parse_price("по заявке").status == "on_request"
    assert parse_price("бесплатно").amount == Decimal("0.00")
    assert parse_price("без оплаты").amount == Decimal("0.00")
    assert parse_price(None).status == "empty"
    assert parse_price("").status == "empty"
    assert parse_price("*").status == "empty"
    assert parse_price("***").status == "empty"
    assert parse_price("14300р.**").amount == Decimal("14300.00")
    assert parse_price("по запр").status == "on_request"


def test_price_evidence_rejects_requisites_numbers():
    """ИНН и банковские счета парсятся как числа, но ценой не являются."""
    assert parse_price("043601607").status == "ok"
    assert not is_price_evidence("043601607")
    assert not is_price_evidence("30101810200000000607")
    assert not is_price_evidence("40702810954400017252")
    # Десятизначный ИНН без ведущего нуля остаётся правдоподобным числом —
    # осознанное ограничение, для таких строк есть override с причиной.
    assert is_price_evidence("7707083893")
    # Цена, записанная с ведущими нулями, доказательством не считается.
    assert not is_price_evidence("007,50")


def test_price_evidence_keeps_money_and_qualified_values():
    assert is_price_evidence("5786.00")
    assert is_price_evidence("5 786,00")
    assert is_price_evidence("1 234,56*")
    assert is_price_evidence(4367.0)
    assert is_price_evidence(Decimal("4367.00"))
    assert is_price_evidence("0,5")
    assert is_price_evidence("123456789012")  # 12 разрядов — граница numeric(14,2)
    assert not is_price_evidence("1234567890123")  # 13 разрядов
    assert not is_price_evidence(123456789012345.0)
    # Квалифицированные значения проверку правдоподобия не проходят намеренно.
    assert is_price_evidence("1234 руб")
    assert is_price_evidence("от 100 руб")
    assert is_price_evidence("100-200 руб")
    assert is_price_evidence("по запросу")
    assert is_price_evidence("бесплатно")
    assert is_price_evidence("50 тенге за 1 км")
    # Не цены — как и у parse_price.
    assert not is_price_evidence("итого")
    assert not is_price_evidence("МКПВ-20/3С2Р2")
    assert not is_price_evidence(None)
    assert not is_price_evidence("")
    assert parse_price("под заказ").status == "on_request"
    assert parse_price("ожидается поставка").status == "on_request"
    assert parse_price("итого:").status == "empty"
    assert parse_price("782 тг").amount == Decimal("782.00")
    assert parse_price("н/д цена").status == "error"


def test_parse_quantity_tier_bounds():
    up_to = parse_quantity_tier("до 50 шт.")
    assert up_to is not None
    assert up_to.min_quantity is None
    assert up_to.max_quantity == Decimal("50")
    assert up_to.unit == "шт"

    over = parse_quantity_tier("свыше 50 шт.")
    assert over is not None
    assert over.min_quantity == Decimal("51")
    assert over.max_quantity is None
    assert over.unit == "шт"

    from_n = parse_quantity_tier("от 100 шт.")
    assert from_n is not None
    assert from_n.min_quantity == Decimal("100")
    assert from_n.max_quantity is None

    ranged = parse_quantity_tier("50-100 шт.")
    assert ranged is not None
    assert ranged.min_quantity == Decimal("50")
    assert ranged.max_quantity == Decimal("100")

    assert parse_quantity_tier("Наименование") is None
    assert parse_quantity_tier("Цена") is None
    assert parse_quantity_tier(None) is None
    assert parse_quantity_tier("") is None


def test_currency_marker_does_not_match_letters_inside_russian_headers():
    from app.parsing.normalize import currency_code, currency_from_number_format

    assert currency_code("Прайс") is None
    assert currency_code("Розница") is None
    assert currency_code("Размер") is None
    assert currency_code("Цена, тенге") == "KZT"
    assert currency_from_number_format('#,##0"р."') == "RUB"
    assert currency_from_number_format('#,##0.00\\ [$€-1]') == "EUR"


def test_wrapped_word_in_header_is_normalized_without_artificial_space():
    assert suggest_role("Наименова-\nние").role == "name"


def test_normalize_name_and_unit():
    assert normalize_name("  Муфта  Ёлочная ") == "муфта елочная"
    assert normalize_unit("шт.") == "шт"
    assert normalize_unit("пог.м") == "м.п."
    assert normalize_unit("кв.м") == "м2"
    assert normalize_unit("комплект") == "компл"


def test_indexed_normalized_keys_are_bounded_but_source_can_stay_verbatim():
    verbose = "😀" * (MAX_INDEXED_NORMALIZED_TEXT_CHARS + 100)

    assert normalize_name(verbose) == verbose[:MAX_INDEXED_NORMALIZED_TEXT_CHARS]
    assert normalize_article(verbose) == verbose[:MAX_INDEXED_NORMALIZED_TEXT_CHARS]

    grid = SheetGrid(
        name="Прайс",
        rows=[
            [Cell(value="Наименование"), Cell(value="Цена")],
            [Cell(value=verbose), Cell(value="100")],
        ],
    )
    item = parse_grids([grid], build_auto_mapping([grid])).items[0]
    assert item.source_name == verbose
    assert item.normalized_name == verbose[:MAX_INDEXED_NORMALIZED_TEXT_CHARS]


def test_preheader_section_caption_becomes_item_category():
    grid = SheetGrid(
        name="Лист1",
        rows=[
            [Cell(value="ШКАФЫ ПОЖАРНЫЕ")],
            [Cell(value=None)],
            [Cell(value="Наименование"), Cell(value="Цена")],
            [Cell(value="ШПК-310Н"), Cell(value=1188)],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert result.items[0].category_path == "ШКАФЫ ПОЖАРНЫЕ"


def test_mixed_case_preheader_section_caption_becomes_item_category():
    grid = SheetGrid(
        name="Лист1",
        rows=[
            [Cell(value="Шкафы пожарные для комплектации")],
            [Cell(value="Наименование"), Cell(value="Цена")],
            [Cell(value="ШПК-310Н"), Cell(value=1188)],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert result.items[0].category_path == "Шкафы пожарные для комплектации"


def test_preheader_price_notice_is_not_item_category():
    grid = SheetGrid(
        name="Лист1",
        rows=[
            [Cell(value="Цены указаны на 15.12.2017")],
            [Cell(value="Наименование"), Cell(value="Цена")],
            [Cell(value="ШПК-310Н"), Cell(value=1188)],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert result.items[0].category_path is None


def test_code_on_its_own_name_line_becomes_article():
    grid = SheetGrid(
        name="Лист1",
        rows=[
            [Cell(value="Наименование"), Cell(value="Цена")],
            [
                Cell(value="ШПК-310Н\nШкаф для пожарного крана"),
                Cell(value=1188),
            ],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert result.items[0].source_article == "ШПК-310Н"


def test_dimensions_column_is_kept_on_parsed_item():
    grid = SheetGrid(
        name="Лист1",
        rows=[
            [Cell(value="Наименование"), Cell(value="Габариты"), Cell(value="Цена")],
            [Cell(value="ШПК-310Н"), Cell(value="540х640х230"), Cell(value=1188)],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert getattr(result.items[0], "dimensions", None) == "540х640х230"


def test_short_description_recovers_a_missing_product_name_on_a_priced_row():
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [Cell(value="Наименование"), Cell(value="Краткая характеристика"), Cell(value="Цена")],
            [Cell(value=None), Cell(value="ЦПИУ Орион на 512 устройств"), Cell(value="400400.52")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert [(item.source_name, item.description, item.values[0].amount) for item in result.items] == [
        (
            "ЦПИУ Орион на 512 устройств",
            "ЦПИУ Орион на 512 устройств",
            Decimal("400400.52"),
        )
    ]
    assert [(issue.code, issue.severity) for issue in result.issues] == [
        ("NAME_FROM_DESCRIPTION", "warning")
    ]


def test_target_price_columns_are_retained_in_parsed_item():
    grid = SheetGrid(
        name="pdf",
        rows=[
            [
                Cell(value="Наименование"),
                Cell(value="Артикул"),
                Cell(value="Толщина\nметалла"),
                Cell(value="Габаритные\nразмеры\nДлина/глубина\n/высота, мм"),
                Cell(value="Описание"),
                Cell(value="Цена, с НДС"),
            ],
            [
                Cell(value="Пост пожарный"),
                Cell(value="ППМП-02"),
                Cell(value="Лист 0.8 мм"),
                Cell(value="1000х900х1670"),
                Cell(value="Передвижной пост для средств пожаротушения"),
                Cell(value="64 500"),
            ],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    item = result.items[0]
    assert item.source_article == "ППМП-02"
    assert item.dimensions == "1000х900х1670"
    assert item.description == "Передвижной пост для средств пожаротушения"
    assert [
        (value.source_header, value.raw_value, value.attribute_code, value.value_kind)
        for value in item.attribute_values
    ] == [("Толщина металла", "Лист 0.8 мм", None, "text")]


def test_recognized_variable_columns_are_retained_as_labeled_attributes():
    grid = SheetGrid(
        name="Лист1",
        rows=[
            [
                Cell(value="Наименование"),
                Cell(value="Производитель"),
                Cell(value="Вес, кг"),
                Cell(value="Наличие"),
                Cell(value="Цена"),
            ],
            [
                Cell(value="Шкаф пожарный"),
                Cell(value="DocPrice"),
                Cell(value="42"),
                Cell(value="В наличии"),
                Cell(value="1000"),
            ],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert [
        (value.source_header, value.raw_value, value.attribute_code, value.value_kind, value.canonical_unit)
        for value in result.items[0].attribute_values
    ] == [
        ("Производитель", "DocPrice", "manufacturer", "text", None),
        ("Вес, кг", "42", "weight", "decimal", "kg"),
        ("Наличие", "В наличии", "stock", "text", None),
    ]


def test_unknown_named_columns_are_retained_as_draft_attributes():
    grid = SheetGrid(
        name="Лист1",
        rows=[
            [
                Cell(value="Наименование"),
                Cell(value="Материал корпуса"),
                Cell(value="Класс защиты"),
                Cell(value="Цена"),
            ],
            [
                Cell(value="Шкаф пожарный"),
                Cell(value="Сталь"),
                Cell(value="IP54"),
                Cell(value="1000"),
            ],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert [
        (value.source_header, value.raw_value, value.attribute_code, value.value_kind)
        for value in result.items[0].attribute_values
    ] == [
        ("Материал корпуса", "Сталь", None, "text"),
        ("Класс защиты", "IP54", None, "text"),
    ]


def test_price_like_column_is_never_saved_as_a_generic_attribute():
    grid = SheetGrid(
        name="Лист1",
        rows=[
            [
                Cell(value="Наименование"),
                Cell(value="Цена"),
                Cell(value="Стоимость дилера"),
            ],
            [Cell(value="Шкаф пожарный"), Cell(value="1000"), Cell(value="800")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert result.items[0].attribute_values == []
    assert [(value.kind, value.amount) for value in result.items[0].values] == [
        ("other", Decimal("1000.00")),
        ("dealer", Decimal("800.00")),
    ]


def test_named_rows_with_empty_price_are_skipped_as_reference_content():
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [Cell("Наименование"), Cell("Ед. изм."), Cell("Цена")],
            [Cell("Товар с ценой"), Cell("шт"), Cell("100")],
            [Cell("Справочная позиция"), Cell("шт"), Cell(None)],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert [item.source_name for item in result.items] == ["Товар с ценой"]
    assert result.rows_accepted == 1
    assert any(issue.code == "NO_PRICE" for issue in result.issues)


def test_unnamed_structural_row_without_price_does_not_fail_quality_gate():
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [Cell("Наименование"), Cell("Размер"), Cell("Цена")],
            [Cell("Товар с ценой"), Cell("100x100"), Cell("100")],
            [Cell(None), Cell("200x200"), Cell(None)],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert [item.source_name for item in result.items] == ["Товар с ценой"]
    assert not any(issue.code == "NO_NAME" for issue in result.issues)


def test_parallel_horizontal_panels_create_two_independent_products():
    """A right-hand price must never be attached to the left-hand product."""
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [
                Cell(value="Наименование"),
                Cell(value="Ед. изм."),
                Cell(value="Цена"),
                Cell(value="Наименование"),
                Cell(value="Ед. изм."),
                Cell(value="Цена"),
            ],
            [
                Cell(value="Шкаф левый"),
                Cell(value="шт"),
                Cell(value="1000"),
                Cell(value="Шкаф правый"),
                Cell(value="компл"),
                Cell(value="2000"),
            ],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert result.rows_accepted == 2
    assert [
        (item.source_name, item.unit_code, [(value.kind, value.amount) for value in item.values])
        for item in result.items
    ] == [
        ("Шкаф левый", "шт", [("other", Decimal("1000.00"))]),
        ("Шкаф правый", "компл", [("other", Decimal("2000.00"))]),
    ]


def test_merged_name_fill_inside_price_column_is_not_parsed_as_money():
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [Cell("Наименование"), Cell("Опт"), Cell("Розница")],
            [
                Cell("Товар по запросу"),
                Cell("Товар по запросу", is_merged_fill=True),
                Cell("1200"),
            ],
        ],
    )
    decision = MappingDecision(
        tables=[
            TableMapping(
                sheet_name="Прайс",
                header_row_index=0,
                columns=[
                    ColumnMapping(index=0, role="name"),
                    ColumnMapping(
                        index=1,
                        role="price",
                        price_descriptor={"kind": "wholesale", "vat_mode": "unknown"},
                    ),
                    ColumnMapping(
                        index=2,
                        role="price",
                        price_descriptor={"kind": "retail", "vat_mode": "unknown"},
                    ),
                ],
            )
        ],
        auto_ok=True,
    )

    result = parse_grids([grid], decision)

    assert not any(issue.code == "PRICE_PARSE_ERROR" for issue in result.issues)
    assert result.rows_accepted == 1
    assert [(value.kind, value.amount) for value in result.items[0].values] == [
        ("retail", Decimal("1200.00"))
    ]


def test_sparse_parallel_panel_rows_do_not_leak_neighbouring_product_as_category():
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [
                Cell(value="Наименование"),
                Cell(value="Цена"),
                Cell(value="Наименование"),
                Cell(value="Цена"),
            ],
            [Cell(value="Товар слева"), Cell(value="100"), Cell(value=None), Cell(value=None)],
            [Cell(value=None), Cell(value=None), Cell(value="Товар справа"), Cell(value="200")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert [(item.source_name, item.category_path) for item in result.items] == [
        ("Товар слева", None),
        ("Товар справа", None),
    ]


def _merged_panel_caption(
    text: str,
    *,
    start: int,
    width: int,
    total_width: int,
) -> list[Cell]:
    row = [Cell(value=None) for _ in range(total_width)]
    row[start] = Cell(value=text, is_bold=True)
    for index in range(start + 1, start + width):
        row[index] = Cell(value=text, is_bold=True, is_merged_fill=True)
    return row


def test_explanatory_row_does_not_replace_confirmed_panel_category() -> None:
    heading = _merged_panel_caption(
        "Пирамиды для гидрантов", start=0, width=4, total_width=9
    )
    right_heading = _merged_panel_caption(
        "Колонки водоразборные", start=5, width=4, total_width=9
    )
    heading[5:9] = right_heading[5:9]
    note = _merged_panel_caption(
        "Для разбора питьевой воды. Указана фактическая длина всей колонки",
        start=5,
        width=4,
        total_width=9,
    )
    note[0:4] = [
        Cell(value="Пирамида ПГ-1"),
        Cell(value="шт"),
        Cell(value="1246"),
        Cell(value="1682"),
    ]
    grid = SheetGrid(
        name="Каталог",
        rows=[
            [
                Cell("Наименование продукции"),
                Cell("Изм."),
                Cell("Цена опт"),
                Cell("Цена розница"),
                Cell(None),
                Cell("Наименование продукции"),
                Cell("Изм."),
                Cell("Цена опт"),
                Cell("Цена розница"),
            ],
            heading,
            note,
            [
                Cell("Пирамида ПГ-2"),
                Cell("шт"),
                Cell("3870"),
                Cell("5225"),
                Cell(None),
                Cell("Колонка водоразборная 1,50 м"),
                Cell("шт"),
                Cell("7575"),
                Cell("10226"),
            ],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))
    item = next(
        item for item in result.items if item.source_name == "Колонка водоразборная 1,50 м"
    )

    assert item.category_path == "Колонки водоразборные"


@pytest.mark.parametrize(
    "heading",
    [
        "Огнетушащий порошок",
        "Гидранты",
        "Рукава пожарные",
        "ОП. Порошковые",
    ],
)
def test_structural_category_heading_is_preserved(heading: str) -> None:
    grid = SheetGrid(
        name="Каталог",
        rows=[
            [Cell("Наименование"), Cell("Цена")],
            [Cell(heading, is_bold=True), Cell(heading, is_merged_fill=True)],
            [Cell("Товар A"), Cell("100")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert result.items[0].category_path == heading


def test_explanatory_note_after_heading_preserves_current_category() -> None:
    grid = SheetGrid(
        name="Каталог",
        rows=[
            [Cell("Наименование"), Cell("Цена")],
            [Cell("Огнетушащий порошок"), Cell(None)],
            [Cell("Для использования в специальных установках."), Cell(None)],
            [Cell("Товар A"), Cell("100")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert result.items[0].category_path == "Огнетушащий порошок"


def test_explanatory_note_after_completed_item_block_clears_stale_category() -> None:
    grid = SheetGrid(
        name="Каталог",
        rows=[
            [Cell("Наименование"), Cell("Цена")],
            [Cell("Подставки под огнетушитель"), Cell(None)],
            [Cell("Подставка A"), Cell("100")],
            [Cell("Подставка B"), Cell("110")],
            [Cell("Чехлы изготовлены из защитного материала. Цвет красный."), Cell(None)],
            [Cell(None), Cell(None)],
            [Cell("Чехол A"), Cell("200")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    by_name = {item.source_name: item for item in result.items}
    assert by_name["Подставка A"].category_path == "Подставки под огнетушитель"
    assert by_name["Подставка B"].category_path == "Подставки под огнетушитель"
    assert by_name["Чехол A"].category_path is None


def test_explanatory_note_without_heading_does_not_create_category() -> None:
    grid = SheetGrid(
        name="Лист1",
        rows=[
            [Cell("Наименование"), Cell("Цена")],
            [Cell("Для применения в специальных установках и помещениях."), Cell(None)],
            [Cell("Товар A"), Cell("100")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert result.items[0].category_path is None


def test_long_explanatory_clause_without_terminal_punctuation_is_not_category() -> None:
    grid = SheetGrid(
        name="Каталог",
        rows=[
            [Cell("Наименование"), Cell("Цена")],
            [
                Cell(
                    "Обращаем внимание, что изделия поставляются без дополнительных "
                    "соединительных деталей и монтажных приспособлений"
                ),
                Cell(None),
            ],
            [Cell("Товар A"), Cell("100")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert result.items[0].category_path is None


def test_cover_block_does_not_inherit_completed_stand_category() -> None:
    grid = SheetGrid(
        name="Каталог",
        rows=[
            [Cell("Наименование"), Cell("Цена")],
            [Cell("Подставки под огнетушитель"), Cell(None)],
            [Cell("Подставка универсальная"), Cell("100")],
            [
                Cell(
                    "Чехлы для огнетушителя — материал ТЕНТ. "
                    "Красный цвет, полуглянец."
                ),
                Cell(None),
            ],
            [Cell(None), Cell(None)],
            [Cell("Чехол ОП-5"), Cell("200")],
            [Cell("Чехол ОУ-5"), Cell("210")],
            [Cell("Чехол ОП-10"), Cell("220")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert [item.category_path for item in result.items[-3:]] == [None, None, None]


def test_service_items_after_hose_prose_have_no_category() -> None:
    grid = SheetGrid(
        name="Каталог",
        rows=[
            [Cell("Наименование"), Cell("Цена")],
            [Cell("Рукава пожарные"), Cell(None)],
            [Cell("Рукав напорный"), Cell("100")],
            [
                Cell(
                    "Рукава сертифицированы по техническому регламенту. "
                    "Сведения приведены для справки."
                ),
                Cell(None),
            ],
            [Cell("С внутренним гидроизоляционным покрытием из полиуретана"), Cell(None)],
            [Cell("Технические условия и сертификат соответствия"), Cell(None)],
            [Cell("(поставляется без соединительных муфт на навязку)"), Cell(None)],
            [Cell(None), Cell(None)],
            [Cell(None), Cell(None)],
            [
                Cell(
                    "Обращаем внимание, что часть изделий поставляется без дополнительных "
                    "деталей и монтажных приспособлений"
                ),
                Cell(None),
            ],
            [Cell(None), Cell(None)],
            [Cell("Услуги по комплектации Ду-25"), Cell("200")],
            [Cell("Услуги по комплектации Ду-40"), Cell("210")],
            [Cell("Услуги по комплектации Ду-50"), Cell("220")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))
    services = [item for item in result.items if item.source_name.startswith("Услуги")]

    assert len(services) == 3
    assert all(item.category_path is None for item in services)


def test_headerless_refining_caption_keeps_meaningful_fallback_parent() -> None:
    grid = SheetGrid(
        name="Обслуживание",
        rows=[
            [Cell("Обслуживание порошковых огнетушителей"), Cell(None), Cell(None)],
            [Cell("Сервисная позиция"), Cell("шт"), Cell("100")],
        ],
    )
    decision = MappingDecision(
        tables=[
            TableMapping(
                sheet_name=grid.name,
                header_row_index=None,
                start_row_index=0,
                columns=[
                    ColumnMapping(index=0, role="name"),
                    ColumnMapping(index=1, role="unit"),
                    ColumnMapping(index=2, role="price"),
                ],
                default_category_path="Обслуживание",
            )
        ],
        auto_ok=True,
    )

    result = parse_grids([grid], decision)

    assert result.items[0].category_path == (
        "Обслуживание / Обслуживание порошковых огнетушителей"
    )


def test_uppercase_explanatory_prose_does_not_create_category() -> None:
    grid = SheetGrid(
        name="Лист1",
        rows=[
            [Cell("Наименование"), Cell("Цена")],
            [
                Cell(
                    "ТЕХНИЧЕСКОЕ ОПИСАНИЕ ИЗДЕЛИЯ. "
                    "ПАРАМЕТРЫ ПРИВЕДЕНЫ ТОЛЬКО ДЛЯ СПРАВКИ."
                ),
                Cell(None),
            ],
            [Cell("Товар A"), Cell("100")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert result.items[0].category_path is None


def test_explanatory_note_does_not_leak_between_horizontal_panels() -> None:
    grid = SheetGrid(
        name="Каталог",
        rows=[
            [
                Cell("Наименование"),
                Cell("Цена"),
                Cell("Наименование"),
                Cell("Цена"),
            ],
            [Cell("Гидранты"), Cell(None), Cell("Рукава пожарные"), Cell(None)],
            [
                Cell("Для наружной установки и обслуживания оборудования."),
                Cell(None),
                Cell("Рукав B"),
                Cell("200"),
            ],
            [Cell("Гидрант A"), Cell("100"), Cell(None), Cell(None)],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))
    by_name = {item.source_name: item for item in result.items}

    assert by_name["Гидрант A"].category_path == "Гидранты"
    assert by_name["Рукав B"].category_path == "Рукава пожарные"


def test_parallel_panels_own_their_row_number_category_captions() -> None:
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [
                Cell("№"), Cell("Наименование"), Cell("Цена"),
                Cell("№"), Cell("Наименование"), Cell("Цена"),
            ],
            [Cell("1. Оборудование"), Cell(None), Cell(None), Cell("2. Перезарядка"), Cell(None), Cell(None)],
            [Cell("1.1. Огнетушители"), Cell(None), Cell(None), Cell("2.1. Огнетушители"), Cell(None), Cell(None)],
            [Cell("1"), Cell("Огнетушитель"), Cell("1000"), Cell("77"), Cell("Перезарядка"), Cell("500")],
            [Cell("1.2. Комплектующие"), Cell(None), Cell(None), Cell("3. Услуги"), Cell(None), Cell(None)],
            [Cell("2"), Cell("Кронштейн"), Cell("200"), Cell("106"), Cell("Испытание лестниц"), Cell("Смета")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert [(item.source_name, item.category_path) for item in result.items] == [
        ("Огнетушитель", "1. Оборудование / 1.1. Огнетушители"),
        ("Кронштейн", "1. Оборудование / 1.2. Комплектующие"),
        ("Перезарядка", "2. Перезарядка / 2.1. Огнетушители"),
        ("Испытание лестниц", "3. Услуги"),
    ]
    estimate = next(item for item in result.items if item.source_name == "Испытание лестниц")
    assert len(estimate.values) == 1
    assert estimate.values[0].is_on_request is True
    assert estimate.values[0].amount is None


def test_ambiguous_horizontal_panels_are_rejected_instead_of_merging_prices():
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [Cell(value="Наименование"), Cell(value="Цена"), Cell(value="Наименование")],
            [Cell(value="Шкаф левый"), Cell(value="1000"), Cell(value="Шкаф правый")],
        ],
    )
    decision = MappingDecision(
        tables=[
            TableMapping(
                sheet_name="Прайс",
                header_row_index=0,
                columns=[
                    ColumnMapping(index=0, role="name"),
                    ColumnMapping(
                        index=1,
                        role="price",
                        price_descriptor={"kind": "other", "vat_mode": "unknown"},
                    ),
                    ColumnMapping(index=2, role="name"),
                ],
            )
        ],
        auto_ok=True,
    )

    result = parse_grids([grid], decision)

    assert result.needs_mapping
    assert result.items == []
    assert [issue.code for issue in result.issues] == ["AMBIGUOUS_HORIZONTAL_PANELS"]


def test_stored_mapping_retains_unlisted_unknown_column_as_attribute():
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [
                Cell(value="Наименование"),
                Cell(value="Материал корпуса"),
                Cell(value="Цена"),
            ],
            [Cell(value="Шкаф"), Cell(value="Сталь"), Cell(value="1000")],
        ],
    )
    mapping = {
        "tables": [
            {
                "sheet_name": "Прайс",
                "columns": [
                    {"index": 0, "role": "name"},
                    {"index": 2, "role": "price"},
                ],
            }
        ]
    }

    result = parse_grids([grid], stored_mapping_to_tables(mapping, [grid]))

    assert [(value.source_header, value.raw_value) for value in result.items[0].attribute_values] == [
        ("Материал корпуса", "Сталь")
    ]


def test_stored_mapping_respects_explicit_ignore_for_unknown_column():
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [
                Cell(value="Наименование"),
                Cell(value="Материал корпуса"),
                Cell(value="Цена"),
            ],
            [Cell(value="Шкаф"), Cell(value="Сталь"), Cell(value="1000")],
        ],
    )
    mapping = {
        "tables": [
            {
                "sheet_name": "Прайс",
                "columns": [
                    {"index": 0, "role": "name"},
                    {"index": 1, "role": "ignore"},
                    {"index": 2, "role": "price"},
                ],
            }
        ]
    }

    result = parse_grids([grid], stored_mapping_to_tables(mapping, [grid]))

    assert result.items[0].attribute_values == []


def test_multiple_vertical_tables_use_their_own_columns() -> None:
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [
                Cell(value="Наименование"),
                Cell(value="Цвет"),
                Cell(value="Толщина металла"),
                Cell(value="Цена"),
            ],
            [Cell(value="Шкаф"), Cell(value="серый"), Cell(value="0,8 мм"), Cell(value="100")],
            [Cell(value=None), Cell(value=None), Cell(value=None), Cell(value=None)],
            [
                Cell(value="Кол-во секций"),
                Cell(value="Толщина металла"),
                Cell(value="Опт-1"),
                Cell(value=None),
            ],
            [Cell(value="2"), Cell(value="0,8 мм"), Cell(value="200"), Cell(value=None)],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert [(item.source_name, item.source_row) for item in result.items] == [
        ("Шкаф", 2),
        ("2", 5),
    ]
    assert [
        (value.source_header, value.raw_value)
        for item in result.items
        for value in item.attribute_values
        if value.source_header == "Толщина металла"
    ] == [
        ("Толщина металла", "0,8 мм"),
        ("Толщина металла", "0,8 мм"),
    ]
    assert [item.values[0].amount for item in result.items] == [Decimal("100.00"), Decimal("200.00")]


def test_stored_mapping_first_section_does_not_misparse_later_table() -> None:
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [
                Cell(value="Наименование"),
                Cell(value="Цвет"),
                Cell(value="Толщина металла"),
                Cell(value="Цена"),
            ],
            [Cell(value="Шкаф"), Cell(value="серый"), Cell(value="0,8 мм"), Cell(value="100")],
            [Cell(value=None), Cell(value=None), Cell(value=None), Cell(value=None)],
            [
                Cell(value="Кол-во секций"),
                Cell(value="Толщина металла"),
                Cell(value="Опт-1"),
                Cell(value=None),
            ],
            [Cell(value="2"), Cell(value="0,8 мм"), Cell(value="200"), Cell(value=None)],
        ],
    )
    first_section_mapping = {
        "tables": [
            {
                "sheet_name": "Прайс",
                "header_row_index": 0,
                "columns": [
                    {"index": 0, "role": "name"},
                    {"index": 3, "role": "price"},
                ],
            }
        ]
    }

    result = parse_grids([grid], stored_mapping_to_tables(first_section_mapping, [grid]))

    assert [(item.source_name, item.values[0].amount) for item in result.items] == [
        ("Шкаф", Decimal("100.00")),
        ("2", Decimal("200.00")),
    ]
    assert [
        value.raw_value
        for item in result.items
        for value in item.attribute_values
        if value.source_header.casefold() == "толщина металла"
    ] == ["0,8 мм", "0,8 мм"]


def test_repeated_identical_header_keeps_category_context() -> None:
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [Cell(value="Наименование"), Cell(value="Цена")],
            [Cell(value="Шкафы"), Cell(value=None)],
            [Cell(value="Шкаф А"), Cell(value="100")],
            [Cell(value="Наименование"), Cell(value="Цена")],
            [Cell(value="Шкафы В"), Cell(value=None)],
            [Cell(value="Шкаф Б"), Cell(value="200")],
        ],
    )

    decision = build_auto_mapping([grid])
    result = parse_grids([grid], decision)

    assert len(decision.tables) == 1
    assert [(item.source_name, item.category_path) for item in result.items] == [
        ("Шкаф А", "Шкафы"),
        ("Шкаф Б", "Шкафы В"),
    ]


def test_price_header_continuation_is_not_a_category() -> None:
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [Cell(value="Ценовая группа / Номенклатура"), Cell(value="Оптовая")],
            [
                Cell(value="Ценовая группа / Номенклатура", is_merged_fill=True),
                Cell(value="Цена"),
            ],
            [Cell(value="    Крепёж"), Cell(value=None)],
            [Cell(value="        Анкер"), Cell(value="100")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert [(item.source_name, item.category_path) for item in result.items] == [
        ("Анкер", "Крепёж")
    ]


def test_nonempty_unrecognized_price_does_not_turn_product_into_category() -> None:
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [Cell(value="Наименование"), Cell(value="Цена")],
            [Cell(value="Огнетушители"), Cell(value=None)],
            [Cell(value="ОВЭ-10 летний/зимний"), Cell(value="15000/16000")],
            [Cell(value="Шкаф пожарный"), Cell(value="2500")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert [(item.source_name, item.category_path) for item in result.items] == [
        ("Шкаф пожарный", "Огнетушители")
    ]
    assert any(
        issue.code == "PRICE_PARSE_ERROR" and issue.row_no == 3
        for issue in result.issues
    )


def test_indented_category_siblings_replace_their_level() -> None:
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [Cell(value="Наименование"), Cell(value="Цена")],
            [Cell(value="    Инструмент"), Cell(value=None)],
            [Cell(value="        Ручной"), Cell(value=None)],
            [Cell(value="            Ключи"), Cell(value=None)],
            [Cell(value="                Ключ 10"), Cell(value="100")],
            [Cell(value="        Электрический"), Cell(value=None)],
            [Cell(value="            Дрели"), Cell(value=None)],
            [Cell(value="                Дрель 500"), Cell(value="200")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert [(item.source_name, item.category_path) for item in result.items] == [
        ("Ключ 10", "Инструмент / Ручной / Ключи"),
        ("Дрель 500", "Инструмент / Электрический / Дрели"),
    ]


def test_flat_outline_categories_replace_numbered_siblings_and_their_children() -> None:
    grid = SheetGrid(
        name="Каталог",
        rows=[
            [Cell(value="Наименование"), Cell(value="Цена")],
            [Cell(value="3.3 Компоненты оптических сетей"), Cell(value=None)],
            [Cell(value="Патч-корды"), Cell(value=None)],
            [Cell(value="Шнур"), Cell(value="100")],
            [Cell(value="3.4 Инструменты для ВОЛС"), Cell(value=None)],
            [Cell(value="Скалыватели"), Cell(value=None)],
            [Cell(value="Скалыватель"), Cell(value="200")],
            [Cell(value="12 В"), Cell(value=None)],
            [Cell(value="Блок питания"), Cell(value="300")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert [(item.source_name, item.category_path) for item in result.items] == [
        ("Шнур", "3.3 Компоненты оптических сетей / Патч-корды"),
        ("Скалыватель", "3.4 Инструменты для ВОЛС / Скалыватели"),
        ("Блок питания", "3.4 Инструменты для ВОЛС / 12 В"),
    ]


def test_flat_consecutive_category_captions_are_siblings() -> None:
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [Cell(value="Наименование"), Cell(value="Цена")],
            [Cell(value="Старая группа"), Cell(value=None)],
            [Cell(value="Актуальная группа"), Cell(value=None)],
            [Cell(value="Товар"), Cell(value="100")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert result.items[0].category_path == "Актуальная группа"


def test_category_context_keeps_only_the_six_most_local_levels() -> None:
    rows = [[Cell(value="Наименование"), Cell(value="Цена")]]
    rows.extend(
        [Cell(value=(" " * (level * 4)) + f"Уровень {level}"), Cell(value=None)]
        for level in range(1, 9)
    )
    rows.append([Cell(value=" " * 36 + "Товар"), Cell(value="100")])
    grid = SheetGrid(name="Прайс", rows=rows)

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert result.items[0].category_path == " / ".join(
        f"Уровень {level}" for level in range(3, 9)
    )


def test_price_uses_the_stored_value_when_the_format_hides_kopecks():
    """The stored cell value is the price of record, not what Excel prints.

    A zero-decimal number format shows 1187.59 as 1188; persisting the display
    rounds every such price to whole roubles, which is what a catalogue then
    quotes to a customer.
    """
    price = Cell(value=1187.59)
    price.display_value = "1188"  # type: ignore[attr-defined]
    grid = SheetGrid(
        name="Лист1",
        rows=[
            [Cell(value="Наименование"), Cell(value="Цена")],
            [Cell(value="ШПК-310Н"), price],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert result.items[0].values[0].amount == Decimal("1187.59")


def test_empty_worksheet_does_not_create_a_non_price_warning():
    price_sheet = SheetGrid(
        name="Прайс",
        rows=[
            [Cell(value="Наименование"), Cell(value="Цена")],
            [Cell(value="Клапан"), Cell(value=100)],
        ],
    )
    empty_sheet = SheetGrid(name="Пустой лист")

    result = parse_grids(
        [price_sheet, empty_sheet],
        build_auto_mapping([price_sheet, empty_sheet]),
    )

    assert not result.needs_mapping
    assert result.rows_accepted == 1
    assert result.issues == []


def test_meaningful_sheet_title_is_fallback_category_but_generic_title_is_not() -> None:
    meaningful = SheetGrid(
        name="Крепёж",
        rows=[
            [Cell(value="Наименование"), Cell(value="Цена")],
            [Cell(value="Анкер"), Cell(value="100")],
        ],
    )
    generic = SheetGrid(
        name="Лист1",
        rows=[
            [Cell(value="Наименование"), Cell(value="Цена")],
            [Cell(value="Болт"), Cell(value="200")],
        ],
    )

    result = parse_grids(
        [meaningful, generic], build_auto_mapping([meaningful, generic])
    )

    assert [(item.source_name, item.category_path) for item in result.items] == [
        ("Анкер", "Крепёж"),
        ("Болт", None),
    ]


def test_truncated_excel_catalog_sheet_title_is_not_a_category() -> None:
    grid = SheetGrid(
        name="Каталог - Оптические кабели свя",
        rows=[
            [Cell(value="Наименование"), Cell(value="Цена")],
            [Cell(value="Кабель"), Cell(value="100")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert result.items[0].category_path is None


def test_non_price_sheet_warning_contains_a_compact_preview():
    price_sheet = SheetGrid(
        name="Прайс",
        rows=[
            [Cell(value="Наименование"), Cell(value="Цена")],
            [Cell(value="Клапан"), Cell(value=100)],
        ],
    )
    notes_sheet = SheetGrid(
        name="Примечания",
        rows=[
            [Cell(value="Условия поставки")],
            [Cell(value="Оплата по счёту")],
        ],
    )

    result = parse_grids(
        [price_sheet, notes_sheet],
        build_auto_mapping([price_sheet, notes_sheet]),
    )

    issue = next(issue for issue in result.issues if issue.sheet == "Примечания")
    assert issue.source_value is not None
    assert "2 строки × 1 колонка" in issue.source_value
    assert "Условия поставки" in issue.source_value


# --- end-to-end pipeline against the real corpus -----------------------------

@requires_corpus
def test_ssd_parses_end_to_end():
    result = parse_workbook(SSD_FILE.read_bytes(), SSD_FILE.name)

    assert not result.needs_mapping
    assert len(result.items) > 50
    assert result.rows_accepted == len(result.items)

    # prices are Decimal and both retail + wholesale columns were mapped
    kinds = set()
    for item in result.items:
        for value in item.values:
            if not value.is_on_request:
                assert isinstance(value.amount, Decimal)
            kinds.add(value.kind)
    assert {"retail", "wholesale"} <= kinds

    # a known first data row
    first = result.items[0]
    assert first.source_article == "120201-00003"
    assert first.unit_code == "шт"
    retail = [v for v in first.values if v.kind == "retail"][0]
    assert retail.amount == Decimal("2340.36")
    assert retail.vat_mode == "included"

    # category captions became context, not items
    names = {i.source_name for i in result.items}
    assert "Муфты свинцовые для ЖД" not in names
    assert any(i.category_path for i in result.items)


@requires_corpus
def test_pb01_xls_preserves_stored_price_and_product_fields():
    """The workbook shows 1188 under a zero-decimal format; it stores 1187.59."""
    result = parse_workbook(PB01_XLS_FILE.read_bytes(), PB01_XLS_FILE.name)

    assert result.rows_accepted == 43
    first = result.items[0]
    assert first.category_path == "ШКАФЫ ПОЖАРНЫЕ"
    assert first.source_article == "ШПК-310Н"
    assert getattr(first, "dimensions", None) == "540х640х230"
    assert first.values[0].amount == Decimal("1187.59")


@requires_corpus
def test_consol_category_hierarchy_uses_indent_and_skips_price_labels():
    result = parse_workbook(CONSOL_XLS_FILE.read_bytes(), CONSOL_XLS_FILE.name)

    first = next(item for item in result.items if item.source_row == 15)
    assert first.category_path == (
        "Асбесто-резинотехнические  изделия / Резино-технические / Камлоки"
    )
    assert all(
        "Цена" not in (item.category_path or "").split(" / ")
        and not (item.category_path or "").startswith("Цены указаны")
        for item in result.items
    )
    last = next(item for item in result.items if item.source_row == 7011)
    assert last.category_path == "Электрооборудование / Элементы питания / Батарейки"


@requires_corpus
def test_centrfire_composite_price_rows_do_not_become_categories():
    result = parse_workbook(DOCX_FILE.read_bytes(), DOCX_FILE.name)
    category_components = {
        component
        for item in result.items
        for component in (item.category_path or "").split(" / ")
        if component
    }

    assert {
        "ОВЭ-10(з)АВСЕ (Ярпож.) до+5/до -40°С",
        "Мостик рукавный МПР-80/150",
        "Ключ К-80/150",
        "ПОЗВОНИТЬ НАМ:  (351) 225-04-39.",
    }.isdisjoint(category_components)
    assert max(
        (len(item.category_path.split(" / ")) for item in result.items if item.category_path),
        default=0,
    ) == 1


@requires_corpus
def test_zel_parallel_pdf_keeps_independent_categories_and_estimate_services():
    result = parse_workbook(ZEL_PDF_FILE.read_bytes(), ZEL_PDF_FILE.name)

    assert not result.needs_mapping
    assert result.rows_accepted == 96
    by_name = {item.source_name: item for item in result.items}
    assert by_name["Углекислотный ОУ-1"].category_path == (
        "1. Оборудование / 1.1. Огнетушители углекислотные"
    )
    assert by_name["Перезарядка ОУ-2"].category_path == (
        "2. Перезарядка / 2.1. Огнетушители углекислотные (перезарядка)"
    )
    assert by_name["Испытание лестниц"].category_path == (
        "3. Пожарные услуги, прочие услуги"
    )
    assert by_name["Испытание лестниц"].values[0].is_on_request is True
    assert by_name["Формат А3 (на бумаге)"].category_path == "4. Планы эвакуации"
    assert by_name["Самоклеющиеся знаки, Звонить 101, ПК, ВЫХОД."].category_path == (
        "5. Прочее"
    )
    service_names = {
        "Испытание лестниц",
        "Обработка дерева/ ткани/ металла",
        "Замер сопротивления изоляции",
        "Декларирование",
        "Проект, монтаж систем АПС, СОУЭ, АУПТ, ВИДЕО",
    }
    assert service_names <= by_name.keys()
    assert all(
        not service_names.intersection((item.category_path or "").split(" / "))
        for item in result.items
    )


@requires_corpus
def test_poseidon_parses_end_to_end():
    result = parse_workbook(POSEIDON_FILE.read_bytes(), POSEIDON_FILE.name)

    assert not result.needs_mapping
    assert len(result.items) > 20

    for item in result.items:
        assert item.normalized_name
        for value in item.values:
            if not value.is_on_request:
                assert isinstance(value.amount, Decimal)

    # category rows ("Пожарное оборудование", "Противопожарные муфты") excluded
    names = {i.source_name for i in result.items}
    assert "Пожарное оборудование" not in names
    assert "Противопожарные муфты" not in names
    assert any(
        i.category_path and "Противопожарные муфты" in i.category_path for i in result.items
    )

    # article + retail price of a known row
    first = result.items[0]
    assert first.source_article == "ТТ000008388"
    assert first.values[0].kind == "retail"
    assert first.values[0].amount == Decimal("201.30")


@requires_corpus
def test_stored_mapping_override():
    """PUT-mapping path: an explicit mapping is honored instead of suggestions."""
    mapping = {
        "tables": [
            {
                "sheet_name": "Прайс-лист",
                "columns": [
                    {"index": 1, "role": "article"},
                    {"index": 2, "role": "name"},
                    {
                        "index": 4,
                        "role": "price",
                        "price_descriptor": {"kind": "other", "vat_mode": "unknown"},
                    },
                ],
            }
        ]
    }
    result = parse_workbook(POSEIDON_FILE.read_bytes(), POSEIDON_FILE.name, mapping)
    assert not result.needs_mapping
    assert len(result.items) > 20
    assert result.items[0].values[0].kind == "other"


@requires_corpus
def test_parse_workbook_accepts_legacy_doc():
    result = parse_workbook(DOC_FILE.read_bytes(), DOC_FILE.name)

    assert result.tables
    assert result.items


@requires_corpus
@pytest.mark.parametrize("document", [PDF_FILE, DOCX_FILE])
def test_parse_workbook_accepts_pdf_and_docx(document: Path):
    result = parse_workbook(document.read_bytes(), document.name)

    assert result.tables
    assert result.items


# --- repeating-section template (category -> header -> positions, repeated) ---

def _repeating_sections_grid() -> SheetGrid:
    """Three identical sections: caption row, header row, item rows."""
    rows = []
    for caption, products in [
        ("Огнетушители", [("ОП-4", "1200"), ("ОП-5", "1500")]),
        ("Пожарные шкафы", [("ШП-К", "900"), ("ШП-О", "1100")]),
        ("Рукава", [("РПК-50", "2500"), ("РПК-65", "3100")]),
    ]:
        rows.append([Cell(value=caption), Cell(value=None), Cell(value=None)])
        rows.append(
            [Cell(value="Наименование"), Cell(value="Ед. изм."), Cell(value="Цена")]
        )
        for name, price in products:
            rows.append([Cell(value=name), Cell(value="шт"), Cell(value=price)])
    return SheetGrid(name="Прайс", rows=rows)


def test_count_header_sections_counts_identical_stacked_headers():
    grid = _repeating_sections_grid()
    detections = detect_header_candidates(grid)
    # Identical stacked headers are deduped to a single detection...
    assert [d.header_row_index for d in detections] == [1]
    # ...but the raw section count sees all three repetitions.
    assert count_header_sections(grid, 1) == 3
    assert count_header_sections(grid, None) == 0


def test_repeating_sections_recipe_is_one_template_and_round_trips():
    from app.parsing.rules import recipe_from_decision, recipe_to_decision

    grid = _repeating_sections_grid()
    decision = build_auto_mapping([grid])
    assert len(decision.tables) == 1

    recipe = recipe_from_decision(decision, [grid])
    assert len(recipe["sheets"]) == 1
    sheet = recipe["sheets"][0]
    assert sheet["repeat"] is True
    assert sheet["repeat_sections"] == 3
    assert sheet["header_row"] is None
    assert not sheet.get("headerless")

    # The template re-locates the header by structure and re-parses identically,
    # keeping the per-section category taken from each caption row.
    replay = parse_grids([grid], recipe_to_decision(recipe, [grid]))
    base = parse_grids([grid], decision)
    assert [(i.source_name, i.category_path) for i in replay.items] == [
        (i.source_name, i.category_path) for i in base.items
    ]
    assert [i.category_path for i in replay.items] == [
        "Огнетушители", "Огнетушители", "Пожарные шкафы", "Пожарные шкафы",
        "Рукава", "Рукава",
    ]


def test_repeating_sections_template_survives_header_shift():
    """A repeat recipe must not pin a row number: prepend two rows and the
    header is re-located by structure instead of failing on the stale index."""
    from app.parsing.rules import recipe_from_decision, recipe_to_decision

    grid = _repeating_sections_grid()
    recipe = recipe_from_decision(build_auto_mapping([grid]), [grid])

    shifted = SheetGrid(
        name="Прайс",
        rows=[
            [Cell(value="Прайс-лист поставщика"), Cell(value=None), Cell(value=None)],
            [Cell(value=None), Cell(value=None), Cell(value=None)],
            *grid.rows,
        ],
    )
    replay = parse_grids([shifted], recipe_to_decision(recipe, [shifted]))
    assert [i.source_name for i in replay.items] == [
        "ОП-4", "ОП-5", "ШП-К", "ШП-О", "РПК-50", "РПК-65",
    ]
    assert [i.category_path for i in replay.items] == [
        "Огнетушители", "Огнетушители", "Пожарные шкафы", "Пожарные шкафы",
        "Рукава", "Рукава",
    ]


def test_interleaved_sections_are_not_collapsed_into_repeat():
    """A -> B -> A: the two A headers cannot be told apart by structure alone,
    so they must keep fixed header rows instead of a repeat template."""
    from app.parsing.rules import recipe_from_decision, recipe_to_decision

    rows = [
        [Cell(value="Наименование"), Cell(value="Цена")],          # A header (row 0)
        [Cell(value="Товар А1"), Cell(value="100")],
        [Cell(value="Товар"), Cell(value="Опт")],                  # B header (row 2)
        [Cell(value="Болт М6"), Cell(value="50")],
        [Cell(value="Наименование"), Cell(value="Цена")],          # A header again (row 4)
        [Cell(value="Товар А2"), Cell(value="200")],
    ]
    grid = SheetGrid(name="Прайс", rows=rows)
    decision = build_auto_mapping([grid])
    assert [t.header_row_index for t in decision.tables] == [0, 2, 4]

    recipe = recipe_from_decision(decision, [grid])
    assert len(recipe["sheets"]) == 3
    assert not any(s.get("repeat") for s in recipe["sheets"])
    assert [s["header_row"] for s in recipe["sheets"]] == [0, 2, 4]

    replay = parse_grids([grid], recipe_to_decision(recipe, [grid]))
    base = parse_grids([grid], decision)
    assert sorted(i.source_name for i in replay.items) == sorted(
        i.source_name for i in base.items
    )


SSD_SIBIRSKY_FILE = CORPUS / "108_ССД_Сибирский_Арсенал.xlsx"

requires_ssd_sibirsky = pytest.mark.skipif(
    not SSD_SIBIRSKY_FILE.is_file(), reason="external price corpus is not available"
)


@requires_ssd_sibirsky
def test_repeating_sections_corpus_ssd_sibirsky_arsenal():
    """The sheet repeats one category -> header -> positions layout 39 times.
    The recipe must store a single repeating template and re-parse to the same
    1489 positions with their per-section categories."""
    from app.parsing import loaders
    from app.parsing.rules import recipe_from_decision, recipe_to_decision

    data = SSD_SIBIRSKY_FILE.read_bytes()
    loader = loaders.detect_loader(data, SSD_SIBIRSKY_FILE.name)
    grids = loaders.load_grids_for(loader, data, SSD_SIBIRSKY_FILE.name)

    decision = build_auto_mapping(grids)
    recipe = recipe_from_decision(decision, grids)

    repeat_sheets = [s for s in recipe["sheets"] if s.get("repeat")]
    assert len(repeat_sheets) == 1
    assert repeat_sheets[0]["repeat_sections"] == 39

    base = parse_grids(grids, decision)
    replay = parse_grids(grids, recipe_to_decision(recipe, grids))
    assert len(base.items) == 1489
    assert sorted(
        (i.source_sheet, i.source_row, i.normalized_name, i.category_path)
        for i in replay.items
    ) == sorted(
        (i.source_sheet, i.source_row, i.normalized_name, i.category_path)
        for i in base.items
    )
    assert len({i.category_path for i in replay.items}) == 39
