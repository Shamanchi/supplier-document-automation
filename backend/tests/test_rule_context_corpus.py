"""Golden extractor gate over the ten reviewed benchmark price lists."""
from __future__ import annotations

import json
from pathlib import Path

from app.parsing import loaders
from app.parsing.headers import AUTO_CONFIDENCE_THRESHOLD, suggest_role
from app.parsing.normalize import parse_price
from app.parsing.rule_proposal import build_tabular_section_contexts


APP_DIR = Path(__file__).resolve().parents[2]
CORPUS_DIR = APP_DIR.parents[1] / "external_price_corpus"
AUDIT_PATH = APP_DIR / "reports/full_price_audit_2026-07-12/rules_for_all_prices.json"
BENCHMARK_FILES = [
    "xls/004_Global_Security_KZ.xls",
    "xls/015_UNFIRE01.xls",
    "xls/032_Интернет-магазин_пожарного_обо.xls",
    "xls/043_НПО_Сибирский_Арсенал.xls",
    "xls/054_Пождело_оборудование.xls",
    "xlsx/005_gefest_orositeli.xlsx",
    "xlsx/043_Гефест.xlsx",
    "xlsx/047_Комплектоборудование_KZ.xlsx",
    "xlsx/054_МЧС01_ДВ.xlsx",
    "xlsx/058_ПироХимУрал.xlsx",
]


# Rows that the former header-only extractor omitted even though manual audit
# confirmed a product/service identity and a price in the same physical row.
# Coordinates are 1-based Excel rows; the production extractor contains no
# supplier/file-specific branches.
PREVIOUSLY_MISSED_PRICED_ROWS = {
    "xls/004_Global_Security_KZ.xls": {"Услуги": {4, 88}},
    "xls/015_UNFIRE01.xls": {"Головки, стволы": {60, 61}},
    "xls/032_Интернет-магазин_пожарного_обо.xls": {
        "Шкафы,Щиты,Инстр.": {75},
        " Стенды 1": set(range(4, 71)) - {18, 42, 53},
        "Стенды 2": set(range(4, 64)) - {10, 13, 21, 36, 50},
        "Плакаты, журналы, знаки": {
            4, 5, 6, 7, 9, 10, 11, 12, 14, 15, 17, 18, 19, 20, 21, 22,
            23, 24, 25, 26, 27, 28, 30, 31, 32, 33, 34, 35, 36, 37, 38,
            40, 41, 42, 43, 45, 46, 48, 49, 51, 52, 53, 54, 55, 56, 57,
            58, 59, 60, 62, 63, 64, 65, 66, 67, 68,
        },
    },
    "xls/054_Пождело_оборудование.xls": {
        "Шкафы": set(range(7, 27)) | set(range(29, 49)) | set(range(51, 54)) | set(range(55, 61)),
        "Подставки, Чехлы": set(range(6, 30)),
    },
    "xlsx/005_gefest_orositeli.xlsx": {
        "Оросители пов. производ.": {6, 7, 8, 9, 11, 12, 13, 14},
        "Оросители стеллажные": {5, 6, 8, 9},
    },
    "xlsx/043_Гефест.xlsx": {
        "Оросители пов. производ.": {6, 7, 8, 9, 11, 12, 13, 14},
        "Оросители пов производ с ПП": {5, 6, 7, 8},
        "Оросители стеллажные": {5, 6, 8, 9},
    },
    "xlsx/058_ПироХимУрал.xlsx": {"ПЛАНЫ ЭВАКУАЦИИ": {16}},
}

# Coordinates discovered by the full-corpus reconciliation. They cover three
# generic extractor gaps: a headerless prefix before a repeated header, a
# worksheet where a false header candidate suppressed structural fallback,
# and a legitimate one-item table.
FULL_CORPUS_PREVIOUSLY_MISSED_PRICED_ROWS = {
    "xls/069_Техресурс.xls": {"CKMT": {11}},
    "xls/074_Фабрикант.xls": {"СИЗ_3M_PELTOR": {10}},
    "xls/037_Кубометр_Zetkama.xls": {
        "Фильтры": {3},
        "Обратные клапаны": {3},
    },
    "xlsx/048_Кубометр_арматура.xlsx": {
        "Прерыватели вакуума": {3},
        "Редукционные клапаны": {3},
    },
    "xlsx/062_Пожтехавтоматика_ТО_огнетушителей.xlsx": {
        "Лист1": {4, 5},
    },
}


def test_every_manually_confirmed_priced_row_is_exposed_to_the_model() -> None:
    expectations = {
        **PREVIOUSLY_MISSED_PRICED_ROWS,
        **FULL_CORPUS_PREVIOUSLY_MISSED_PRICED_ROWS,
    }
    for relative_path, expected_by_sheet in expectations.items():
        source = CORPUS_DIR / relative_path
        data = source.read_bytes()
        loader = loaders.detect_loader(data, source.name)
        grids = loaders.load_grids_for(loader, data, source.name)
        contexts = build_tabular_section_contexts(grids, sample_rows_limit=5)
        exposed = {
            (context["sheet_name"], row_index + 1)
            for context in contexts
            for row_index in context["price_presence"]["candidate_row_indexes"]
        }
        expected = {
            (sheet_name, excel_row)
            for sheet_name, rows in expected_by_sheet.items()
            for excel_row in rows
        }

        assert expected <= exposed, f"{relative_path}: скрыты строки {sorted(expected - exposed)}"
        assert all(1 <= len(context["sample_rows"]) <= 5 for context in contexts)


def test_reviewed_horizontal_catalogues_are_unfolded_without_losing_source_rows() -> None:
    expectations = {
        "xls/054_Пождело_оборудование.xls": {
            "sheet": "Подставки, Чехлы",
            "section_kind": "ambiguous_tabular_rows",
            "logical_rows": 45,
            "required_excel_rows": set(range(6, 30)),
        },
        "xlsx/058_ПироХимУрал.xlsx": {
            "sheet": "ПОЖАРНОЕ ОБОРУДОВАНИЕ",
            "section_kind": "price_rows",
            "logical_rows": 113,
            "required_excel_rows": {45, 46},
        },
    }
    for relative_path, expected in expectations.items():
        source = CORPUS_DIR / relative_path
        data = source.read_bytes()
        loader = loaders.detect_loader(data, source.name)
        contexts = build_tabular_section_contexts(
            loaders.load_grids_for(loader, data, source.name), sample_rows_limit=5
        )
        context = next(
            item
            for item in contexts
            if item["sheet_name"] == expected["sheet"]
            and item["section_kind"] == expected["section_kind"]
            and len(item["horizontal_panels"]) == 2
            and expected["required_excel_rows"]
            <= {
                row_index + 1
                for row_index in item["price_presence"]["candidate_row_indexes"]
            }
        )

        assert context["unfolded_panel_rows_available"] == expected["logical_rows"]
        assert [row["panel_index"] for row in context["unfolded_panel_rows"]] == [
            0, 0, 0, 0, 0, 1, 1, 1, 1, 1,
        ]


def _has_value(row, index: int) -> bool:
    return index < len(row) and row[index].value is not None and str(row[index].value).strip() != ""


def _gold_item_rows(grid, gold_sheet: dict, section_start: int, section_end: int) -> list[int]:
    name_columns = [
        column["index"]
        for column in gold_sheet["columns"]
        if column.get("role") == "name"
    ]
    price_columns = [
        column["index"]
        for column in gold_sheet["columns"]
        if column.get("role") == "price"
    ]
    non_price_columns = [
        column["index"]
        for column in gold_sheet["columns"]
        if column.get("role") != "price"
    ]
    def is_multilevel_label_row(row) -> bool:
        real_names = [
            row[index].value
            for index in name_columns
            if index < len(row)
            and _has_value(row, index)
            and not row[index].is_merged_fill
        ]
        prices = [
            row[index].value
            for index in price_columns
            if index < len(row)
            and _has_value(row, index)
            and not row[index].is_merged_fill
        ]
        return (
            bool(prices)
            and all(parse_price(value).status not in {"ok", "on_request"} for value in prices)
            and (
                not real_names
                or all(
                    suggest_role(value).role == "name"
                    and suggest_role(value).confidence >= AUTO_CONFIDENCE_THRESHOLD
                    for value in real_names
                )
            )
        )

    return [
        row_index
        for row_index in range(section_start, section_end)
        if not is_multilevel_label_row(grid.rows[row_index])
        if (
            any(
                _has_value(grid.rows[row_index], index)
                and not grid.rows[row_index][index].is_merged_fill
                for index in name_columns
            )
            or any(
                _has_value(grid.rows[row_index], index)
                and not grid.rows[row_index][index].is_merged_fill
                for index in non_price_columns
            )
        )
        and any(
            index < len(grid.rows[row_index])
            and not grid.rows[row_index][index].is_merged_fill
            and grid.rows[row_index][index].value is not None
            and (
                parse_price(grid.rows[row_index][index].value).status in {"ok", "on_request"}
                or suggest_role(grid.rows[row_index][index].value).role != "price"
                or suggest_role(grid.rows[row_index][index].value).confidence
                < AUTO_CONFIDENCE_THRESHOLD
            )
            for index in price_columns
        )
    ]


def test_all_reviewed_sections_have_exact_header_and_up_to_five_real_item_rows() -> None:
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    by_path = {record["relative_path"]: record for record in audit["price_files"]}
    checked_files: set[str] = set()
    checked_sections = 0

    for relative_path in BENCHMARK_FILES:
        record = by_path[relative_path]
        source = CORPUS_DIR / relative_path
        data = source.read_bytes()
        loader = loaders.detect_loader(data, source.name)
        grids = loaders.load_grids_for(loader, data, source.name)
        contexts = build_tabular_section_contexts(grids, sample_rows_limit=5)
        grids_by_name = {grid.name: grid for grid in grids}
        contexts_by_key: dict[tuple[str, int], list[dict]] = {}
        for context in contexts:
            if context["section_kind"] != "price_rows":
                continue
            contexts_by_key.setdefault(
                (context["sheet_name"], context["header_row_index"]), []
            ).append(context)

        for gold_sheet in record["effective_recipe"]["sheets"]:
            if gold_sheet.get("skip"):
                continue
            sheet_name = gold_sheet["match"]["name_pattern"]
            key = (sheet_name, gold_sheet["header_row"])
            assert key in contexts_by_key, f"{relative_path}: не найдена reviewed-шапка {key}"
            for context in contexts_by_key[key]:
                expected_rows = _gold_item_rows(
                    grids_by_name[sheet_name],
                    gold_sheet,
                    context["section_start_row_index"],
                    context["section_end_row_index"],
                )
                assert expected_rows, f"{relative_path}: у reviewed-секции {key} нет товарных строк"
                assert [row["row_index"] for row in context["sample_rows"]] == expected_rows[:5], (
                    f"{relative_path}: неверные товарные строки секции {key}"
                )
                assert len(context["sample_rows"]) == min(5, len(expected_rows))
                assert context["sample_rows_available"] >= len(context["sample_rows"])
                checked_sections += 1
        checked_files.add(relative_path)

    assert len(checked_files) == 10
    assert checked_sections > 10


def test_global_security_is_split_into_real_price_chunks() -> None:
    relative_path = "xls/004_Global_Security_KZ.xls"
    source = CORPUS_DIR / relative_path
    data = source.read_bytes()
    loader = loaders.detect_loader(data, source.name)
    contexts = build_tabular_section_contexts(
        loaders.load_grids_for(loader, data, source.name), sample_rows_limit=4
    )

    services = [context for context in contexts if context["sheet_name"] == "Услуги"]
    assert [
        [row["row_index"] for row in context["category_rows"]]
        for context in services[:5]
    ] == [[1], [5], [22, 23], [37], [43, 44]]
    assert [
        [row["row_index"] for row in context["sample_rows"]]
        for context in services[:5]
    ] == [[3, 4], [6, 7, 8, 9], [24, 25, 26, 27], [38, 39, 40, 41], [45, 46, 47, 48]]

    extinguishers = [
        context for context in contexts if context["sheet_name"] == "Огнетушители"
    ]
    assert extinguishers[0]["header_row_index"] == 0
    assert [row["row_index"] for row in extinguishers[0]["category_rows"]] == [1]
    assert [row["row_index"] for row in extinguishers[0]["sample_rows"]] == [2, 3, 4, 5]
    covers = next(
        context
        for context in extinguishers
        if [row["row_index"] for row in context["sample_rows"]][:1] == [48]
    )
    assert [row["row_index"] for row in covers["category_rows"]] == [47]

    other_equipment = [
        context for context in contexts if context["sheet_name"] == "пож.оборудование"
        and context["section_kind"] == "price_rows"
    ]
    assert [row["row_index"] for row in other_equipment[0]["category_rows"]] == [42]

    assert all("ИИК KZ44722S000020194838" not in str(context) for context in contexts)


def test_global_security_catalog_rows_without_prices_are_visible_to_the_model() -> None:
    source = CORPUS_DIR / "xls/004_Global_Security_KZ.xls"
    data = source.read_bytes()
    grids = loaders.load_grids_for("xls", data, source.name)
    contexts = build_tabular_section_contexts(grids, sample_rows_limit=5)
    all_diagnostics = [
        context
        for context in contexts
        if context["section_kind"] == "name_rows_without_price"
    ]
    diagnostics = [
        context
        for context in all_diagnostics
        if context["sheet_name"] == "пож.оборудование"
    ]
    assert len(all_diagnostics) == 13
    assert sum(context["sample_rows_available"] for context in all_diagnostics) == 60
    diagnostic_rows = {
        (context["sheet_name"], row_index)
        for context in all_diagnostics
        for row_index in context["price_presence"]["candidate_row_indexes"]
    }
    expected_empty_price_rows = {
        ("Огнетушители", 42),
        ("Огнетушители", 57),
        ("Огнетушители", 96),
        ("Пожарная арматура", 4),
        ("Пожарная арматура", 5),
        ("Пожарная арматура", 9),
        ("Пожарная арматура", 10),
        ("Пожарная арматура", 51),
        ("Пожарная арматура", 52),
        ("Пожарная арматура", 59),
        ("Пожарная арматура", 60),
        ("пож.оборудование", 45),
        ("пож.оборудование", 49),
        ("пож.оборудование", 50),
        ("пож.оборудование", 69),
        ("пож.оборудование", 76),
    }
    assert expected_empty_price_rows <= diagnostic_rows

    first_aid = next(
        context
        for context in diagnostics
        if context["sample_rows"][0]["row_index"] == 2
    )
    assert first_aid["sample_rows_available"] == 40
    assert [row["row_index"] for row in first_aid["sample_rows"]] == [2, 3, 4, 5, 6]
    assert first_aid["price_presence"]["rows_with_price"] == 0
    assert first_aid["price_presence"]["rows_without_price"] == 40
    assert first_aid["price_presence"]["all_source_columns_inspected"] is True
    assert first_aid["price_presence"]["inspected_column_indexes"] == [0, 1, 2, 3, 4]
    assert first_aid["price_presence"]["value_counts_by_column"] == [
        {"index": 0, "nonempty_rows": 40},
        {"index": 1, "nonempty_rows": 0},
        {"index": 2, "nonempty_rows": 0},
        {"index": 3, "nonempty_rows": 0},
        {"index": 4, "nonempty_rows": 0},
    ]

    self_rescuer = next(
        context
        for context in diagnostics
        if any(
            "Самоспасатель ГДЗК КНР" in str(cell.get("value") or "")
            for row in context["sample_rows"]
            for cell in row["cells"]
        )
    )
    assert [row["row_index"] for row in self_rescuer["category_rows"]] == [58]
    assert [row["row_index"] for row in self_rescuer["sample_rows"]] == [59]
    assert self_rescuer["price_presence"]["rows_with_price"] == 0
