"""Pure tests for semantic price chunks sent to the rule model."""
from __future__ import annotations

from copy import deepcopy

from app.parsing.grid import Cell, SheetGrid
from app.parsing.rule_proposal import (
    ProposalError,
    build_tabular_rule_generation_prompt,
    build_tabular_section_contexts,
    compile_batch_proposals,
    compile_section_proposal,
    dry_run_decision,
    parse_proposal,
)
from app.parsing.mapping import ColumnMapping, MappingDecision, TableMapping
from app.parsing.pipeline import parse_grids
from app.parsing.rules import recipe_from_decision, recipe_to_decision


def _bold(value: object, *, merged_fill: bool = False) -> Cell:
    cell = Cell(value, is_merged_fill=merged_fill)
    # Workbook loaders preserve this formatting signal. Setting it dynamically
    # lets the RED test describe the desired contract before Cell grows it.
    cell.is_bold = True
    return cell


def _semantic_chunks_grid() -> SheetGrid:
    return SheetGrid(
        name="Услуги",
        rows=[
            [_bold("Карточка поставщика: адрес, ИИК, телефон"), _bold("Карточка поставщика", merged_fill=True), Cell(None)],
            [_bold("Перезарядка огнетушителей"), _bold("Перезарядка огнетушителей", merged_fill=True), Cell(None)],
            [_bold("Наименование товара/услуги"), _bold("Наименование товара/услуги", merged_fill=True), _bold("Цена, тенге")],
            [Cell("Выезд машины"), Cell("Выезд машины", is_merged_fill=True), Cell("50 тенге за 1 км")],
            [Cell("Переосвидетельствование"), Cell("Переосвидетельствование", is_merged_fill=True), Cell("300")],
            [_bold("Частичная перезарядка"), _bold("Частичная перезарядка", merged_fill=True), _bold("Розница")],
            [Cell("Огнетушитель ОП-1"), Cell("Огнетушитель ОП-1", is_merged_fill=True), Cell("380")],
            [Cell("Огнетушитель ОП-2"), Cell("Огнетушитель ОП-2", is_merged_fill=True), Cell("480")],
            [_bold("Полная перезарядка"), _bold("Полная перезарядка", merged_fill=True), _bold("Полная перезарядка", merged_fill=True)],
            [_bold("Углекислотные огнетушители"), _bold("Углекислотные огнетушители", merged_fill=True), _bold("Углекислотные огнетушители", merged_fill=True)],
            [Cell("Огнетушитель ОУ-1"), Cell("Огнетушитель ОУ-1", is_merged_fill=True), Cell("942")],
        ],
    )


def _implicit_name_header_grid() -> SheetGrid:
    return SheetGrid(
        name="Огнетушители",
        rows=[
            [_bold("ОГНЕТУШИТЕЛИ, КРОНШТЕЙНЫ"), Cell(None), Cell(None), _bold("розница"), _bold("дилер")],
            [_bold("Порошковые ABCE"), _bold("Порошковые ABCE", merged_fill=True), _bold("Порошковые ABCE", merged_fill=True), _bold("Порошковые ABCE", merged_fill=True), _bold("Порошковые ABCE", merged_fill=True)],
            [Cell("Огнетушитель ОП-2"), Cell(None), Cell("110*380"), Cell("22810"), Cell("2726")],
            [Cell("Огнетушитель ОП-3"), Cell(None), Cell("110*470"), Cell("4461"), Cell("4130")],
        ],
    )


def _catalog_without_price_grid() -> SheetGrid:
    return SheetGrid(
        name="пож.оборудование",
        rows=[
            [_bold("Наименование товара"), Cell(None), _bold("розница"), _bold("дилер")],
            [_bold("Аптечки и наборы"), Cell(None), Cell(None), Cell(None)],
            [Cell("Аптечка первой помощи"), Cell("комплект 1"), Cell(None), Cell(None)],
            [Cell("Набор травматологический"), Cell("комплект 2"), Cell(None), Cell(None)],
            [_bold("Фонари"), Cell(None), Cell(None), Cell(None)],
            [Cell("Фонарь пожарный"), Cell("ФП-1"), Cell("1200"), Cell("1100")],
        ],
    )


def _catalog_without_price_proposal(context: dict) -> dict:
    price_column = context["price_presence"]["price_column_indexes"][0]
    item_row = context["sample_rows"][0]["row_index"]
    return {
        "schema_version": "price-rule-proposal-v1",
        "decision": "catalog_without_price",
        "rule_name": "Каталог без цены",
        "loader": "xls",
        "source_section_id": context["section_id"],
        "mapping": None,
        "discovered_attributes": [],
        "evidence": [
            {
                "row_index": context["header_row_index"],
                "column_index": price_column,
                "note": "Колонка розничной цены пуста в строках товаров",
            },
            {
                "row_index": item_row,
                "column_index": 0,
                "note": "Есть наименование товара, но цены нет",
            },
        ],
        "unresolved": ["Товар присутствует в каталоге поставщика без цены"],
        "confidence": 0.99,
        "requires_manual_review": True,
    }


def _multi_level_header_grid() -> SheetGrid:
    return SheetGrid(
        name="Многоуровневая шапка",
        rows=[
            [_bold("Раздел"), _bold("Раздел", merged_fill=True), _bold("Раздел", merged_fill=True), _bold("Раздел", merged_fill=True)],
            [Cell("Наименование"), Cell("Маркировка"), Cell("Цена, руб."), Cell("Цена, руб.", is_merged_fill=True)],
            [Cell("Наименование", is_merged_fill=True), Cell("Маркировка", is_merged_fill=True), Cell("Обычный"), Cell("Декоративный")],
            [Cell("Наименование", is_merged_fill=True), Cell("Маркировка", is_merged_fill=True), Cell("до 100 шт."), Cell("от 101 шт.")],
            [_bold("Подраздел"), _bold("Подраздел", merged_fill=True), _bold("Подраздел", merged_fill=True), _bold("Подраздел", merged_fill=True)],
            [Cell("Товар"), Cell("ABC-1"), Cell("1200"), Cell("по запросу")],
        ],
    )


def test_context_splits_semantic_price_chunks_and_excludes_supplier_card() -> None:
    contexts = build_tabular_section_contexts([_semantic_chunks_grid()])

    assert [context["header_row_index"] for context in contexts] == [2, 2, 2]
    assert [[row["row_index"] for row in context["category_rows"]] for context in contexts] == [
        [1],
        [5],
        [8, 9],
    ]
    assert [[row["row_index"] for row in context["sample_rows"]] for context in contexts] == [
        [3, 4],
        [6, 7],
        [10],
    ]
    assert contexts[0]["category_rows"][0]["cells"][0]["is_bold"] is True
    assert {
        observation["source_header_row_index"]
        for observation in contexts[0]["deterministic_observations"]
    } == {2}
    assert all("Карточка поставщика" not in str(context) for context in contexts)


def test_context_treats_multilevel_labels_as_header_and_samples_real_items() -> None:
    contexts = build_tabular_section_contexts([_multi_level_header_grid()])

    assert len(contexts) == 1
    context = contexts[0]
    assert context["column_header_row_indexes"] == [1, 2, 3]
    assert [row["row_index"] for row in context["sample_rows"]] == [5]
    assert [row["row_index"] for row in context["category_rows"]] == [4]
    assert "Обычный" in context["header_values"][2]
    assert "до 100 шт." in context["header_values"][2]


def test_context_exposes_headerless_repeated_price_table_as_proven_shape() -> None:
    grid = SheetGrid(
        name="Стенды",
        rows=[
            [_bold("Прайс-лист"), Cell(None), Cell(None), Cell(None)],
            [_bold("СТЕНДЫ ПО ПОЖАРНОЙ БЕЗОПАСНОСТИ"), Cell(None), Cell(None), Cell(None)],
            [Cell('Стенд "Пожарная безопасность"'), Cell(None), Cell("шт"), Cell(2300)],
            [Cell('Стенд "Охрана труда"'), Cell(None), Cell("шт"), Cell(2800)],
            [Cell('Стенд "Первая помощь"'), Cell(None), Cell("шт"), Cell(1500)],
        ],
    )

    contexts = build_tabular_section_contexts([grid])

    assert len(contexts) == 1
    context = contexts[0]
    assert context["section_kind"] == "headerless_price_rows"
    assert context["price_presence"]["candidate_row_indexes"] == [2, 3, 4]
    assert context["structural_candidates"] == {
        "semantic_roles_confirmed": False,
        "candidate_name_column_indexes": [0],
        "candidate_numeric_column_indexes": [3],
    }


def test_context_keeps_two_headerless_horizontal_price_panels_separate() -> None:
    grid = SheetGrid(
        name="Подставки и чехлы",
        rows=[
            [_bold("Подставки"), Cell(None), Cell(None), Cell(None), _bold("Чехлы"), Cell(None), Cell(None)],
            [Cell("П-10"), Cell(262), Cell(276), Cell(None), Cell("Чехол ОУ-1"), Cell(180), Cell(189)],
            [Cell("П-15"), Cell(292), Cell(307), Cell(None), Cell("Чехол ОУ-2"), Cell(198), Cell(208)],
        ],
    )

    context = build_tabular_section_contexts([grid])[0]

    assert context["section_kind"] == "ambiguous_tabular_rows"
    assert context["horizontal_panels"] == [
        {
            "start_column_index": 0,
            "end_column_index": 4,
            "name_columns": [0],
            "price_columns": [1, 2],
        },
        {
            "start_column_index": 4,
            "end_column_index": 7,
            "name_columns": [4],
            "price_columns": [5, 6],
        },
    ]
    assert [
        (row["panel_index"], row["row_index"])
        for row in context["unfolded_panel_rows"]
    ] == [(0, 1), (0, 2), (1, 1), (1, 2)]
    assert context["unfolded_panel_rows_available"] == 4


def test_headerless_panel_detector_does_not_choose_unit_as_product_name() -> None:
    grid = SheetGrid(
        name="Две панели с единицами",
        rows=[
            [_bold("Левая"), Cell(None), Cell(None), Cell(None), _bold("Правая"), Cell(None), Cell(None)],
            [Cell("Огнетушитель ОУ-1"), Cell("шт"), Cell(1273), Cell(None), Cell("Огнетушитель ОП-1"), Cell("шт"), Cell(391)],
            [Cell("Огнетушитель ОУ-2"), Cell("шт."), Cell(1597), Cell(None), Cell("Огнетушитель ОП-2"), Cell("шт."), Cell(514)],
        ],
    )

    context = build_tabular_section_contexts([grid])[0]

    assert context["horizontal_panels"] == [
        {
            "start_column_index": 0,
            "end_column_index": 4,
            "name_columns": [0],
            "price_columns": [2],
        },
        {
            "start_column_index": 4,
            "end_column_index": 7,
            "name_columns": [4],
            "price_columns": [6],
        },
    ]


def test_headerless_product_list_is_split_from_following_technical_matrix() -> None:
    grid = SheetGrid(
        name="Контроллеры",
        rows=[
            [_bold("Контроллер"), Cell(None), Cell(None), Cell(None), Cell(None)],
            [Cell("Pixel-1"), Cell(None), Cell(None), Cell("AIN=5\nDIN=6"), Cell(152.86)],
            [Cell("Pixel-2"), Cell(None), Cell(None), Cell("AIN=3\nDIN=6"), Cell(165.00)],
            [_bold("Технические характеристики"), Cell(None), Cell(None), Cell(None), Cell(None)],
            [Cell("Аналоговые входы"), Cell(None), Cell(None), Cell(5), Cell(3)],
            [Cell("Дискретные входы"), Cell(None), Cell(None), Cell(6), Cell(6)],
        ],
    )

    contexts = build_tabular_section_contexts([grid])

    assert len(contexts) == 2
    assert [context["price_presence"]["candidate_row_indexes"] for context in contexts] == [
        [1, 2],
        [4, 5],
    ]
    assert contexts[0]["structural_candidates"]["candidate_name_column_indexes"] == [0]
    assert contexts[0]["structural_candidates"]["candidate_numeric_column_indexes"] == [4]


def test_ambiguous_panel_context_anchors_mapping_to_nearest_header_row() -> None:
    grid = SheetGrid(
        name="Многоуровневая шапка",
        rows=[
            [_bold("Прайс"), Cell(None), Cell(None), Cell(None), Cell(None)],
            [_bold("Каталог"), Cell(None), Cell(None), Cell(None), Cell(None)],
            [_bold("Левая панель"), Cell(None), Cell(None), _bold("Правая панель"), Cell(None)],
            [Cell("Л-1"), Cell(100), Cell(None), Cell("П-1"), Cell(200)],
            [Cell("Л-2"), Cell(110), Cell(None), Cell("П-2"), Cell(210)],
        ],
    )

    context = build_tabular_section_contexts([grid])[0]

    assert context["section_kind"] == "ambiguous_tabular_rows"
    assert context["column_header_row_indexes"] == [0, 1, 2]
    assert context["header_row_index"] == 2


def test_context_treats_price_contractual_as_on_request_value_not_header() -> None:
    grid = SheetGrid(
        name="Стволы",
        rows=[
            [Cell("Наименование"), Cell("Цена")],
            [Cell("Ствол ЛС-С60"), Cell("Цена договорная")],
            [Cell("Ствол ЛС-С40"), Cell("Цена договорная")],
        ],
    )

    context = build_tabular_section_contexts([grid])[0]

    assert context["section_kind"] == "price_rows"
    assert context["price_presence"]["candidate_row_indexes"] == [1, 2]


def test_compiler_accepts_model_proven_price_mapping_for_ambiguous_table() -> None:
    grid = SheetGrid(
        name="Стенды",
        rows=[
            [_bold("СТЕНДЫ"), Cell(None), Cell(None), Cell(None)],
            [Cell("Стенд А"), Cell(None), Cell("шт"), Cell(2300)],
            [Cell("Стенд Б"), Cell(None), Cell("шт"), Cell(2800)],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    proposal = {
        "schema_version": "price-rule-proposal-v1",
        "decision": "propose",
        "rule_name": "Структурно подтверждённый прайс",
        "loader": "xlsx",
        "source_section_id": context["section_id"],
        "mapping": {
            "sheet_name": grid.name,
            "header_row_index": context["header_row_index"],
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
                    "index": 2,
                    "role": "unit",
                    "price_descriptor": None,
                    "attribute_code": None,
                    "attribute_value_kind": None,
                    "canonical_unit": None,
                },
                {
                    "index": 3,
                    "role": "price",
                    "price_descriptor": {"kind": "retail", "vat_mode": "unknown"},
                    "attribute_code": None,
                    "attribute_value_kind": None,
                    "canonical_unit": None,
                },
            ],
        },
        "discovered_attributes": [],
        "evidence": [
            {"row_index": context["header_row_index"], "column_index": index, "note": "source column"}
            for index in (0, 2, 3)
        ],
        "unresolved": [],
        "confidence": 0.95,
        "requires_manual_review": True,
    }

    compiled = compile_section_proposal(
        parse_proposal(proposal), source_context=context, loader="xlsx", grids=[grid]
    )
    result, validation = dry_run_decision(
        [grid], MappingDecision(tables=[compiled.table], auto_ok=True)
    )

    assert validation["passed"] is True
    assert [item.source_name for item in result.items] == ["Стенд А", "Стенд Б"]


def test_rule_can_use_explicit_same_row_name_fallback() -> None:
    grid = SheetGrid(
        name="Варианты",
        rows=[
            [Cell("Наименование"), Cell("Маркировка"), Cell("Цена")],
            [Cell("Основной вариант"), Cell("A-1"), Cell("100")],
            [Cell(None), Cell("A-2"), Cell("120")],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    proposal = {
        "schema_version": "price-rule-proposal-v1",
        "decision": "propose",
        "rule_name": "Варианты с кодом вместо повторённого названия",
        "loader": "xlsx",
        "source_section_id": context["section_id"],
        "mapping": {
            "sheet_name": context["sheet_name"],
            "header_row_index": context["header_row_index"],
            "name_fallback_column_indexes": [1],
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
                    "role": "article",
                    "price_descriptor": None,
                    "attribute_code": None,
                    "attribute_value_kind": None,
                    "canonical_unit": None,
                },
                {
                    "index": 2,
                    "role": "price",
                    "price_descriptor": {"kind": "other", "vat_mode": "unknown"},
                    "attribute_code": None,
                    "attribute_value_kind": None,
                    "canonical_unit": None,
                },
            ],
        },
        "discovered_attributes": [],
        "evidence": [
            {"row_index": 0, "column_index": index, "note": "Шапка"}
            for index in (0, 1, 2)
        ],
        "unresolved": [],
        "confidence": 0.99,
        "requires_manual_review": True,
    }

    compiled = compile_section_proposal(
        parse_proposal(proposal), source_context=context, loader="xlsx", grids=[grid]
    )
    result, validation = dry_run_decision(
        [grid], MappingDecision(tables=[compiled.table], auto_ok=True)
    )

    assert validation["passed"] is True
    assert [item.source_name for item in result.items] == ["Основной вариант", "A-2"]
    assert any(issue.code == "NAME_FROM_EXPLICIT_FALLBACK" for issue in result.issues)


def test_orphan_zero_price_placeholder_is_skipped_without_quality_error() -> None:
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [Cell("Наименование"), Cell("Цена")],
            [Cell("Товар"), Cell("100")],
            [Cell(None), Cell("0")],
        ],
    )
    result = parse_grids(
        [grid],
        MappingDecision(
            auto_ok=True,
            tables=[
                TableMapping(
                    sheet_name=grid.name,
                    header_row_index=0,
                    columns=[
                        ColumnMapping(index=0, role="name"),
                        ColumnMapping(index=1, role="price"),
                    ],
                )
            ],
        ),
    )

    assert [item.source_name for item in result.items] == ["Товар"]
    assert result.errors_count == 0
    assert any(issue.code == "ORPHAN_ZERO_PRICE_PLACEHOLDER" for issue in result.issues)


def test_context_infers_name_column_from_page_title_and_keeps_category() -> None:
    context = build_tabular_section_contexts([_implicit_name_header_grid()])[0]

    assert context["header_row_index"] == 0
    assert context["column_header_row_indexes"] == [0]
    assert [row["row_index"] for row in context["category_rows"]] == [1]
    assert [row["row_index"] for row in context["sample_rows"]] == [2, 3]
    assert [
        observation["index"]
        for observation in context["deterministic_observations"]
        if observation["suggested_role"] == "name"
    ] == [0]
    assert [
        observation["index"]
        for observation in context["deterministic_observations"]
        if observation["suggested_role"] == "price"
    ] == [3, 4]


def test_context_emits_catalog_without_price_audit_without_changing_price_chunk() -> None:
    contexts = build_tabular_section_contexts([_catalog_without_price_grid()])

    price_contexts = [context for context in contexts if context["section_kind"] == "price_rows"]
    no_price_contexts = [
        context for context in contexts
        if context["section_kind"] == "name_rows_without_price"
    ]
    assert len(price_contexts) == 1
    assert [row["row_index"] for row in price_contexts[0]["sample_rows"]] == [5]
    assert len(no_price_contexts) == 1
    no_price = no_price_contexts[0]
    assert [row["row_index"] for row in no_price["category_rows"]] == [1]
    assert [row["row_index"] for row in no_price["sample_rows"]] == [2, 3]
    assert no_price["price_presence"] == {
        "all_source_columns_inspected": True,
        "inspected_column_indexes": [0, 1, 2, 3],
        "price_column_indexes": [2, 3],
        "candidate_row_indexes": [2, 3],
        "value_counts_by_column": [
            {"index": 0, "nonempty_rows": 2},
            {"index": 1, "nonempty_rows": 2},
            {"index": 2, "nonempty_rows": 0},
            {"index": 3, "nonempty_rows": 0},
        ],
        "candidate_name_rows": 2,
        "rows_with_price": 0,
        "rows_without_price": 2,
    }
    prompt = build_tabular_rule_generation_prompt(
        source_context=no_price,
        loader="xls",
        model="qwen3-235b-a22b-fp8/latest",
    )
    assert 'decision="catalog_without_price"' in prompt
    assert '"all_source_columns_inspected": true' in prompt
    assert "Не подменяй цену весом" in prompt


def test_catalog_without_price_is_source_checked_and_does_not_block_batch() -> None:
    grid = _catalog_without_price_grid()
    contexts = build_tabular_section_contexts([grid])
    price_context = next(context for context in contexts if context["section_kind"] == "price_rows")
    no_price_context = next(
        context for context in contexts
        if context["section_kind"] == "name_rows_without_price"
    )
    assert no_price_context["section_start_row_index"] == min(
        no_price_context["price_presence"]["candidate_row_indexes"]
    )

    compiled = compile_section_proposal(
        parse_proposal(_catalog_without_price_proposal(no_price_context)),
        source_context=no_price_context,
        loader="xls",
        grids=[grid],
    )
    assert compiled.table is None
    assert compiled.validation["passed"] is True
    assert compiled.validation["reason_code"] == "catalog_without_price"
    assert compiled.validation["catalog_item_rows"] == 2

    item_only_evidence = _catalog_without_price_proposal(no_price_context)
    item_only_evidence["evidence"] = item_only_evidence["evidence"][1:]
    compiler_completed = compile_section_proposal(
        parse_proposal(item_only_evidence),
        source_context=no_price_context,
        loader="xls",
        grids=[grid],
    )
    assert compiler_completed.validation["passed"] is True
    assert "дополнено source compiler" in " ".join(
        compiler_completed.validation["normalisation_warnings"]
    )

    overlapping_bounds = deepcopy(no_price_context)
    overlapping_bounds["section_end_row_index"] = len(grid.rows)
    still_safe = compile_section_proposal(
        parse_proposal(_catalog_without_price_proposal(overlapping_bounds)),
        source_context=overlapping_bounds,
        loader="xls",
        grids=[grid],
    )
    assert still_safe.validation["passed"] is True
    price_proposal = {
        "schema_version": "price-rule-proposal-v1",
        "decision": "propose",
        "rule_name": "Фонари",
        "loader": "xls",
        "source_section_id": price_context["section_id"],
        "mapping": {
            "sheet_name": price_context["sheet_name"],
            "header_row_index": price_context["header_row_index"],
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
                    "index": 2,
                    "role": "price",
                    "price_descriptor": {"kind": "retail", "vat_mode": "unknown"},
                    "attribute_code": None,
                    "attribute_value_kind": None,
                    "canonical_unit": None,
                },
                {
                    "index": 3,
                    "role": "price",
                    "price_descriptor": {"kind": "dealer", "vat_mode": "unknown"},
                    "attribute_code": None,
                    "attribute_value_kind": None,
                    "canonical_unit": None,
                },
            ],
        },
        "discovered_attributes": [],
        "evidence": [
            {"row_index": 0, "column_index": index, "note": "Шапка"}
            for index in (0, 2, 3)
        ],
        "unresolved": [],
        "confidence": 0.99,
        "requires_manual_review": True,
    }
    decision = compile_batch_proposals(
        [
            (price_proposal, price_context),
            (_catalog_without_price_proposal(no_price_context), no_price_context),
        ],
        loader="xls",
        grids=[grid],
    )
    assert len(decision.tables) == 1


def test_ambiguous_technical_table_can_be_verified_as_catalog_without_price() -> None:
    grid = SheetGrid(
        name="Арматура",
        rows=[
            [_bold("Шаровые краны"), _bold("DN"), _bold("Материал")],
            [Cell("V565-015"), Cell(15), Cell("латунь")],
            [Cell("V565-020"), Cell(20), Cell("латунь")],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    assert context["section_kind"] == "ambiguous_tabular_rows"
    item_row = context["sample_rows"][0]["row_index"]
    proposal = {
        "schema_version": "price-rule-proposal-v1",
        "decision": "catalog_without_price",
        "rule_name": "Технический каталог без цены",
        "loader": "xls",
        "source_section_id": context["section_id"],
        "mapping": None,
        "discovered_attributes": [],
        "evidence": [
            {
                "row_index": item_row,
                "column_index": 0,
                "note": "Код товара; DN является характеристикой, цена отсутствует",
            }
        ],
        "unresolved": ["Числовая колонка DN является технической характеристикой"],
        "confidence": 0.98,
        "requires_manual_review": True,
    }

    compiled = compile_section_proposal(
        parse_proposal(proposal), source_context=context, loader="xls", grids=[grid]
    )

    assert compiled.table is None
    assert compiled.validation["passed"] is True
    assert compiled.validation["reason_code"] == "catalog_without_price"
    assert "технические" in " ".join(compiled.validation["normalisation_warnings"])


def test_catalog_without_price_cannot_hide_a_row_that_has_a_price() -> None:
    grid = _catalog_without_price_grid()
    price_context = next(
        context
        for context in build_tabular_section_contexts([grid])
        if context["section_kind"] == "price_rows"
    )
    forged = _catalog_without_price_proposal(price_context)

    try:
        compile_section_proposal(
            parse_proposal(forged),
            source_context=price_context,
            loader="xls",
            grids=[grid],
        )
    except ProposalError as exc:
        assert "не помечен" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Секция с ценой не должна быть пропущена")


def test_catalog_without_price_cannot_hide_an_on_request_price() -> None:
    grid = _catalog_without_price_grid()
    no_price_context = next(
        context
        for context in build_tabular_section_contexts([grid])
        if context["section_kind"] == "name_rows_without_price"
    )
    item_row = no_price_context["price_presence"]["candidate_row_indexes"][0]
    # The supplier put the contractual price into a shifted/non-price column.
    grid.rows[item_row][1] = Cell("Цена договорная")

    try:
        compile_section_proposal(
            parse_proposal(_catalog_without_price_proposal(no_price_context)),
            source_context=no_price_context,
            loader="xls",
            grids=[grid],
        )
    except ProposalError as exc:
        assert "цена по запросу" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Цена по запросу не должна считаться отсутствующей")


def test_non_price_card_sheet_is_not_sent_to_the_rule_model() -> None:
    card = SheetGrid(
        name="Карточка поставщика",
        rows=[
            [_bold("ООО Поставщик"), Cell(None)],
            [Cell("Адрес"), Cell("Телефон")],
            [Cell("Алматы"), Cell("+7 777 000-00-00")],
        ],
    )

    assert build_tabular_section_contexts([card]) == []


def test_merged_name_continuation_column_recovers_shifted_item_name() -> None:
    grid = SheetGrid(
        name="Услуги",
        rows=[
            [_bold("Наименование"), _bold("Наименование", merged_fill=True), _bold("Цена")],
            [Cell("Обычная строка"), Cell("Обычная строка", is_merged_fill=True), Cell("100")],
            [Cell(None), Cell("ПОС1-532 тг за кг"), Cell("475")],
        ],
    )
    decision = MappingDecision(
        auto_ok=True,
        tables=[
            TableMapping(
                sheet_name="Услуги",
                header_row_index=0,
                columns=[
                    ColumnMapping(index=0, role="name"),
                    ColumnMapping(index=1, role="ignore"),
                    ColumnMapping(index=2, role="price"),
                ],
            )
        ],
    )

    result = parse_grids([grid], decision)

    assert [item.source_name for item in result.items] == [
        "Обычная строка",
        "ПОС1-532 тг за кг",
    ]


def test_compiled_semantic_chunks_keep_non_overlapping_source_boundaries() -> None:
    grid = _semantic_chunks_grid()
    contexts = build_tabular_section_contexts([grid])

    proposals = []
    for context in contexts:
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
                "index": 2,
                "role": "price",
                "price_descriptor": {"kind": "other", "vat_mode": "unknown"},
                "attribute_code": None,
                "attribute_value_kind": None,
                "canonical_unit": None,
            },
        ]
        proposals.append(
            (
                {
                    "schema_version": "price-rule-proposal-v1",
                    "decision": "propose",
                    "rule_name": context["section_id"],
                    "loader": "xls",
                    "source_section_id": context["section_id"],
                    "mapping": {
                        "sheet_name": context["sheet_name"],
                        "header_row_index": context["header_row_index"],
                        "columns": columns,
                    },
                    "discovered_attributes": [],
                    "evidence": [
                        {"row_index": context["header_row_index"], "column_index": index, "note": "шапка"}
                        for index in (0, 2)
                    ],
                    "unresolved": [],
                    "confidence": 0.95,
                    "requires_manual_review": True,
                },
                context,
            )
        )

    decision = compile_batch_proposals(proposals, loader="xls", grids=[grid])
    result, _ = dry_run_decision([grid], decision)

    assert len(decision.tables) == 1
    assert decision.tables[0].dynamic_body is True
    assert decision.tables[0].locate_header_by_signature is True
    assert (decision.tables[0].start_row_index, decision.tables[0].end_row_index) == (
        None,
        None,
    )
    assert [item.source_name for item in result.items] == [
        "Выезд машины",
        "Переосвидетельствование",
        "Огнетушитель ОП-1",
        "Огнетушитель ОП-2",
        "Огнетушитель ОУ-1",
    ]

    recipe = recipe_from_decision(decision, [grid])
    restored = recipe_to_decision(recipe, [grid])
    assert len(restored.tables) == 1
    assert restored.tables[0].dynamic_body is True
    assert restored.tables[0].header_row_index == 2


def _semantic_category_validation_grid(*captions: str) -> SheetGrid:
    rows = [
        [Cell("Наименование"), Cell("Ед."), Cell("Цена")],
    ]
    rows.extend(
        [
            _bold(caption),
            _bold(caption, merged_fill=True),
            _bold(caption, merged_fill=True),
        ]
        for caption in captions
    )
    rows.append([Cell("Контрольный товар"), Cell("шт"), Cell("100")])
    return SheetGrid(name="Семантика категорий", rows=rows)


def _parse_semantic_category_validation_grid(grid: SheetGrid):
    return parse_grids(
        [grid],
        MappingDecision(
            auto_ok=True,
            tables=[
                TableMapping(
                    sheet_name=grid.name,
                    header_row_index=0,
                    start_row_index=1,
                    columns=[
                        ColumnMapping(index=0, role="name"),
                        ColumnMapping(index=1, role="unit"),
                        ColumnMapping(index=2, role="price"),
                    ],
                )
            ],
        ),
    )


def test_semantic_category_validation_keeps_heading_before_note() -> None:
    grid = _semantic_category_validation_grid(
        "Колонки водоразборные",
        "Для разбора питьевой воды. Указана фактическая длина всей колонки.",
    )

    context = build_tabular_section_contexts([grid])[0]
    result = _parse_semantic_category_validation_grid(grid)

    assert context["category_row_indexes"] == [1]
    assert context["price_presence"]["candidate_row_indexes"] == [3]
    assert result.items[0].category_path == "Колонки водоразборные"


def test_semantic_category_validation_does_not_replace_heading_with_metadata() -> None:
    grid = _semantic_category_validation_grid(
        "Гидранты пожарные стальные Пр-во РФ",
        "условный диаметр проходной трубы 100 мм по ГОСТ Р 53961-2010",
    )

    context = build_tabular_section_contexts([grid])[0]
    result = _parse_semantic_category_validation_grid(grid)

    assert context["category_row_indexes"] == [1]
    assert context["price_presence"]["candidate_row_indexes"] == [3]
    assert result.items[0].category_path == "Гидранты пожарные стальные Пр-во РФ"


def test_semantic_category_validation_keeps_fire_cabinet_product_class() -> None:
    grid = _semantic_category_validation_grid(
        "Шкафы УНИВЕРСАЛЬНЫЕ металлические для пожарного крана. "
        "СЕРТИФИКАТ ТР ЕАЭС 043/17",
        "Шкафы оборудованы почтовым замком с карманом для хранения ключей "
        "(ЕВРОручка по запросу)",
    )

    context = build_tabular_section_contexts([grid])[0]
    result = _parse_semantic_category_validation_grid(grid)

    assert context["category_row_indexes"] == [1]
    assert context["price_presence"]["candidate_row_indexes"] == [3]
    assert result.items[0].category_path == (
        "Шкафы УНИВЕРСАЛЬНЫЕ металлические для пожарного крана. "
        "СЕРТИФИКАТ ТР ЕАЭС 043/17"
    )


def test_semantic_category_validation_collapses_multiline_product_card() -> None:
    grid = _semantic_category_validation_grid(
        "Рукава пожарные напорные",
        "С внутренним гидроизоляционным покрытием из полиуретана -ТПУ",
        "без наружного защитного покрытия, на рабочее давление 1,6МПа",
        "ТУ 13.96.16-061-85787444-2022 Сертификат № ЕАЭС RU C-RU.ПБ97.В.00458/23",
        "(поставляется с муфтой для устранения течи и без муфт на навязку)",
    )

    context = build_tabular_section_contexts([grid])[0]
    result = _parse_semantic_category_validation_grid(grid)

    assert context["category_row_indexes"] == [1]
    assert context["price_presence"]["candidate_row_indexes"] == [6]
    assert result.items[0].category_path == "Рукава пожарные напорные"


def test_semantic_category_validation_keeps_heading_with_regulatory_reference() -> None:
    heading = "Рукава пожарные напорные в соответствии с ТР ЕАЭС 043/2017"
    grid = _semantic_category_validation_grid(heading)

    context = build_tabular_section_contexts([grid])[0]
    result = _parse_semantic_category_validation_grid(grid)

    assert context["category_row_indexes"] == [1]
    assert context["price_presence"]["candidate_row_indexes"] == [2]
    assert result.items[0].category_path == heading


def test_semantic_category_validation_keeps_nominal_heading_ending_in_ty() -> None:
    heading = 'Средства защиты , спасение и пожаротушения марки "Шанс"'
    grid = _semantic_category_validation_grid(heading)

    context = build_tabular_section_contexts([grid])[0]
    result = _parse_semantic_category_validation_grid(grid)

    assert context["category_row_indexes"] == [1]
    assert context["price_presence"]["candidate_row_indexes"] == [2]
    assert result.items[0].category_path == heading
