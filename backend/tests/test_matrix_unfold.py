from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.parsing.loaders import detect_loader, load_grids_for
from app.parsing.grid import Cell, SheetGrid
from app.parsing.matrix_unfold import (
    MatrixSchemaChangedError,
    build_matrix_rule_generation_prompt,
    build_matrix_section_contexts,
    compile_matrix_proposal,
    parse_matrix_proposal,
    parse_matrix_regions,
    rebind_matrix_regions,
)
from app.api.parse_rules import build_matrix_preview, rebind_matrix_recipe
from app.parsing.workbook_layout import parse_workbook_layout
from app.rule_proposal_service import (
    batch_prompt_version,
    build_attempt_specs,
    build_chat_completions_request,
)


SOURCE = (
    Path(__file__).parents[4]
    / "original"
    / "datasets"
    / "прайсы"
    / "price_list_sample.xlsx"
)


def _source_grids():
    data = SOURCE.read_bytes()
    loader = detect_loader(data, "renamed-upload.xlsx")
    return load_grids_for(loader, data, "renamed-upload.xlsx")


def test_profiler_groups_matrix_and_leaves_following_table_outside() -> None:
    contexts = build_matrix_section_contexts(_source_grids())

    assert len(contexts) == 2
    context = contexts[0]
    assert context["proposal_contract"] == "matrix-unfold-v1"
    assert context["region_start_row_index"] == 3
    assert context["region_end_row_index"] == 68
    assert len(context["blocks"]) == 9
    assert [block["kind"] for block in context["blocks"]] == [
        "one_axis_matrix",
        "one_axis_matrix",
        "one_axis_matrix",
        "two_axis_matrix",
        "two_axis_matrix",
        "two_axis_matrix",
        "two_axis_matrix",
        "two_axis_matrix",
        "one_axis_matrix",
    ]
    assert context["blocks"][-1]["kind"] == "one_axis_matrix"
    assert all(
        binding["row_index"] < context["region_end_row_index"]
        for binding in context["private_ref_map"].values()
    )

    signs = contexts[1]
    assert signs["sheet_name"] == "Обслуживание"
    assert [block["kind"] for block in signs["blocks"]] == [
        "two_axis_matrix",
        "two_axis_matrix",
        "two_axis_matrix",
    ]
    assert [len(block["private_data_row_indexes"]) for block in signs["blocks"]] == [6, 5, 5]


def test_categorical_matrix_emits_one_position_per_size_and_material_cell() -> None:
    grids = _source_grids()
    context = next(
        candidate
        for candidate in build_matrix_section_contexts(grids)
        if candidate["sheet_name"] == "Обслуживание"
    )
    payload = _proposal(context)
    for block in payload["blocks"]:
        block["row_axis"] = {
            "attribute_code": "size",
            "display_name": "Размер",
            "canonical_unit": None,
        }
        block["column_axis"] = {
            "attribute_code": "material",
            "display_name": "Материал",
            "canonical_unit": None,
        }
    region = compile_matrix_proposal(
        parse_matrix_proposal(payload), source_context=context, grids=grids
    )

    parsed = parse_matrix_regions(grids, [region])

    assert len(parsed.items) == 80
    first = parsed.items[0]
    assert first.source_name == "Знаки безопасности (ГОСТ 12.4.026-2015)*"
    assert {(attribute.attribute_code, attribute.raw_value) for attribute in first.attribute_values} == {
        ("size", "100х100"),
        ("material", "пленка ПП¹"),
    }


def test_profiler_accepts_one_proven_block_and_ignores_a_caption_without_prices() -> None:
    grid = SheetGrid(
        name="Matrix",
        rows=[
            [Cell("Product"), Cell(None), Cell("Diameter, mm"), Cell(None), Cell(None)],
            [Cell(None), Cell(None), Cell(100), Cell(200), Cell(300)],
            [
                Cell("Valves"),
                Cell("Valves", is_merged_fill=True),
                Cell("Valves", is_merged_fill=True),
                Cell("Valves", is_merged_fill=True),
                Cell("Valves", is_merged_fill=True),
            ],
            [Cell("Long product name"), Cell("A-1"), Cell(10), Cell(20), Cell(30)],
            [
                Cell("Notes"),
                Cell("Notes", is_merged_fill=True),
                Cell("Notes", is_merged_fill=True),
                Cell("Notes", is_merged_fill=True),
                Cell("Notes", is_merged_fill=True),
            ],
            [Cell("Long explanatory note"), Cell("N-1"), Cell("-"), Cell("-"), Cell("-")],
        ],
    )

    contexts = build_matrix_section_contexts([grid])

    assert len(contexts) == 1
    assert len(contexts[0]["blocks"]) == 1
    assert contexts[0]["blocks"][0]["kind"] == "one_axis_matrix"


def _shifted_simple_matrix(*, add_axis: bool = False) -> tuple[list[SheetGrid], dict]:
    original = SheetGrid(
        name="Matrix",
        rows=[
            [Cell("Product"), Cell(None), Cell("Diameter, mm"), Cell(None), Cell(None)],
            [Cell(None), Cell(None), Cell(100), Cell(200), Cell(300)],
            [
                Cell("Valves"),
                Cell("Valves", is_merged_fill=True),
                Cell("Valves", is_merged_fill=True),
                Cell("Valves", is_merged_fill=True),
                Cell("Valves", is_merged_fill=True),
            ],
            [Cell("Long product name"), Cell("A-1"), Cell(10), Cell(20), Cell(30)],
        ],
    )
    context = build_matrix_section_contexts([original])[0]
    region = compile_matrix_proposal(
        parse_matrix_proposal(_proposal(context)), source_context=context, grids=[original]
    )
    shifted_rows = [[Cell(None), *row] for row in original.rows]
    if add_axis:
        shifted_rows[1].append(Cell(400))
        shifted_rows[2].append(Cell("Valves", is_merged_fill=True))
        shifted_rows[3].append(Cell(40))
    return [SheetGrid(name=original.name, rows=shifted_rows)], region


def test_matrix_runtime_rebinds_same_axis_structure_after_columns_shift() -> None:
    shifted, region = _shifted_simple_matrix()

    result = parse_matrix_regions(shifted, [region])

    assert [item.source_name for item in result.items] == ["Long product name"] * 3
    assert [item.values[0].source_column_index for item in result.items] == [3, 4, 5]


def test_matrix_runtime_accepts_price_only_changes() -> None:
    shifted, region = _shifted_simple_matrix()
    shifted[0].rows[3][3].value = 110
    shifted[0].rows[3][4].value = 220
    shifted[0].rows[3][5].value = 330

    result = parse_matrix_regions(shifted, [region])

    assert [str(item.values[0].amount) for item in result.items] == [
        "110.00", "220.00", "330.00",
    ]


@pytest.mark.parametrize(
    ("raw_price", "expected"),
    [
        (
            "130-300",
            {"qualifier": "range", "amount_from": "130.00", "amount_to": "300.00"},
        ),
        (
            "от 1 000,00 р",
            {"qualifier": "from", "amount_from": "1000.00", "amount_to": None},
        ),
        (
            "50 тенге за 1 км",
            {"currency": "KZT", "basis_quantity": "1.00", "basis_unit": "км"},
        ),
    ],
)
def test_matrix_runtime_keeps_qualified_price_only_changes(
    raw_price: str, expected: dict[str, str | None]
) -> None:
    grid = SheetGrid(
        name="Matrix",
        rows=[
            [Cell("Product"), Cell(None), Cell("Diameter, mm"), Cell(None), Cell(None), Cell(None)],
            [Cell(None), Cell(None), Cell(100), Cell(200), Cell(300), Cell(400)],
            [
                Cell("Valves"),
                Cell("Valves", is_merged_fill=True),
                Cell("Valves", is_merged_fill=True),
                Cell("Valves", is_merged_fill=True),
                Cell("Valves", is_merged_fill=True),
                Cell("Valves", is_merged_fill=True),
            ],
            [Cell("Long product name"), Cell("A-1"), Cell(10), Cell(20), Cell(30), Cell(40)],
        ],
    )
    context = build_matrix_section_contexts([grid])[0]
    region = compile_matrix_proposal(
        parse_matrix_proposal(_proposal(context)), source_context=context, grids=[grid]
    )
    grid.rows[3][2].value = raw_price

    result = parse_matrix_regions([grid], [region])

    assert len(result.items) == 4
    price = next(
        item.values[0]
        for item in result.items
        if item.values[0].source_column_index == 2
    )
    assert price.raw_value == raw_price
    for field, wanted in expected.items():
        actual = getattr(price, field)
        assert (None if actual is None else str(actual)) == wanted


def test_matrix_preview_uses_rebound_rectangle_after_rows_shift() -> None:
    shifted, region = _shifted_simple_matrix()
    shifted[0].rows.insert(0, [Cell(None)])

    preview = build_matrix_preview(
        shifted,
        {"matrix_regions": [region]},
        source_section_id=region["source_section_id"],
    )

    assert preview["row_offset"] == region["region_start_row_index"] + 1
    assert {item["source_row"] for item in preview["items"]} == {5}


def test_matrix_runtime_reports_schema_change_when_axis_value_is_added() -> None:
    changed, region = _shifted_simple_matrix(add_axis=True)

    try:
        parse_matrix_regions(changed, [region])
    except MatrixSchemaChangedError as exc:
        assert "схема матрицы изменилась" in str(exc).casefold()
    else:
        raise AssertionError("matrix axis change must stop parsing")


def test_matrix_runtime_reports_schema_change_when_axis_semantics_change() -> None:
    shifted, region = _shifted_simple_matrix()
    shifted[0].rows[0][3].value = "Length, cm"

    with pytest.raises(MatrixSchemaChangedError, match="Схема матрицы изменилась"):
        parse_matrix_regions(shifted, [region])


def test_legacy_matrix_rule_fails_safe_until_it_is_rebound() -> None:
    changed, region = _shifted_simple_matrix(add_axis=True)
    region.pop("structure_signature")

    with pytest.raises(MatrixSchemaChangedError, match="Схема матрицы изменилась"):
        parse_matrix_regions(changed, [region])


def test_unsigned_legacy_matrix_rule_accepts_unchanged_workbook() -> None:
    grids, region = _shifted_simple_matrix()
    current_region = rebind_matrix_regions(grids, [region])[0]
    current_region.pop("structure_signature")

    result = parse_matrix_regions(grids, [current_region])

    assert len(result.items) == 3


def test_v1_matrix_signature_accepts_unchanged_workbook() -> None:
    grids, region = _shifted_simple_matrix()
    current_region = rebind_matrix_regions(grids, [region])[0]
    current_region["structure_signature"]["version"] = "matrix-structure-v1"

    result = parse_matrix_regions(grids, [current_region])

    assert len(result.items) == 3


def test_matrix_runtime_reports_schema_change_when_row_axis_value_is_added() -> None:
    grid = SheetGrid(
        name="Sizes",
        rows=[
            [Cell("Safety signs")],
            [Cell("Size"), Cell("Film"), Cell("Plastic"), Cell("Metal")],
            [Cell("100x100"), Cell(10), Cell(20), Cell(30)],
            [Cell("200x200"), Cell(40), Cell(50), Cell(60)],
        ],
    )
    context = build_matrix_section_contexts([grid])[0]
    region = compile_matrix_proposal(
        parse_matrix_proposal(_proposal(context)), source_context=context, grids=[grid]
    )
    grid.rows.append([Cell("300x300"), Cell(70), Cell(80), Cell(90)])

    with pytest.raises(MatrixSchemaChangedError, match="Схема матрицы изменилась"):
        parse_matrix_regions([grid], [region])


def test_matrix_only_rebind_accepts_new_axis_and_keeps_semantics() -> None:
    changed, region = _shifted_simple_matrix(add_axis=True)

    rebound = rebind_matrix_regions(changed, [region])
    result = parse_matrix_regions(changed, rebound)

    assert len(result.items) == 4
    assert rebound[0]["source_section_id"] == region["source_section_id"]
    assert rebound[0]["structure_signature"] != region["structure_signature"]


def test_matrix_recipe_rebind_preserves_every_non_matrix_rule_part() -> None:
    changed, region = _shifted_simple_matrix(add_axis=True)
    recipe = {
        "format": "workbook_layout_v1",
        "profile": "generated_matrix_v1",
        "sheets": [{"match": {"name_pattern": "Ordinary"}, "columns": []}],
        "options": {"currency": "RUB"},
        "ordinary_exclusions": ["Archive"],
        "matrix_regions": [region],
    }

    rebound, result = rebind_matrix_recipe(changed, recipe)

    assert rebound["sheets"] == recipe["sheets"]
    assert rebound["options"] == recipe["options"]
    assert rebound["ordinary_exclusions"] == recipe["ordinary_exclusions"]
    assert rebound["matrix_regions"] != recipe["matrix_regions"]
    assert len(result.items) == 4


def test_matrix_recipe_rebind_supports_price_new_hybrid_profile() -> None:
    changed, region = _shifted_simple_matrix(add_axis=True)
    recipe = {
        "format": "workbook_layout_v1",
        "profile": "price_new_v1",
        "sheets": [
            {
                "match": {"name_pattern": "Matrix", "or_index": 0},
                "skip": True,
                "columns": [],
            }
        ],
        "ordinary_exclusions": ["Matrix"],
        "hydrant_matrix": {},
        "service_matrix": {},
        "matrix_regions": [region],
    }

    rebound, result = rebind_matrix_recipe(changed, recipe)

    assert rebound["profile"] == "price_new_v1"
    assert rebound["hydrant_matrix"] == recipe["hydrant_matrix"]
    assert rebound["service_matrix"] == recipe["service_matrix"]
    assert rebound["matrix_regions"] != recipe["matrix_regions"]
    assert len(result.items) == 4


def test_matrix_prompt_contains_opaque_structure_not_source_coordinates_or_products() -> None:
    context = build_matrix_section_contexts(_source_grids())[0]
    prompt = build_matrix_rule_generation_prompt(
        source_context=context,
        loader="xlsx",
        model="test-model",
        attribute_catalog=[],
    )

    assert "Подст под гидрант" not in prompt
    assert "Тройник фланцевый" not in prompt
    assert "По запросу" not in prompt
    assert "4766" not in prompt
    assert "6469" not in prompt
    assert "price_new" not in prompt
    assert "row_index" not in prompt
    assert "column_index" not in prompt
    assert "private_ref_map" not in prompt
    assert '"block_ref"' in prompt
    assert '"cell_ref"' not in prompt
    assert '"column_track"' in prompt
    assert "Физические bindings уже определены source profiler" in prompt
    assert "requires_manual_review=true" in prompt
    assert "Наименование продукции из стали" in prompt
    assert "Диамерт прохода" in prompt
    assert "matrix-unfold-v2" in prompt
    assert len(prompt) < 6_000
    json.dumps(context, ensure_ascii=False)

    public_context = json.loads(prompt.split("\nСтруктурный контекст:\n", 1)[1])
    header_tracks = {
        cell["column_track"] for cell in public_context["semantic_header"]["cells"]
    }
    block_tracks = {
        cell["column_track"]
        for block in public_context["blocks"]
        for cell in block["column_axis"]
    }
    assert header_tracks & block_tracks
    two_axis = next(
        block for block in public_context["blocks"] if block["kind"] == "two_axis_matrix"
    )
    assert two_axis["row_axis_tracks"]
    assert two_axis["row_axis_unit_hint"] == "мм"
    assert two_axis["identity_mode"] == "block"
    assert two_axis["article_detected"] is True


def _proposal(context: dict) -> dict:
    blocks = []
    for source_block in context["blocks"]:
        has_row_axis = source_block["kind"] == "two_axis_matrix"
        blocks.append(
            {
                "block_ref": source_block["block_ref"],
                "row_axis": (
                    {
                        "attribute_code": "outlet_diameter",
                        "display_name": "Диаметр отвода",
                        "canonical_unit": "mm",
                    }
                    if has_row_axis
                    else None
                ),
                "column_axis": {
                    "attribute_code": "main_diameter",
                    "display_name": "Магистральный диаметр",
                    "canonical_unit": "mm",
                },
                "price_descriptor": {"kind": "other", "vat_mode": "unknown"},
            }
        )
    return {
        "schema_version": "matrix-unfold-proposal-v2",
        "decision": "propose",
        "rule_name": "Матричный прайс",
        "source_section_id": context["section_id"],
        "blocks": blocks,
        "unresolved": [],
        "confidence": 0.95,
        "requires_manual_review": True,
    }


def test_compiler_and_executor_emit_one_position_per_measure_cell_with_axes() -> None:
    grids = _source_grids()
    context = build_matrix_section_contexts(grids)[0]
    region = compile_matrix_proposal(
        parse_matrix_proposal(_proposal(context)),
        source_context=context,
        grids=grids,
    )

    result = parse_matrix_regions(grids, [region])

    target = next(
        item
        for item in result.items
        if item.source_row == 23
        and item.values[0].source_column_index == 15
    )
    assert target.source_name == "Тройник фланцевый ТФ"
    assert target.source_article == "ТФ"
    assert target.values[0].amount is not None
    assert str(target.values[0].amount) == "6469.00"
    assert {
        (attribute.attribute_code, attribute.raw_value, attribute.canonical_unit)
        for attribute in target.attribute_values
    } == {
        ("outlet_diameter", "100", "mm"),
        ("main_diameter", "250", "mm"),
    }
    coordinates = {
        (item.source_sheet, item.source_row, item.values[0].source_column_index)
        for item in result.items
    }
    assert len(coordinates) == len(result.items)
    assert result.rows_accepted == len(result.items)


def test_matrix_preview_keeps_source_grid_and_every_unfolded_position() -> None:
    grids = _source_grids()
    context = build_matrix_section_contexts(grids)[0]
    region = compile_matrix_proposal(
        parse_matrix_proposal(_proposal(context)), source_context=context, grids=grids
    )

    preview = build_matrix_preview(
        grids,
        {"matrix_regions": [region]},
        source_section_id=context["section_id"],
    )

    parsed = parse_matrix_regions(grids, [region])
    assert preview["sheet_name"] == context["sheet_name"]
    assert preview["row_offset"] == region["region_start_row_index"]
    assert preview["total_items"] == len(parsed.items)
    assert len(preview["items"]) == len(parsed.items)
    assert preview["rows"][0]["excel_row"] == region["region_start_row_index"] + 1
    assert preview["rows"][0]["cells"][0]["excel_column"] == "A"
    assert isinstance(preview["rows"][0]["cells"][0]["is_merged_fill"], bool)
    assert any(
        item["source_row"] == 23
        and item["prices"][0]["source_column_index"] == 15
        for item in preview["items"]
    )


def test_compiler_rejects_ref_from_another_block() -> None:
    grids = _source_grids()
    context = build_matrix_section_contexts(grids)[0]
    payload = _proposal(context)
    payload["blocks"][0]["block_ref"] = "b999"

    try:
        compile_matrix_proposal(
            parse_matrix_proposal(payload), source_context=context, grids=grids
        )
    except ValueError as exc:
        assert "покрывать все matrix blocks" in str(exc)
    else:
        raise AssertionError("cross-block ref must be rejected")


def test_schema_rejects_model_supplied_physical_binding() -> None:
    context = build_matrix_section_contexts(_source_grids())[0]
    payload = _proposal(context)
    payload["blocks"][0]["name_ref"] = "cell_c1"

    try:
        parse_matrix_proposal(payload)
    except ValueError as exc:
        assert "matrix-контракту" in str(exc)
    else:
        raise AssertionError("model must not supply deterministic physical bindings")


def test_compiler_requires_explicit_manual_review() -> None:
    grids = _source_grids()
    context = build_matrix_section_contexts(grids)[0]
    payload = _proposal(context)
    payload["requires_manual_review"] = False

    try:
        compile_matrix_proposal(
            parse_matrix_proposal(payload), source_context=context, grids=grids
        )
    except ValueError as exc:
        assert "requires_manual_review" in str(exc)
    else:
        raise AssertionError("matrix proposal must remain review-only")


def test_compiler_requires_row_axis_for_structural_two_axis_block() -> None:
    grids = _source_grids()
    context = build_matrix_section_contexts(grids)[0]
    payload = _proposal(context)
    two_axis = next(
        block
        for block in payload["blocks"]
        if next(
            source
            for source in context["blocks"]
            if source["block_ref"] == block["block_ref"]
        )["kind"]
        == "two_axis_matrix"
    )
    two_axis["row_axis"] = None

    try:
        compile_matrix_proposal(
            parse_matrix_proposal(payload), source_context=context, grids=grids
        )
    except ValueError as exc:
        assert "двухосного" in str(exc)
    else:
        raise AssertionError("two-axis matrix must require its row axis")


def test_compiler_derives_complete_column_axis_coverage() -> None:
    grids = _source_grids()
    context = build_matrix_section_contexts(grids)[0]
    region = compile_matrix_proposal(
        parse_matrix_proposal(_proposal(context)), source_context=context, grids=grids
    )

    for source_block, compiled_block in zip(context["blocks"], region["blocks"], strict=True):
        expected = {
            cell["column_track"]
            for cell in source_block["axis_header"]["cells"]
            if cell["value_kind"] == "axis_value"
        }
        assert len(compiled_block["column_axis_columns"]) == len(expected)


def test_compiler_preserves_detected_article_and_category_candidates() -> None:
    grids = _source_grids()
    context = build_matrix_section_contexts(grids)[0]
    region = compile_matrix_proposal(
        parse_matrix_proposal(_proposal(context)), source_context=context, grids=grids
    )

    assert region["blocks"][3]["article"] is not None
    assert region["blocks"][3]["category"] is not None


def test_compiler_uses_explicit_source_unit_for_row_axis() -> None:
    grids = _source_grids()
    context = build_matrix_section_contexts(grids)[0]
    payload = _proposal(context)
    two_axis = next(
        block
        for block in payload["blocks"]
        if next(
            source
            for source in context["blocks"]
            if source["block_ref"] == block["block_ref"]
        )["kind"]
        == "two_axis_matrix"
    )
    two_axis["row_axis"]["canonical_unit"] = None

    region = compile_matrix_proposal(
        parse_matrix_proposal(payload), source_context=context, grids=grids
    )
    compiled = next(
        block for block in region["blocks"] if block["block_ref"] == two_axis["block_ref"]
    )

    assert compiled["row_axis"]["canonical_unit"] == "mm"


def test_existing_batch_routes_each_matrix_region_to_matrix_handler() -> None:
    specs = build_attempt_specs(
        grids=_source_grids(), loader="xlsx", model="test-model", attribute_catalog=[]
    )

    matrix_specs = [spec for spec in specs if spec["proposal_contract"] == "matrix-unfold-v1"]
    assert len(matrix_specs) == 2
    assert {spec["input_context"]["sheet_name"] for spec in matrix_specs} == {
        "Подст под гидрант",
        "Обслуживание",
    }
    assert all(spec["prompt_version"] == "matrix-unfold-v2" for spec in matrix_specs)
    assert batch_prompt_version(specs) == "mixed-section-proposals-v1"
    target_tabular = [
        spec
        for spec in specs
        if spec["proposal_contract"] == "tabular-section-mapping-v7"
        and spec["input_context"]["sheet_name"] == "Подст под гидрант"
    ]
    assert len(target_tabular) == 1
    assert target_tabular[0]["input_context"]["section_start_row_index"] >= 68
    service_tabular = [
        spec["input_context"]
        for spec in specs
        if spec["proposal_contract"] == "tabular-section-mapping-v7"
        and spec["input_context"]["sheet_name"] == "Обслуживание"
    ]
    covered_service_rows = {
        row_index + 1
        for context in service_tabular
        for row_index in context["price_presence"]["candidate_row_indexes"]
    }
    assert {5, 6, 7, 8, 9, 10, 14, 15, 16} <= covered_service_rows


def test_request_body_selects_matrix_system_prompt_and_schema() -> None:
    body = build_chat_completions_request(
        prompt="matrix prompt",
        model="test-model",
        provider="openai",
        max_output_tokens=2048,
        proposal_contract="matrix-unfold-v1",
    )

    assert body["response_format"]["json_schema"]["name"] == "matrix_rule_proposal"
    assert "разворачивания одной матрицы" in body["messages"][0]["content"]
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["schema_version"]["const"] == "matrix-unfold-proposal-v2"


def test_generated_workbook_recipe_combines_matrix_and_ordinary_table() -> None:
    grids = _source_grids()
    context = build_matrix_section_contexts(grids)[0]
    region = compile_matrix_proposal(
        parse_matrix_proposal(_proposal(context)), source_context=context, grids=grids
    )
    recipe = {
        "format": "workbook_layout_v1",
        "profile": "generated_matrix_v1",
        "sheets": [
            {
                "match": {
                    "name_pattern": context["sheet_name"],
                    "or_index": context["sheet_index"],
                },
                "skip": False,
                "header_row": 68,
                "start_row": 69,
                "end_row": 75,
                "columns": [
                    {"index": 2, "role": "name"},
                    {"index": 6, "role": "unit"},
                    {
                        "index": 8,
                        "role": "price",
                        "price_descriptor": {"kind": "wholesale", "vat_mode": "unknown"},
                    },
                    {
                        "index": 10,
                        "role": "price",
                        "price_descriptor": {"kind": "retail", "vat_mode": "unknown"},
                    },
                ],
            }
        ],
        "options": {},
        "matrix_regions": [region],
    }

    result = parse_workbook_layout(grids, recipe)

    ordinary = [item for item in result.items if item.source_row in {70, 72, 74}]
    matrix = [item for item in result.items if item.source_section_id == context["section_id"]]
    assert len(ordinary) == 3
    assert matrix
    assert len(result.items) == len(ordinary) + len(matrix)
    assert result.rows_accepted == len(result.items)


def test_service_matrix_keeps_every_priced_row_outside_matrix_region() -> None:
    grids = _source_grids()
    context = next(
        candidate
        for candidate in build_matrix_section_contexts(grids)
        if candidate["sheet_name"] == "Обслуживание"
    )
    region = compile_matrix_proposal(
        parse_matrix_proposal(_proposal(context)), source_context=context, grids=grids
    )
    ordinary_columns = [
        {"index": 0, "role": "name"},
        {"index": 6, "role": "unit"},
        {
            "index": 7,
            "role": "price",
            "price_descriptor": {"kind": "other", "vat_mode": "unknown"},
        },
    ]
    recipe = {
        "format": "workbook_layout_v1",
        "profile": "generated_matrix_v1",
        "sheets": [
            {
                "match": {"name_pattern": "Обслуживание", "or_index": 16},
                "header_row": 3,
                "start_row": 4,
                "end_row": 12,
                "columns": ordinary_columns,
            },
            {
                "match": {"name_pattern": "Обслуживание", "or_index": 16},
                "header_row": 12,
                "start_row": 13,
                "end_row": 18,
                "columns": ordinary_columns,
            },
        ],
        "options": {},
        "matrix_regions": [region],
    }

    result = parse_workbook_layout(grids, recipe)

    ordinary_rows = {
        item.source_row
        for item in result.items
        if item.source_sheet == "Обслуживание"
        and item.source_section_id != context["section_id"]
    }
    assert ordinary_rows == {*range(5, 13), *range(14, 19)}
    assert any(
        item.source_section_id == context["section_id"] for item in result.items
    )
