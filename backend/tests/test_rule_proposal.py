"""Tests for the safe GPT section-proposal boundary.

No test here contacts OpenAI: the contract is intentionally pure until the
network adapter is called by a Celery task.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import (
    Base,
    ORIGINAL_PROJECT_ID,
    ParseRule,
    ParseRuleProvenance,
    ParseRuleStatus,
    RuleGenerationAttempt,
    Supplier,
)
from app.parsing.grid import Cell, SheetGrid
from app.parsing.mapping import ColumnMapping, MappingDecision, TableMapping
from app.parsing.rule_proposal import (
    RULE_PROPOSAL_SYSTEM_PROMPT,
    ProposalError,
    build_tabular_rule_generation_prompt,
    build_tabular_section_contexts,
    compile_batch_proposals,
    consolidate_section_tables,
    compile_section_proposal,
    context_hash,
    dry_run_decision,
    parse_proposal,
    rule_proposal_json_schema,
    _is_price_indicated,
)
from app import rules_service
from app.rule_proposal_service import draft_rule_name, verified_current_context


def _grid() -> SheetGrid:
    return SheetGrid(
        name="Прайс",
        rows=[
            [Cell("Каталог оборудования"), Cell(None), Cell(None)],
            [Cell("Название"), Cell("Код"), Cell("Оптовая цена, руб.")],
            [Cell("Клапан"), Cell("K-1"), Cell("1200,00")],
            [Cell("Связь +7 999 123-45-67"), Cell("K-2"), Cell("1300")],
            [Cell("Наименование"), Cell("Артикул"), Cell("Цена, руб.")],
            [Cell("Муфта"), Cell("M-1"), Cell("900")],
        ],
    )


def _grid_with_description() -> SheetGrid:
    return SheetGrid(
        name="Полный прайс",
        rows=[
            [Cell("Наименование"), Cell("Артикул"), Cell("Описание"), Cell("Цена, руб.")],
            [Cell("Клапан"), Cell("K-1"), Cell("Стальной клапан"), Cell("1200")],
        ],
    )


def _contacts_grid() -> SheetGrid:
    return SheetGrid(
        name="Контакты",
        rows=[
            [Cell("Контакт"), Cell("Телефон")],
            [Cell("Отдел продаж"), Cell("+7 999 123-45-67")],
        ],
    )


def _horizontal_panels_grid() -> SheetGrid:
    return SheetGrid(
        name="Два блока",
        rows=[
            [
                Cell("Наименование"),
                Cell("Ед. изм."),
                Cell("Цена"),
                Cell(None),
                Cell("Наименование"),
                Cell("Ед. изм."),
                Cell("Цена"),
            ],
            [
                Cell("Огнетушитель"),
                Cell("шт"),
                Cell("1200"),
                Cell(None),
                Cell("Шкаф пожарный"),
                Cell("шт"),
                Cell("5400"),
            ],
        ],
    )


def _horizontal_panels_with_category_preamble_grid() -> SheetGrid:
    def merged_caption(text: str, *, start: int) -> list[Cell]:
        row = [Cell(None) for _ in range(9)]
        row[start] = Cell(text, is_bold=True)
        for index in range(start + 1, start + 4):
            row[index] = Cell(text, is_merged_fill=True, is_bold=True)
        return row

    category_row = merged_caption("Пирамиды для гидрантов", start=0)
    right_category = merged_caption("Колонки водоразборные", start=5)
    category_row[5:9] = right_category[5:9]
    note_row = merged_caption(
        "Для разбора питьевой воды. Указана фактическая длина всей колонки",
        start=5,
    )
    note_row[0:4] = [
        Cell("Пирамида ПГ-1"),
        Cell("шт"),
        Cell("1246"),
        Cell("1682"),
    ]
    return SheetGrid(
        name="Гидранты",
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
            category_row,
            note_row,
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


def _horizontal_panels_with_empty_repeated_attribute_grid() -> SheetGrid:
    return SheetGrid(
        name="Два блока с упаковкой",
        rows=[
            [
                Cell("Наименование"), Cell("Ед. изм."), Cell("Упак"), Cell("Цена"),
                Cell(None),
                Cell("Наименование"), Cell("Ед. изм."), Cell("Упак"), Cell("Цена"),
            ],
            [
                Cell("Огнетушитель"), Cell("шт"), Cell("10 шт"), Cell("1200"),
                Cell(None),
                Cell("Шкаф пожарный"), Cell("шт"), Cell(None), Cell("5400"),
            ],
        ],
    )


def _headerless_composite_grid() -> SheetGrid:
    return SheetGrid(
        name="Составной лист",
        rows=[
            [Cell("Оборудование"), *[Cell(None) for _ in range(7)]],
            [Cell("Станция зарядная"), *[Cell(None) for _ in range(5)], Cell("шт"), Cell("453718")],
            [Cell("Испытательная установка"), *[Cell(None) for _ in range(5)], Cell("шт"), Cell("112371")],
            [Cell("Стенд для сушки"), *[Cell(None) for _ in range(5)], Cell("шт"), Cell("246293")],
            [Cell("Знаки"), *[Cell(None) for _ in range(7)]],
            [Cell("Размер"), Cell("ПВХ"), Cell("ПЭТ"), Cell("Металл"), *[Cell(None) for _ in range(4)]],
            [Cell("100x100"), Cell("12.50"), Cell("14.60"), Cell("по запросу"), *[Cell(None) for _ in range(4)]],
        ],
    )


def test_requisites_block_is_not_detected_as_a_price_section() -> None:
    """ИНН, р/с и БИК парсятся как числа, но ценовой секции из них не бывает."""
    grid = SheetGrid(
        name="Счёт",
        rows=[
            [Cell("ООО «Таурус»"), Cell("ИНН"), Cell("043601607")],
            [Cell("Банк"), Cell("р/с"), Cell("40702810954400017252")],
            [Cell("БИК"), Cell("к/с"), Cell("30101810200000000607")],
        ],
    )

    contexts = build_tabular_section_contexts([grid])
    assert [
        context
        for context in contexts
        if context["price_presence"]["price_column_indexes"]
    ] == []


def test_headerless_price_shape_is_explicit_model_evidence() -> None:
    contexts = build_tabular_section_contexts([_headerless_composite_grid()])

    linear = [
        context for context in contexts
        if context.get("section_kind") == "headerless_price_rows"
    ]
    assert linear
    assert linear[0]["headerless_mapping_hint"] == {
        "name_column_index": 0,
        "unit_column_index": 6,
        "price_column_indexes": [7],
    }
    assert _is_price_indicated(linear[0]) is True


def test_consolidates_category_chunks_into_one_dynamic_template() -> None:
    grid = SheetGrid(
        name="Шкафы",
        rows=[
            [Cell("Наименование"), Cell("Тип шкафа"), Cell("Цена")],
            [Cell("Компакт"), Cell(None), Cell(None)],
            [Cell("ШПК-1"), Cell("закрытый"), Cell("100")],
            [Cell("Стандарт"), Cell(None), Cell(None)],
            [Cell("ШПК-2"), Cell("открытый"), Cell("200")],
        ],
    )
    first = TableMapping(
        sheet_name="Шкафы", header_row_index=0, start_row_index=2, end_row_index=3,
        source_section_id="sec_first", columns=[
            ColumnMapping(0, "name", header="Наименование"),
            ColumnMapping(1, "attribute", header="Тип шкафа", attribute_code="type"),
            ColumnMapping(2, "price", header="Цена", price_descriptor={"kind": "other", "vat_mode": "unknown"}),
        ],
    )
    second = TableMapping(
        sheet_name="Шкафы", header_row_index=0, start_row_index=4, end_row_index=5,
        source_section_id="sec_second", columns=[
            ColumnMapping(0, "name", header="Наименование"),
            ColumnMapping(1, "attribute", header="Тип шкафа", attribute_code="cabinet_type"),
            ColumnMapping(2, "price", header="Цена", price_descriptor={"kind": "other", "vat_mode": "unknown"}),
        ],
    )

    consolidated = consolidate_section_tables([first, second], grids=[grid])

    assert len(consolidated) == 1
    table = consolidated[0]
    assert table.start_row_index is None
    assert table.end_row_index is None
    assert table.locate_header_by_signature is True
    assert table.dynamic_body is True
    assert next(column for column in table.columns if column.index == 1).attribute_code == "cabinet_type"
    replay, validation = dry_run_decision(
        [grid], MappingDecision(tables=consolidated, auto_ok=True)
    )
    assert validation["passed"] is True
    assert [item.source_name for item in replay.items] == ["ШПК-1", "ШПК-2"]
    assert [
        item.attribute_values[0].attribute_code for item in replay.items
    ] == ["cabinet_type", "cabinet_type"]


def test_consolidates_sparse_attribute_chunks_without_duplicate_positions() -> None:
    grid = SheetGrid(
        name="Шкафы",
        rows=[
            [Cell("Наименование"), Cell("Крепление"), Cell("Цена")],
            [Cell("ШПК-1"), Cell(None), Cell("100")],
            [Cell("ШПК-2"), Cell("настенное"), Cell("200")],
        ],
    )
    sparse = TableMapping(
        sheet_name="Шкафы", header_row_index=0, start_row_index=1, end_row_index=2,
        source_section_id="sec_sparse", columns=[
            ColumnMapping(0, "name", header="Наименование"),
            ColumnMapping(1, "ignore", header="Крепление"),
            ColumnMapping(
                2,
                "price",
                header="Цена",
                price_descriptor={"kind": "other", "vat_mode": "unknown"},
            ),
        ],
    )
    populated = TableMapping(
        sheet_name="Шкафы", header_row_index=0, start_row_index=2, end_row_index=3,
        source_section_id="sec_populated", columns=[
            ColumnMapping(0, "name", header="Наименование"),
            ColumnMapping(
                1,
                "attribute",
                header="Крепление",
                attribute_code="mounting",
                attribute_value_kind="text",
            ),
            ColumnMapping(
                2,
                "price",
                header="Цена",
                price_descriptor={"kind": "other", "vat_mode": "unknown"},
            ),
        ],
    )

    consolidated = consolidate_section_tables([sparse, populated], grids=[grid])
    replay, validation = dry_run_decision(
        [grid], MappingDecision(tables=consolidated, auto_ok=True)
    )

    assert len(consolidated) == 1
    mounting = next(column for column in consolidated[0].columns if column.index == 1)
    assert mounting.role == "attribute"
    assert mounting.attribute_code == "mounting"
    assert validation["passed"] is True
    assert [item.source_name for item in replay.items] == ["ШПК-1", "ШПК-2"]
    assert replay.items[0].attribute_values == []
    assert [attribute.raw_value for attribute in replay.items[1].attribute_values] == [
        "настенное"
    ]


def test_sparse_attribute_inheritance_reads_values_from_its_own_sheet() -> None:
    unrelated = SheetGrid(
        name="Другой лист",
        rows=[
            [Cell("Наименование"), Cell("Крепление"), Cell("Цена")],
            [Cell("Чужой товар"), Cell(None), Cell("50")],
        ],
    )
    target = SheetGrid(
        name="Шкафы",
        rows=[
            [Cell("Наименование"), Cell("Крепление"), Cell("Цена")],
            [Cell("ШПК-1"), Cell("явное значение"), Cell("100")],
            [Cell("ШПК-2"), Cell("настенное"), Cell("200")],
        ],
    )
    ignored_populated = TableMapping(
        sheet_name="Шкафы", header_row_index=0, start_row_index=1, end_row_index=2,
        source_section_id="sec_ignored", columns=[
            ColumnMapping(0, "name", header="Наименование"),
            ColumnMapping(1, "ignore", header="Крепление"),
            ColumnMapping(
                2,
                "price",
                header="Цена",
                price_descriptor={"kind": "other", "vat_mode": "unknown"},
            ),
        ],
    )
    reviewed_attribute = TableMapping(
        sheet_name="Шкафы", header_row_index=0, start_row_index=2, end_row_index=3,
        source_section_id="sec_attribute", columns=[
            ColumnMapping(0, "name", header="Наименование"),
            ColumnMapping(
                1,
                "attribute",
                header="Крепление",
                attribute_code="mounting",
                attribute_value_kind="text",
            ),
            ColumnMapping(
                2,
                "price",
                header="Цена",
                price_descriptor={"kind": "other", "vat_mode": "unknown"},
            ),
        ],
    )

    consolidated = consolidate_section_tables(
        [ignored_populated, reviewed_attribute], grids=[unrelated, target]
    )

    assert len(consolidated) == 2
    assert any(
        column.role == "ignore"
        for table in consolidated
        for column in table.columns
        if column.index == 1
    )


def test_headerless_shape_absorbs_model_price_qualifier_variants() -> None:
    grid = _headerless_composite_grid()
    columns = [
        ColumnMapping(0, "name"),
        ColumnMapping(6, "unit"),
        ColumnMapping(
            7,
            "price",
            price_descriptor={"kind": "other", "vat_mode": "included"},
        ),
    ]
    generated_chunk = TableMapping(
        sheet_name=grid.name,
        header_row_index=0,
        start_row_index=1,
        end_row_index=4,
        source_section_id="sec_generated",
        columns=columns,
    )

    consolidated = consolidate_section_tables([generated_chunk], grids=[grid])

    assert len(consolidated) == 1
    assert consolidated[0].dynamic_body is True
    assert consolidated[0].header_row_index is None
    assert [(column.index, column.role) for column in consolidated[0].columns] == [
        (0, "name"),
        (6, "unit"),
        (7, "price"),
    ]


def _grid_with_category_and_five_items() -> SheetGrid:
    return SheetGrid(
        name="Прайс",
        rows=[
            [Cell("Наименование"), Cell("Цена")],
            [Cell("Огнетушители"), Cell(None)],
            *[
                [Cell(f"Товар {index}"), Cell(str(index * 100))]
                for index in range(1, 6)
            ],
        ],
    )




def _proposal_for(context: dict, *, price_index: int = 2) -> dict:
    header_row = context["header_row_index"]
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
            "role": "article",
            "price_descriptor": None,
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        },
        {
            "index": price_index,
            "role": "price",
            "price_descriptor": {"kind": "retail", "vat_mode": "unknown"},
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        },
    ]
    return {
        "schema_version": "price-rule-proposal-v1",
        "decision": "propose",
        "rule_name": "Обычный трёхколоночный прайс",
        "loader": "xlsx",
        "source_section_id": context["section_id"],
        "mapping": {
            "sheet_name": context["sheet_name"],
            "header_row_index": header_row,
            "columns": columns,
        },
        "discovered_attributes": [],
        "evidence": [
            {"row_index": header_row, "column_index": index, "note": "шапка"}
            for index in (0, 1, price_index)
        ],
        "unresolved": [],
        "confidence": 0.93,
        "requires_manual_review": True,
    }


def test_context_is_redacted_and_split_for_repeated_headers() -> None:
    contexts = build_tabular_section_contexts([_grid()])

    assert [context["header_row_index"] for context in contexts] == [1, 4]
    assert all(context["section_id"].startswith("sec_") for context in contexts)
    text = str(contexts[0]["sample_rows"])
    assert "+7 999 123-45-67" not in text
    assert "[PHONE]" in text


def test_context_marks_independent_horizontal_price_panels() -> None:
    context = build_tabular_section_contexts([_horizontal_panels_grid()])[0]

    assert context["horizontal_panels"] == [
        {"start_column_index": 0, "end_column_index": 4, "name_columns": [0], "price_columns": [2]},
        {"start_column_index": 4, "end_column_index": 7, "name_columns": [4], "price_columns": [6]},
    ]
    assert [
        (row["panel_index"], row["row_index"])
        for row in context["unfolded_panel_rows"]
    ] == [(0, 1), (1, 1)]
    assert context["unfolded_panel_rows"][0]["cells"][0]["value"] == "Огнетушитель"
    assert context["unfolded_panel_rows"][0]["cells"][4]["value"] is None
    assert context["unfolded_panel_rows"][1]["cells"][0]["value"] is None
    assert context["unfolded_panel_rows"][1]["cells"][4]["value"] == "Шкаф пожарный"
    prompt = build_tabular_rule_generation_prompt(
        source_context=context,
        loader="xlsx",
        model="qwen3-235b-a22b-fp8/latest",
    )
    assert "каждый независимый горизонтальный блок" in prompt
    assert '"horizontal_panels_unfolded": true' in prompt
    assert '"panel_index": 0' in prompt
    assert '"panel_index": 1' in prompt
    assert "Исходный loader: xlsx" in prompt
    assert 'поле loader должно быть ровно "xlsx"' in prompt


def test_compiler_requires_and_parser_reads_every_horizontal_panel() -> None:
    grid = _horizontal_panels_grid()
    context = build_tabular_section_contexts([grid])[0]
    partial = _proposal_for(context)

    with pytest.raises(ProposalError, match="Горизонтальная панель 1"):
        compile_section_proposal(
            parse_proposal(partial), source_context=context, loader="xlsx", grids=[grid]
        )

    def column(index: int, role: str, *, price: bool = False) -> dict:
        return {
            "index": index,
            "role": role,
            "price_descriptor": (
                {"kind": "retail", "vat_mode": "unknown"} if price else None
            ),
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        }

    complete = {
        **partial,
        "mapping": {
            **partial["mapping"],
            "columns": [
                column(0, "name"),
                column(1, "unit"),
                column(2, "price", price=True),
                column(4, "name"),
                column(5, "unit"),
                column(6, "price", price=True),
            ],
        },
        "evidence": [
            {"row_index": 0, "column_index": index, "note": "шапка панели"}
            for index in (0, 1, 2, 4, 5, 6)
        ],
    }
    compiled = compile_section_proposal(
        parse_proposal(complete), source_context=context, loader="xlsx", grids=[grid]
    )
    result, metrics = dry_run_decision(
        [grid], MappingDecision(tables=[compiled.table], auto_ok=True)
    )

    assert [item.source_name for item in result.items] == [
        "Огнетушитель",
        "Шкаф пожарный",
    ]
    assert metrics["item_count"] == 2


def test_compiler_keeps_layout_confirmed_category_preamble_for_horizontal_panels() -> None:
    grid = _horizontal_panels_with_category_preamble_grid()
    context = build_tabular_section_contexts([grid])[0]

    def column(index: int, role: str, *, price: bool = False) -> dict:
        return {
            "index": index,
            "role": role,
            "price_descriptor": (
                {"kind": "retail", "vat_mode": "unknown"} if price else None
            ),
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        }

    proposal = {
        "schema_version": "price-rule-proposal-v1",
        "decision": "propose",
        "rule_name": "Гидранты с двумя панелями",
        "loader": "xlsx",
        "source_section_id": context["section_id"],
        "mapping": {
            "sheet_name": context["sheet_name"],
            "header_row_index": context["header_row_index"],
            "columns": [
                column(0, "name"),
                column(1, "unit"),
                column(2, "price", price=True),
                column(3, "price", price=True),
                column(5, "name"),
                column(6, "unit"),
                column(7, "price", price=True),
                column(8, "price", price=True),
            ],
        },
        "discovered_attributes": [],
        "evidence": [
            {"row_index": 0, "column_index": index, "note": "шапка панели"}
            for index in (0, 1, 2, 3, 5, 6, 7, 8)
        ],
        "unresolved": [],
        "confidence": 0.99,
        "requires_manual_review": True,
    }

    assert context["section_start_row_index"] == 1
    assert context["category_row_indexes"] == [1]
    assert context["price_presence"]["candidate_row_indexes"][0] == 2

    compiled = compile_section_proposal(
        parse_proposal(proposal), source_context=context, loader="xlsx", grids=[grid]
    )
    result, validation = dry_run_decision(
        [grid], MappingDecision(tables=[compiled.table], auto_ok=True)
    )
    right_item = next(
        item
        for item in result.items
        if item.source_name == "Колонка водоразборная 1,50 м"
    )

    assert compiled.table.start_row_index == 1
    assert validation["passed"] is True
    assert right_item.category_path == "Колонки водоразборные"
    assert "Для разбора питьевой воды" not in right_item.category_path

    context_without_confirmed_category = {
        **context,
        "category_row_indexes": [],
    }
    compiled_without_confirmed_category = compile_section_proposal(
        parse_proposal(proposal),
        source_context=context_without_confirmed_category,
        loader="xlsx",
        grids=[grid],
    )
    assert compiled_without_confirmed_category.table.start_row_index == 2


def test_compiler_propagates_a_confirmed_role_to_an_empty_repeated_panel_column() -> None:
    grid = _horizontal_panels_with_empty_repeated_attribute_grid()
    context = build_tabular_section_contexts([grid])[0]

    def column(
        index: int,
        role: str,
        *,
        price: bool = False,
        attribute_code: str | None = None,
    ) -> dict:
        return {
            "index": index,
            "role": role,
            "price_descriptor": (
                {"kind": "retail", "vat_mode": "unknown"} if price else None
            ),
            "attribute_code": attribute_code,
            "attribute_value_kind": "text" if role == "attribute" else None,
            "canonical_unit": None,
        }

    payload = {
        "schema_version": "price-rule-proposal-v1",
        "decision": "propose",
        "rule_name": "Две одинаковые панели",
        "loader": "xlsx",
        "source_section_id": context["section_id"],
        "mapping": {
            "sheet_name": context["sheet_name"],
            "header_row_index": context["header_row_index"],
            "columns": [
                column(0, "name"),
                column(1, "unit"),
                column(2, "attribute", attribute_code="packaging"),
                column(3, "price", price=True),
                column(5, "name"),
                column(6, "unit"),
                column(7, "ignore"),
                column(8, "price", price=True),
            ],
        },
        "discovered_attributes": [],
        "evidence": [
            {"row_index": 0, "column_index": index, "note": "шапка панели"}
            for index in (0, 1, 2, 3, 5, 6, 7, 8)
        ],
        "unresolved": [],
        "confidence": 0.95,
        "requires_manual_review": True,
    }

    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )

    repeated = next(column for column in compiled.table.columns if column.index == 7)
    assert repeated.role == "attribute"
    assert repeated.attribute_code == "packaging"
    assert repeated.attribute_value_kind == "text"


def test_quantity_threshold_is_scoped_to_its_horizontal_panel() -> None:
    grid = SheetGrid(
        name="Две панели с разными условиями цены",
        rows=[
            [
                Cell("Наименование"), Cell("Ед."), Cell("Упаковка"), Cell("Опт"), Cell("Розница"), Cell(None),
                Cell("Наименование"), Cell("Ед."), Cell("Опт"), Cell("Розница"),
            ],
            [Cell("Товар А"), Cell("шт"), Cell(10), Cell(100), Cell(120), Cell(None), Cell("Товар Б"), Cell("шт"), Cell(200), Cell(240)],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]

    def column(index: int, role: str, *, price: bool = False, threshold: int | None = None) -> dict:
        return {
            "index": index,
            "role": role,
            "price_descriptor": ({"kind": "retail", "vat_mode": "unknown"} if price else None),
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
            "quantity_threshold_column_index": threshold,
        }

    payload = {
        "schema_version": "price-rule-proposal-v1",
        "decision": "propose",
        "rule_name": "Панели с независимыми условиями",
        "loader": "xlsx",
        "source_section_id": context["section_id"],
        "mapping": {
            "sheet_name": grid.name,
            "header_row_index": 0,
            "name_fallback_column_indexes": [],
            "columns": [
                column(0, "name"), column(1, "unit"), column(2, "quantity_threshold"),
                column(3, "price", price=True, threshold=2), column(4, "price", price=True, threshold=2),
                column(6, "name"), column(7, "unit"),
                column(8, "price", price=True), column(9, "price", price=True),
            ],
        },
        "discovered_attributes": [],
        "evidence": [
            {"row_index": 0, "column_index": index, "note": "шапка панели"}
            for index in (0, 1, 2, 3, 4, 6, 7, 8, 9)
        ],
        "unresolved": [],
        "confidence": 0.98,
        "requires_manual_review": True,
    }

    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )

    assert compiled.table is not None
    assert {
        column.index: column.quantity_threshold_column_index
        for column in compiled.table.columns
        if column.role == "price"
    } == {3: 2, 4: 2, 8: None, 9: None}


def test_packaging_text_is_preserved_as_attribute_not_price_threshold() -> None:
    grid = SheetGrid(
        name="Упаковка рядом с ценами",
        rows=[
            [Cell("Наименование"), Cell("Упак"), Cell("Опт"), Cell("Розница")],
            [Cell("Товар А"), Cell("100 шт"), Cell(100), Cell(120)],
            [Cell("Товар Б"), Cell("50 шт"), Cell(200), Cell(240)],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]

    def column(index: int, role: str, *, price: bool = False, threshold: int | None = None) -> dict:
        return {
            "index": index, "role": role,
            "price_descriptor": ({"kind": "retail", "vat_mode": "unknown"} if price else None),
            "attribute_code": None, "attribute_value_kind": None,
            "canonical_unit": None, "quantity_threshold_column_index": threshold,
        }

    payload = {
        "schema_version": "price-rule-proposal-v1", "decision": "propose",
        "rule_name": "Упаковка и цены", "loader": "xlsx",
        "source_section_id": context["section_id"],
        "mapping": {
            "sheet_name": grid.name, "header_row_index": 0,
            "name_fallback_column_indexes": [],
            "columns": [
                column(0, "name"), column(1, "quantity_threshold"),
                column(2, "price", price=True, threshold=1),
                column(3, "price", price=True, threshold=1),
            ],
        },
        "discovered_attributes": [],
        "evidence": [
            {"row_index": 0, "column_index": index, "note": "шапка"}
            for index in range(4)
        ],
        "unresolved": [], "confidence": 0.98, "requires_manual_review": True,
    }

    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )
    columns = {column.index: column for column in compiled.table.columns}

    assert columns[1].role == "attribute"
    assert columns[1].attribute_value_kind == "text"
    assert columns[2].quantity_threshold_column_index is None
    assert columns[3].quantity_threshold_column_index is None


def test_compiler_keeps_source_bound_panel_name_repeated_by_vertical_merge() -> None:
    grid = SheetGrid(
        name="Панель с вертикальным merge",
        rows=[
            [Cell("Наименование"), Cell("Цена"), Cell("Наименование"), Cell("Цена")],
            [Cell("Левый товар"), Cell("100"), Cell("Правый товар"), Cell("200")],
            [Cell(None), Cell(None), Cell("Правый товар", is_merged_fill=True), Cell("210")],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]

    def column(index: int, role: str, *, price: bool = False) -> dict:
        return {
            "index": index,
            "role": role,
            "price_descriptor": (
                {"kind": "retail", "vat_mode": "unknown"} if price else None
            ),
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        }

    payload = {
        "schema_version": "price-rule-proposal-v1",
        "decision": "propose",
        "rule_name": "Две панели с объединённым названием",
        "loader": "xlsx",
        "source_section_id": context["section_id"],
        "mapping": {
            "sheet_name": grid.name,
            "header_row_index": 0,
            "name_fallback_column_indexes": [],
            "columns": [
                column(0, "name"),
                column(1, "price", price=True),
                column(2, "name"),
                column(3, "price", price=True),
            ],
        },
        "discovered_attributes": [],
        "evidence": [
            {"row_index": 0, "column_index": index, "note": "шапка панели"}
            for index in range(4)
        ],
        "unresolved": [],
        "confidence": 0.98,
        "requires_manual_review": True,
    }

    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )

    assert [column.index for column in compiled.table.columns if column.role == "name"] == [
        0, 2,
    ]


def test_three_sparse_panels_are_unfolded_panel_first_and_all_rows_are_parsed() -> None:
    grid = SheetGrid(
        name="Три панели",
        rows=[
            [
                Cell("Наименование"), Cell("Цена"), Cell(None),
                Cell("Наименование"), Cell("Цена"), Cell(None),
                Cell("Наименование"), Cell("Цена"),
            ],
            [Cell("Л-1"), Cell("100"), Cell(None), Cell("Ц-1"), Cell("200"), Cell(None), Cell("П-1"), Cell("300")],
            [Cell("Л-2"), Cell("110"), Cell(None), Cell(None), Cell(None), Cell(None), Cell("П-2"), Cell("310")],
            [Cell(None), Cell(None), Cell(None), Cell("Ц-2"), Cell("210"), Cell(None), Cell(None), Cell(None)],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]

    assert [
        (row["panel_index"], row["row_index"])
        for row in context["unfolded_panel_rows"]
    ] == [(0, 1), (0, 2), (1, 1), (1, 3), (2, 1), (2, 2)]

    def column(index: int, role: str, *, price: bool = False) -> dict:
        return {
            "index": index,
            "role": role,
            "price_descriptor": (
                {"kind": "retail", "vat_mode": "unknown"} if price else None
            ),
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        }

    payload = {
        "schema_version": "price-rule-proposal-v1",
        "decision": "propose",
        "rule_name": "Три независимые панели",
        "loader": "xlsx",
        "source_section_id": context["section_id"],
        "mapping": {
            "sheet_name": grid.name,
            "header_row_index": 0,
            "name_fallback_column_indexes": [],
            "columns": [
                column(0, "name"), column(1, "price", price=True),
                column(3, "name"), column(4, "price", price=True),
                column(6, "name"), column(7, "price", price=True),
            ],
        },
        "discovered_attributes": [],
        "evidence": [
            {"row_index": 0, "column_index": index, "note": "шапка панели"}
            for index in (0, 1, 3, 4, 6, 7)
        ],
        "unresolved": [],
        "confidence": 0.98,
        "requires_manual_review": True,
    }
    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )
    result, _ = dry_run_decision(
        [grid], MappingDecision(tables=[compiled.table], auto_ok=True)
    )

    assert [item.source_name for item in result.items] == [
        "Л-1", "Л-2", "Ц-1", "Ц-2", "П-1", "П-2",
    ]


def test_prompt_uses_model_semantics_not_deterministic_role_hints() -> None:
    context = build_tabular_section_contexts([_grid()])[0]
    prompt = build_tabular_rule_generation_prompt(
        source_context=context,
        loader="xlsx",
        model="qwen3-235b-a22b-fp8/latest",
        attribute_catalog=[
            {
                "code": "nominal_diameter",
                "display_name": "Номинальный диаметр",
                "value_kind": "decimal",
                "canonical_unit": "mm",
            }
        ],
    )

    assert "nominal_diameter" in prompt
    assert "deterministic_observations" not in prompt
    assert "suggested_role" not in prompt
    assert "по полному смыслу заголовка" in RULE_PROPOSAL_SYSTEM_PROMPT
    assert "коммерческая величина не становится габаритом" in RULE_PROPOSAL_SYSTEM_PROMPT
    assert "supplier-scoped draft" in RULE_PROPOSAL_SYSTEM_PROMPT
    assert "sample_rows — реальные строки позиций" in RULE_PROPOSAL_SYSTEM_PROMPT


def test_model_can_correct_bad_local_role_hints() -> None:
    grid = SheetGrid(
        name="Ошибочные эвристики",
        rows=[
            [
                Cell("Наименование"),
                Cell("размер скидки"),
                Cell("Номинальный диаметр присоединенной трубы, мм"),
                Cell("Цена"),
            ],
            [Cell("Клапан"), Cell("5%"), Cell("50"), Cell("1200")],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    payload = {
        "schema_version": "price-rule-proposal-v1",
        "decision": "propose",
        "rule_name": "Исправленная семантика",
        "loader": "xlsx",
        "source_section_id": context["section_id"],
        "mapping": {
            "sheet_name": grid.name,
            "header_row_index": 0,
            "columns": [
                {"index": 0, "role": "name", "price_descriptor": None, "attribute_code": None, "attribute_value_kind": None, "canonical_unit": None},
                {"index": 1, "role": "discount_percent", "price_descriptor": None, "attribute_code": None, "attribute_value_kind": "decimal", "canonical_unit": "%"},
                {"index": 2, "role": "attribute", "price_descriptor": None, "attribute_code": "nominal_diameter", "attribute_value_kind": "decimal", "canonical_unit": "mm"},
                {"index": 3, "role": "price", "price_descriptor": {"kind": "retail", "vat_mode": "unknown"}, "attribute_code": None, "attribute_value_kind": None, "canonical_unit": None},
            ],
        },
        "discovered_attributes": [
            {
                "source_kind": "column",
                "extractor_kind": None,
                "source_column_index": 2,
                "attribute_code": "nominal_diameter",
                "display_name": "Номинальный диаметр",
                "value_kind": "decimal",
                "canonical_unit": "mm",
                "is_new": False,
                "evidence": [{"row_index": 0, "column_index": 2, "note": "шапка"}],
            }
        ],
        "evidence": [
            {"row_index": 0, "column_index": index, "note": "шапка"}
            for index in range(4)
        ],
        "unresolved": [],
        "confidence": 0.97,
        "requires_manual_review": True,
    }

    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )

    assert [column.role for column in compiled.table.columns] == [
        "name", "discount_percent", "attribute", "price"
    ]
    assert compiled.validation["discovered_attributes"][0]["attribute_code"] == "nominal_diameter"
    assert "лишние metadata атрибута отброшены" in compiled.validation["normalisation_warnings"][0]


def test_inline_technical_attribute_requires_item_row_evidence() -> None:
    grid = SheetGrid(
        name="Inline",
        rows=[
            [Cell("Наименование"), Cell("Цена")],
            [Cell("Насос 220 В IP54"), Cell("3400")],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    payload = _proposal_for(context, price_index=1)
    payload["mapping"]["columns"] = [payload["mapping"]["columns"][0], payload["mapping"]["columns"][2]]
    payload["evidence"] = [
        {"row_index": 0, "column_index": index, "note": "шапка"} for index in (0, 1)
    ]
    payload["discovered_attributes"] = [
        {
            "source_kind": "inline",
            "extractor_kind": "number_with_unit",
            "source_column_index": 0,
            "attribute_code": "rated_voltage",
            "display_name": "Номинальное напряжение",
            "value_kind": "decimal",
            "canonical_unit": "V",
            "is_new": False,
            "evidence": [{"row_index": 1, "column_index": 0, "note": "220 В"}],
        }
    ]

    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )

    assert compiled.validation["discovered_attributes"][0]["source_kind"] == "inline"
    assert compiled.table.inline_attributes[0].extractor_kind == "number_with_unit"


def test_compiler_drops_inline_model_designation_that_safe_extractor_cannot_confirm() -> None:
    grid = SheetGrid(
        name="Inline",
        rows=[
            [Cell("Наименование"), Cell("Цена")],
            [Cell("Огнетушитель ОВП-4"), Cell("1300")],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    payload = _proposal_for(context, price_index=1)
    payload["mapping"]["columns"] = [
        payload["mapping"]["columns"][0],
        payload["mapping"]["columns"][2],
    ]
    payload["evidence"] = [
        {"row_index": 0, "column_index": index, "note": "шапка"}
        for index in (0, 1)
    ]
    payload["discovered_attributes"] = [
        {
            "source_kind": "inline",
            "extractor_kind": "number_with_unit",
            "source_column_index": 0,
            "attribute_code": "fire_extinguisher_type",
            "display_name": "Тип огнетушителя",
            "value_kind": "text",
            "canonical_unit": None,
            "is_new": True,
            "evidence": [{"row_index": 1, "column_index": 0, "note": "ОВП-4"}],
        }
    ]

    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )

    assert compiled.table is not None
    assert compiled.table.inline_attributes == []
    assert "number_with_unit требует canonical_unit" in compiled.validation[
        "normalisation_warnings"
    ][0]


def test_compiler_accepts_discovered_attribute_for_typed_weight_role() -> None:
    grid = SheetGrid(
        name="Weight",
        rows=[
            [Cell("Наименование"), Cell("Вес"), Cell("Цена")],
            [Cell("Клапан"), Cell("1,2"), Cell("5000")],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    payload = _proposal_for(context)
    payload["mapping"]["columns"][1] = {
        "index": 1,
        "role": "weight",
        "price_descriptor": None,
        "attribute_code": "weight",
        "attribute_value_kind": "decimal",
        "canonical_unit": "кг",
    }
    payload["discovered_attributes"] = [
        {
            "source_kind": "column",
            "extractor_kind": None,
            "source_column_index": 1,
            "attribute_code": "weight",
            "display_name": "Вес",
            "value_kind": "decimal",
            "canonical_unit": "кг",
            "is_new": False,
            "evidence": [{"row_index": 0, "column_index": 1, "note": "Вес"}],
        }
    ]

    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )

    assert compiled.table is not None
    weight = next(column for column in compiled.table.columns if column.index == 1)
    assert (weight.role, weight.attribute_code, weight.canonical_unit) == (
        "weight",
        "weight",
        "kg",
    )


def test_compiler_deduplicates_repeated_inline_candidates() -> None:
    grid = SheetGrid(
        name="Inline",
        rows=[
            [Cell("Наименование"), Cell("Цена")],
            [Cell("Насос 220 В"), Cell("3400")],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    payload = _proposal_for(context, price_index=1)
    payload["mapping"]["columns"] = [
        payload["mapping"]["columns"][0],
        payload["mapping"]["columns"][2],
    ]
    payload["evidence"] = [
        {"row_index": 0, "column_index": index, "note": "шапка"}
        for index in (0, 1)
    ]
    candidate = {
        "source_kind": "inline",
        "extractor_kind": "number_with_unit",
        "source_column_index": 0,
        "attribute_code": "rated_voltage",
        "display_name": "Напряжение",
        "value_kind": "decimal",
        "canonical_unit": "В",
        "is_new": False,
        "evidence": [{"row_index": 1, "column_index": 0, "note": "220 В"}],
    }
    payload["discovered_attributes"] = [candidate, dict(candidate)]

    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )

    assert compiled.table is not None
    assert len(compiled.table.inline_attributes) == 1
    assert compiled.table.inline_attributes[0].canonical_unit == "V"
    assert "повторный кандидат отброшен" in " ".join(
        compiled.validation["normalisation_warnings"]
    )


def test_compiler_allows_ignored_merged_fill_header_column() -> None:
    grid = SheetGrid(
        name="Merged",
        rows=[
            [Cell("Наименование"), Cell("Наименование", is_merged_fill=True), Cell("Цена")],
            [Cell("Товар"), Cell("Товар", is_merged_fill=True), Cell("100")],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    payload = _proposal_for(context)
    payload["mapping"]["columns"][1]["role"] = "ignore"

    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )

    assert compiled.table is not None


def test_compiler_normalizes_model_merged_fill_copy_from_name_to_ignore() -> None:
    grid = SheetGrid(
        name="Merged",
        rows=[
            [Cell("Наименование"), Cell("Наименование", is_merged_fill=True), Cell("Цена")],
            [Cell("Товар"), Cell("Товар", is_merged_fill=True), Cell("100")],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    payload = _proposal_for(context)
    payload["mapping"]["columns"][1]["role"] = "name"

    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )

    assert compiled.table is not None
    assert [(column.index, column.role) for column in compiled.table.columns] == [
        (0, "name"),
        (1, "ignore"),
        (2, "price"),
    ]
    assert "merged-fill копия name" in " ".join(
        compiled.validation["normalisation_warnings"]
    )


def test_compiler_separates_currency_marker_column_from_price_amount() -> None:
    grid = SheetGrid(
        name="Currency split",
        rows=[
            [
                Cell("Наименование"),
                Cell("Описание"),
                Cell("Цена с НДС"),
                Cell("Цена с НДС", is_merged_fill=True),
            ],
            [Cell("Клапан"), Cell("Стальной"), Cell("€"), Cell("15")],
            [Cell("Муфта"), Cell("Латунная"), Cell("€"), Cell("7.5")],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    payload = _proposal_for(context, price_index=3)
    payload["mapping"]["columns"].append(
        {
            "index": 2,
            "role": "price",
            "price_descriptor": {"kind": "retail", "vat_mode": "included"},
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        }
    )
    payload["evidence"].append(
        {"row_index": 0, "column_index": 2, "note": "часть объединённой шапки цены"}
    )

    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )
    result, validation = dry_run_decision(
        [grid], MappingDecision(tables=[compiled.table], auto_ok=True)
    )

    assert [(column.index, column.role) for column in compiled.table.columns] == [
        (0, "name"),
        (1, "article"),
        (3, "price"),
        (2, "ignore"),
    ]
    assert validation["passed"] is True
    assert [value.amount for item in result.items for value in item.values] == [15, 7.5]


def test_compiler_completes_only_unambiguous_source_backed_name_fallback() -> None:
    grid = SheetGrid(
        name="Variants",
        rows=[
            [Cell("Наименование"), Cell("Маркировка"), Cell("Цена")],
            [Cell("Спринклер"), Cell("SPR-57"), Cell("100")],
            [Cell(None), Cell("SPR-68"), Cell("110")],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    payload = _proposal_for(context)

    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )

    assert compiled.table is not None
    assert compiled.table.name_fallback_column_indexes == [1]
    result, validation = dry_run_decision(
        [grid], MappingDecision(tables=[compiled.table], auto_ok=True)
    )
    assert validation["passed"] is True
    assert [item.source_name for item in result.items] == ["Спринклер", "SPR-68"]


def test_compiler_allows_ignored_named_artifact_column_without_section_values() -> None:
    grid = SheetGrid(
        name="Artifact",
        rows=[
            [Cell("Наименование"), Cell("Цена"), Cell("Оглавление")],
            [Cell("Товар"), Cell("100"), Cell(None)],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    payload = _proposal_for(context, price_index=1)
    payload["mapping"]["columns"] = [
        payload["mapping"]["columns"][0],
        payload["mapping"]["columns"][2],
        {
            "index": 2,
            "role": "ignore",
            "price_descriptor": None,
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        },
    ]
    payload["evidence"] = [
        {"row_index": 0, "column_index": index, "note": "шапка"}
        for index in (0, 1, 2)
    ]

    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )

    assert compiled.table is not None
    assert next(column for column in compiled.table.columns if column.index == 2).role == "ignore"


def test_compiler_preserves_populated_ignored_named_column_as_supplier_attribute() -> None:
    grid = SheetGrid(
        name="Supplier field",
        rows=[
            [Cell("Наименование"), Cell("Цена"), Cell("Монтаж")],
            [Cell("Клапан"), Cell("100"), Cell("настенный")],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    payload = _proposal_for(context, price_index=1)
    payload["mapping"]["columns"] = [
        payload["mapping"]["columns"][0],
        payload["mapping"]["columns"][2],
        {
            "index": 2,
            "role": "ignore",
            "price_descriptor": None,
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        },
    ]
    payload["evidence"] = [
        {"row_index": 0, "column_index": index, "note": "шапка"}
        for index in (0, 1, 2)
    ]

    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )
    result, validation = dry_run_decision(
        [grid], MappingDecision(tables=[compiled.table], auto_ok=True)
    )

    column = next(column for column in compiled.table.columns if column.index == 2)
    assert column.role == "attribute"
    assert column.attribute_code is None
    assert column.attribute_value_kind == "text"
    assert validation["passed"] is True
    assert result.items[0].attribute_values[0].raw_value == "настенный"


def test_compiler_preserves_omitted_merged_name_continuation() -> None:
    grid = SheetGrid(
        name="Merged",
        rows=[
            [Cell("Наименование"), Cell("Наименование", is_merged_fill=True), Cell("Цена")],
            [Cell("Товар"), Cell("Товар", is_merged_fill=True), Cell("100")],
            [Cell(None), Cell("Товар из продолжения"), Cell("200")],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    payload = _proposal_for(context)
    payload["mapping"]["columns"] = [
        payload["mapping"]["columns"][0],
        payload["mapping"]["columns"][2],
    ]

    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )

    assert compiled.table is not None
    assert [(column.index, column.role) for column in compiled.table.columns] == [
        (0, "name"),
        (2, "price"),
        (1, "ignore"),
    ]
    result, validation = dry_run_decision(
        [grid], MappingDecision(tables=[compiled.table], auto_ok=True)
    )
    assert validation["passed"] is True
    assert [item.source_name for item in result.items] == [
        "Товар",
        "Товар из продолжения",
    ]


def test_context_samples_only_item_rows_and_honours_requested_limit() -> None:
    grid = _grid_with_category_and_five_items()

    for limit in (1, 3, 4, 5):
        context = build_tabular_section_contexts(
            [grid], sample_rows_limit=limit
        )[0]
        assert [row["row_index"] for row in context["sample_rows"]] == list(
            range(2, 2 + limit)
        )
        assert context["sample_rows_requested"] == limit
        assert context["sample_rows_available"] == 5


def test_context_returns_all_available_rows_for_short_section() -> None:
    grid = SheetGrid(
        name="Короткий прайс",
        rows=[
            [Cell("Название"), Cell("Цена")],
            [Cell("Единственный товар"), Cell("123")],
        ],
    )

    context = build_tabular_section_contexts([grid], sample_rows_limit=5)[0]

    assert [row["row_index"] for row in context["sample_rows"]] == [1]
    assert context["sample_rows_available"] == 1


def test_context_defaults_to_five_real_item_rows() -> None:
    context = build_tabular_section_contexts([_grid_with_category_and_five_items()])[0]

    assert context["sample_rows_requested"] == 5
    assert [row["row_index"] for row in context["sample_rows"]] == [2, 3, 4, 5, 6]


def test_complex_header_is_not_truncated_at_280_characters() -> None:
    long_header = "Цена для сложной конфигурации " + "очень подробное условие " * 20
    grid = SheetGrid(
        name="Сложная шапка",
        rows=[
            [Cell("Наименование"), Cell(long_header)],
            [Cell("Товар"), Cell("100")],
        ],
    )

    context = build_tabular_section_contexts([grid], sample_rows_limit=1)[0]

    assert len(long_header) > 280
    assert context["header_values"][1] == long_header.strip()


def test_model_proposals_compile_only_against_exact_source_coordinates() -> None:
    grids = [_grid()]
    contexts = build_tabular_section_contexts(grids)
    proposals = [_proposal_for(context) for context in contexts]

    decision = compile_batch_proposals(
        zip(proposals, contexts), loader="xlsx", grids=grids
    )
    result, validation = dry_run_decision(grids, decision)

    assert len(decision.tables) == 2
    assert [item.source_name for item in result.items] == ["Клапан", "Связь +7 999 123-45-67", "Муфта"]
    assert validation["passed"] is True


def test_attempt_context_must_still_match_current_source() -> None:
    grids = [_grid()]
    context = build_tabular_section_contexts(grids)[0]
    attempt = RuleGenerationAttempt(
        section_id=context["section_id"],
        source_context_hash=context_hash(context),
        input_context=context,
    )
    assert verified_current_context(attempt=attempt, grids=grids)["section_id"] == context["section_id"]

    grids[0].rows[2][0] = Cell("Подменённый товар")
    try:
        verified_current_context(attempt=attempt, grids=grids)
    except ProposalError as exc:
        assert "Источник изменился" in str(exc)
    else:  # pragma: no cover - guards the model/source binding boundary
        raise AssertionError("Подменённый источник не должен принять старый ответ модели")


def test_compiler_rejects_nonexistent_or_uncited_column() -> None:
    grids = [_grid()]
    context = build_tabular_section_contexts(grids)[0]
    raw = _proposal_for(context, price_index=9)
    proposal = parse_proposal(raw)

    try:
        compile_section_proposal(
            proposal, source_context=context, loader="xlsx", grids=grids
        )
    except ProposalError as exc:
        assert "вне исходного листа" in str(exc)
    else:  # pragma: no cover - makes the safety boundary explicit
        raise AssertionError("Не существующая колонка не должна компилироваться")


def test_compiler_retains_description_and_article_not_named_by_model() -> None:
    grids = [_grid_with_description()]
    context = build_tabular_section_contexts(grids)[0]
    raw = _proposal_for(context)
    raw["mapping"]["columns"] = [
        raw["mapping"]["columns"][0],
        {
            "index": 3,
            "role": "price",
            "price_descriptor": {"kind": "retail", "vat_mode": "unknown"},
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        },
    ]
    raw["evidence"] = [
        {"row_index": context["header_row_index"], "column_index": index, "note": "шапка"}
        for index in (0, 3)
    ]
    compiled = compile_section_proposal(
        parse_proposal(raw), source_context=context, loader="xlsx", grids=grids
    )
    assert compiled.table is not None
    assert {column.role for column in compiled.table.columns} >= {
        "name",
        "article",
        "description",
        "price",
    }
    result, validation = dry_run_decision(
        grids, MappingDecision(tables=[compiled.table], auto_ok=True)
    )
    assert validation["passed"] is True
    assert result.items[0].source_article == "K-1"
    assert result.items[0].description == "Стальной клапан"


def test_compiler_does_not_revive_omitted_technical_column_as_price() -> None:
    grid = SheetGrid(
        name="Насосы",
        rows=[
            [
                Cell("Наименование"),
                Cell("Трубное присоединение"),
                Cell("Трубное присоединение", is_merged_fill=True),
                Cell("у.е. без НДС"),
            ],
            [Cell("Насос 1"), Cell("G 1/2''"), Cell("G 1/2''"), Cell("82.24")],
            [Cell("Насос 2"), Cell("G 3/4''"), Cell("G 3/4''"), Cell("по запросу")],
        ],
    )
    context = build_tabular_section_contexts([grid])[0]
    payload = _proposal_for(context, price_index=3)
    payload["mapping"]["columns"][1] = {
        "index": 1,
        "role": "attribute",
        "price_descriptor": None,
        "attribute_code": "supplier_tube_connection",
        "attribute_value_kind": "text",
        "canonical_unit": None,
    }

    compiled = compile_section_proposal(
        parse_proposal(payload), source_context=context, loader="xlsx", grids=[grid]
    )
    result, validation = dry_run_decision(
        [grid], MappingDecision(tables=[compiled.table], auto_ok=True)
    )

    assert [column.index for column in compiled.table.columns if column.role == "price"] == [3]
    assert validation["passed"] is True
    assert [item.source_name for item in result.items] == ["Насос 1", "Насос 2"]


def test_non_price_sheet_is_removed_before_model_without_blocking_price_table() -> None:
    grids = [_grid(), _contacts_grid()]
    contexts = build_tabular_section_contexts(grids)
    product_contexts = [context for context in contexts if context["sheet_name"] == "Прайс"]
    assert all(context["sheet_name"] != "Контакты" for context in contexts)
    decision = compile_batch_proposals(
        [(_proposal_for(context), context) for context in product_contexts],
        loader="xlsx",
        grids=grids,
    )
    # The two price chunks have different physical header signatures, so they
    # remain two reusable templates; the skipped contact block adds no table.
    assert len(decision.tables) == 2
    assert decision.non_price_sheets == []


def test_non_price_section_inside_price_sheet_keeps_bounded_price_tables() -> None:
    contacts = _contacts_grid()
    contacts.name = "Прайс"
    grids = [_grid(), contacts]
    contexts = build_tabular_section_contexts([grids[0]])
    product_contexts = [context for context in contexts if context["sheet_index"] == 0]
    contacts_context = {
        "schema_version": "tabular-section-mapping-v1",
        "section_id": "sec_contacts",
        "sheet_index": 1,
        "sheet_name": "Прайс",
        "header_row_index": 0,
        "header_values": ["Контакт", "Телефон"],
        "header_rows": [
            {
                "row_index": 0,
                "cells": [
                    {"value": "Контакт", "is_merged_fill": False},
                    {"value": "Телефон", "is_merged_fill": False},
                ],
            }
        ],
        "sample_rows": [],
        "deterministic_observations": [],
    }
    skipped = {
        "schema_version": "price-rule-proposal-v1",
        "decision": "not_price_section",
        "rule_name": "Контакты",
        "loader": "xlsx",
        "source_section_id": contacts_context["section_id"],
        "mapping": None,
        "discovered_attributes": [],
        "evidence": [
            {
                "row_index": contacts_context["header_row_index"],
                "column_index": 0,
                "note": "Контакты отдела",
            }
        ],
        "unresolved": ["Контактные данные, а не товарная таблица"],
        "confidence": 0.99,
        "requires_manual_review": True,
    }
    decision = compile_batch_proposals(
        [
            *[(_proposal_for(context), context) for context in product_contexts],
            (skipped, contacts_context),
        ],
        loader="xlsx",
        grids=grids,
    )

    # The skipped contact section must not erase or duplicate the price
    # tables.  The two repeated header blocks keep their source-bound
    # semantics: the block under ``Оптовая цена, руб.``
    # resolves deterministically to wholesale, while the generic
    # ``Цена, руб.`` block keeps the model retail.  Since
    # deterministic price_kind is authoritative, they can no longer be
    # collapsed into one template with a single descriptor.
    assert len(decision.tables) == 2
    assert decision.non_price_sheets == []
    by_header = {table.header_row_index: table for table in decision.tables}
    assert by_header[1].dynamic_body is True
    assert {
        header: next(
            column.price_descriptor["kind"]
            for column in table.columns
            if column.role == "price"
        )
        for header, table in by_header.items()
    } == {1: "wholesale", 4: "retail"}


def test_ambiguous_numeric_contact_context_can_be_classified_as_not_price() -> None:
    context = {
        "schema_version": "tabular-section-mapping-v1",
        "section_kind": "ambiguous_tabular_rows",
        "section_id": "sec_contacts_numeric",
        "sheet_index": 0,
        "sheet_name": "Контакты",
        "header_row_index": 0,
        "header_values": ["Отдел", "Телефон"],
        "header_rows": [
            {
                "row_index": 0,
                "cells": [
                    {"value": "Отдел", "is_merged_fill": False},
                    {"value": "Телефон", "is_merged_fill": False},
                ],
            }
        ],
        "sample_rows": [],
        "deterministic_observations": [
            {"index": 1, "header": "Телефон", "suggested_role": "price"}
        ],
    }
    proposal = parse_proposal(
        {
            "schema_version": "price-rule-proposal-v1",
            "decision": "not_price_section",
            "rule_name": "Контакты",
            "loader": "xlsx",
            "source_section_id": context["section_id"],
            "mapping": None,
            "discovered_attributes": [],
            "evidence": [{"row_index": 0, "column_index": 0, "note": "Отдел"}],
            "unresolved": ["Контактный справочник без товаров и цен"],
            "confidence": 0.99,
            "requires_manual_review": True,
        }
    )

    compiled = compile_section_proposal(
        proposal, source_context=context, loader="xlsx", grids=[]
    )

    assert compiled.table is None
    assert compiled.validation["passed"] is True


def test_explicit_price_context_cannot_be_classified_as_not_price() -> None:
    context = {
        "schema_version": "tabular-section-mapping-v1",
        "section_kind": "price_rows",
        "section_id": "sec_explicit_price",
        "sheet_index": 0,
        "sheet_name": "Прайс",
        "header_row_index": 0,
        "header_values": ["Наименование", "Цена"],
        "header_rows": [
            {
                "row_index": 0,
                "cells": [
                    {"value": "Наименование", "is_merged_fill": False},
                    {"value": "Цена", "is_merged_fill": False},
                ],
            }
        ],
        "sample_rows": [],
        "deterministic_observations": [
            {"index": 1, "header": "Цена", "suggested_role": "price"}
        ],
    }
    proposal = parse_proposal(
        {
            "schema_version": "price-rule-proposal-v1",
            "decision": "not_price_section",
            "rule_name": "Прайс",
            "loader": "xlsx",
            "source_section_id": context["section_id"],
            "mapping": None,
            "discovered_attributes": [],
            "evidence": [{"row_index": 0, "column_index": 0, "note": "Шапка"}],
            "unresolved": ["Ошибочная попытка пропуска"],
            "confidence": 0.99,
            "requires_manual_review": True,
        }
    )

    assert _is_price_indicated(context) is True
    assert _is_price_indicated(
        {
            **context,
            "deterministic_observations": [
                {
                    "index": 1,
                    "header": "РАЗДЕЛ VI. ТРУБОПРОВОДНАЯ АРМАТУРА",
                    "suggested_role": "price",
                }
            ],
        }
    ) is False

    with pytest.raises(ProposalError, match="локальным признаком цены"):
        compile_section_proposal(
            proposal, source_context=context, loader="xlsx", grids=[]
        )


def test_strict_schema_closes_every_object() -> None:
    schema = rule_proposal_json_schema()

    def assert_closed(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
            for value in node.values():
                assert_closed(value)
        elif isinstance(node, list):
            for value in node:
                assert_closed(value)

    assert_closed(schema)
    mapping_schema = schema["$defs"]["ProposalMapping"]
    assert "name_fallback_column_indexes" in mapping_schema["properties"]
    assert "name_fallback_column_indexes" in mapping_schema["required"]


def test_prompt_implementation_has_no_test_price_or_supplier_hardcodes() -> None:
    implementation = RULE_PROPOSAL_SYSTEM_PROMPT + inspect.getsource(
        build_tabular_rule_generation_prompt
    )

    forbidden = (
        "Global Security",
        "UNFIRE",
        "Сибирский Арсенал",
        "Пождело",
        "Гефест",
        "Комплектоборудование",
        "ПироХимУрал",
        "004_Global",
        "043_Гефест",
        "054_МЧС",
    )
    assert not any(value.casefold() in implementation.casefold() for value in forbidden)


def test_model_draft_is_not_eligible_for_automatic_matching() -> None:
    asyncio.run(_model_draft_is_not_eligible_for_automatic_matching())


def test_model_draft_name_comes_from_source_filename_not_first_sheet_proposal() -> None:
    assert draft_rule_name('price_list_sample.xlsx') == 'price_list_sample'
    assert draft_rule_name('catalog') == 'catalog'


async def _model_draft_is_not_eligible_for_automatic_matching() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory() as session:
        supplier = Supplier(code="proposal-test", name="Proposal test")
        session.add(supplier)
        await session.flush()
        rule = ParseRule(
            supplier_id=supplier.id,
            name="GPT draft",
            loader="xlsx",
            header_hash="same-structure",
            fingerprint={"loader": "xlsx", "sheet_signatures": []},
            recipe={"sheets": []},
            revision=1,
            status=ParseRuleStatus.draft.value,
            provenance=ParseRuleProvenance.model_proposed.value,
        )
        session.add(rule)
        await session.flush()
        assert await rules_service.find_exact_rule(
            session,
            "same-structure",
            "xlsx",
            supplier.id,
            project_id=ORIGINAL_PROJECT_ID,
        ) is None
        rule.status = ParseRuleStatus.active.value
        await session.flush()
        assert await rules_service.find_exact_rule(
            session,
            "same-structure",
            "xlsx",
            supplier.id,
            project_id=ORIGINAL_PROJECT_ID,
        ) is rule
    await engine.dispose()


def test_model_draft_idempotency_is_scoped_to_its_batch() -> None:
    asyncio.run(_model_draft_idempotency_is_scoped_to_its_batch())


async def _model_draft_idempotency_is_scoped_to_its_batch() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory() as session:
        supplier = Supplier(code="proposal-batch-test", name="Proposal batch test")
        session.add(supplier)
        await session.flush()
        kwargs = {
            "project_id": ORIGINAL_PROJECT_ID,
            "supplier_id": supplier.id,
            "name": "GPT draft",
            "loader": "xlsx",
            "header_hash": "same-structure",
            "fingerprint": {"loader": "xlsx", "sheet_signatures": []},
            "recipe": {"sheets": []},
            "created_from_version_id": None,
        }
        first = await rules_service.create_model_draft_rule(
            session, validation={"model_batch_id": "batch-1", "passed": True, "metrics": {}}, **kwargs
        )
        assert (
            await rules_service.create_model_draft_rule(
                session,
                validation={"model_batch_id": "batch-1", "passed": True, "metrics": {}},
                **kwargs,
            )
        ) is first
        try:
            await rules_service.create_model_draft_rule(
                session,
                validation={"model_batch_id": "batch-2", "passed": True, "metrics": {}},
                **kwargs,
            )
        except ValueError as exc:
            assert "другого model batch" in str(exc)
        else:  # pragma: no cover - protects recipe provenance
            raise AssertionError("Другой batch не должен получить чужой model draft")
    await engine.dispose()


def test_model_draft_can_review_a_structure_that_already_has_an_active_rule() -> None:
    asyncio.run(_model_draft_can_review_a_structure_that_already_has_an_active_rule())


async def _model_draft_can_review_a_structure_that_already_has_an_active_rule() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory() as session:
        supplier = Supplier(code="proposal-review", name="Proposal review")
        session.add(supplier)
        await session.flush()
        session.add(
            ParseRule(
                supplier_id=supplier.id,
                name="Current rule",
                loader="xlsx",
                header_hash="same-structure",
                fingerprint={"loader": "xlsx", "sheet_signatures": []},
                recipe={"sheets": []},
                revision=4,
                status=ParseRuleStatus.active.value,
                provenance=ParseRuleProvenance.user_confirmed.value,
            )
        )
        await session.flush()

        candidate = await rules_service.create_model_draft_rule(
            session,
            project_id=ORIGINAL_PROJECT_ID,
            supplier_id=supplier.id,
            name="DeepSeek candidate",
            loader="xlsx",
            header_hash="same-structure",
            fingerprint={"loader": "xlsx", "sheet_signatures": []},
            recipe={"sheets": []},
            created_from_version_id=None,
            validation={"model_batch_id": "review-batch", "passed": True, "metrics": {}},
        )

        assert candidate.status == ParseRuleStatus.draft.value
        assert candidate.provenance == ParseRuleProvenance.model_proposed.value
        assert candidate.revision == 5
    await engine.dispose()
