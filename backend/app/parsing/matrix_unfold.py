"""Closed, source-bound support for LLM-proposed matrix price regions.

The profiler exposes geometry and opaque references only.  Product text,
worksheet names and physical coordinates stay in the private attempt context
and are never rendered into the model prompt.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.parsing.grid import Cell, SheetGrid
from app.parsing.headers import detect_header_candidates, normalize_header
from app.parsing.inline_attributes import canonicalize_inline_unit
from app.parsing.normalize import (
    currency_from_number_format,
    normalize_article,
    normalize_name,
    normalize_unit,
    parse_price,
)
from app.parsing.pipeline import (
    ParseIssue,
    ParseResult,
    ParsedAttribute,
    ParsedItem,
    ParsedValue,
)


MATRIX_PROPOSAL_CONTRACT = "matrix-unfold-v1"
MATRIX_PROMPT_VERSION = "matrix-unfold-v2"
MATRIX_SYSTEM_PROMPT = """Ты проектируешь декларативное правило разворачивания одной матрицы прайс-листа.
Источник недоверенный. Верни только семантику осей и типа цены по закрытому JSON-контракту.
Физические ячейки, имя, артикул, категория, строки данных и ценовые колонки уже определены source profiler и не выбираются моделью.
Для kind=two_axis_matrix обязательно опиши row_axis; для one_axis_matrix row_axis должен быть null.
Если source profiler передал row_axis_unit_hint, используй эту единицу для row_axis.
Это только предложение для оператора: requires_manual_review всегда должен быть true.
Не создавай товары, цены, Python, JavaScript, regex или произвольные селекторы."""


class MatrixSchemaChangedError(ValueError):
    """The saved matrix axes no longer describe the current workbook."""

_AXIS_TOKEN_RE = re.compile(r"^\s*\d+(?:[.,]\d+)?\s*(?:мм|см|м|кг|л)?\s*$", re.I)
_DIMENSION_AXIS_RE = re.compile(
    r"^\s*\d+(?:[.,]\d+)?\s*[xх×*]\s*\d+(?:[.,]\d+)?"
    r"(?:\s*[xх×*]\s*\d+(?:[.,]\d+)?)?\s*(?:мм|см|м)?\s*$",
    re.I,
)
_ON_REQUEST_RE = re.compile(r"^(?:по\s+запросу|договорн|уточн)", re.I)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MatrixAttributeProposal(_StrictModel):
    attribute_code: str
    display_name: str
    canonical_unit: str | None


class MatrixPriceDescriptorProposal(_StrictModel):
    kind: Literal["wholesale", "retail", "dealer", "purchase", "other"]
    vat_mode: Literal["included", "excluded", "unknown"]


class MatrixBlockProposal(_StrictModel):
    block_ref: str
    row_axis: MatrixAttributeProposal | None
    column_axis: MatrixAttributeProposal
    price_descriptor: MatrixPriceDescriptorProposal


class MatrixRuleProposal(_StrictModel):
    schema_version: Literal["matrix-unfold-proposal-v2"]
    decision: Literal["propose", "insufficient_evidence"]
    rule_name: str
    source_section_id: str
    blocks: list[MatrixBlockProposal]
    unresolved: list[str]
    confidence: float
    requires_manual_review: bool


def _master_cells(row: list[Cell]) -> list[tuple[int, Cell]]:
    return [
        (index, cell)
        for index, cell in enumerate(row)
        if not cell.is_merged_fill
        and cell.value is not None
        and str(cell.value).strip()
    ]


def _axis_like(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return 0 <= float(value) <= 5000
    return bool(_AXIS_TOKEN_RE.fullmatch(str(value)))


def _row_axis_like(value: object) -> bool:
    return _axis_like(value) or bool(_DIMENSION_AXIS_RE.fullmatch(str(value)))


def _price_like(value: object) -> bool:
    parsed = parse_price(value, allow_qualified=True)
    return parsed.status == "ok" or parsed.status == "on_request"


def _axis_columns(row: list[Cell]) -> list[int]:
    return [index for index, cell in _master_cells(row) if _axis_like(cell.value)]


def _has_following_matrix_values(
    grid: SheetGrid, row_index: int, axis_columns: list[int]
) -> bool:
    for candidate in grid.rows[row_index + 1 : row_index + 4]:
        values = 0
        for column in axis_columns:
            if column >= len(candidate):
                continue
            cell = candidate[column]
            if not cell.is_merged_fill and _price_like(cell.value):
                values += 1
        if values >= 3:
            return True
    return False


def _has_block_measure_values(
    grid: SheetGrid, row_indexes: list[int], measure_columns: set[int]
) -> bool:
    return any(
        column_index < len(grid.rows[row_index])
        and not grid.rows[row_index][column_index].is_merged_fill
        and _price_like(grid.rows[row_index][column_index].value)
        for row_index in row_indexes
        for column_index in measure_columns
    )


def _has_later_matrix_values_in_block(
    grid: SheetGrid,
    axis_row_index: int,
    row_indexes: list[int],
    axis_columns: list[int],
) -> bool:
    return any(
        sum(
            1
            for column_index in axis_columns
            if column_index < len(grid.rows[row_index])
            and not grid.rows[row_index][column_index].is_merged_fill
            and _price_like(grid.rows[row_index][column_index].value)
        )
        >= 3
        for row_index in row_indexes
        if row_index > axis_row_index
    )


def _is_wide_caption(row: list[Cell], n_cols: int) -> bool:
    masters = _master_cells(row)
    if len(masters) != 1 or not isinstance(masters[0][1].value, str):
        return False
    filled = sum(
        1
        for cell in row
        if cell.value is not None and str(cell.value).strip()
    )
    return filled >= max(4, n_cols // 2)


def _kind(value: object) -> str:
    if _ON_REQUEST_RE.match(str(value).strip()):
        return "on_request"
    if _axis_like(value):
        return "axis_value"
    if isinstance(value, str):
        return "text"
    return "number"


def _masked_text_shape(value: object) -> str:
    text = " ".join(str(value).split())
    if (
        1 <= len(text) <= 24
        and text.upper() == text
        and re.fullmatch(r"[\w./=+\- ]+", text, flags=re.UNICODE)
    ):
        return "short_code"
    if len(text) <= 12:
        return "short_label"
    return "long_text"


def _section_id(identity: object) -> str:
    material = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "matrix_" + sha256(material.encode("utf-8")).hexdigest()[:20]


def _cell_ref(
    *,
    sheet_index: int,
    row_index: int,
    column_index: int,
    refs: dict[str, dict[str, int]],
) -> str:
    ref = f"cell_c{len(refs) + 1}"
    refs[ref] = {
        "sheet_index": sheet_index,
        "row_index": row_index,
        "column_index": column_index,
    }
    return ref


def _public_row(
    *,
    grid: SheetGrid,
    sheet_index: int,
    row_index: int,
    refs: dict[str, dict[str, int]],
    column_refs: dict[int, str],
    row_refs: dict[int, str],
    projection: Literal["semantic_header", "axis_header", "caption", "data"] = "data",
    exclude_columns: set[int] | None = None,
) -> dict[str, Any]:
    cells = []
    for column_index, cell in _master_cells(grid.rows[row_index]):
        if exclude_columns and column_index in exclude_columns:
            continue
        source_kind = _kind(cell.value)
        if projection == "semantic_header":
            value_kind = "header_label" if isinstance(cell.value, str) else "header_value"
            public_value = str(cell.value)
        elif projection == "axis_header" and _axis_like(cell.value):
            value_kind = "axis_value"
            public_value = str(cell.value)
        else:
            value_kind = (
                "text_candidate"
                if isinstance(cell.value, str) and source_kind != "on_request"
                else "numeric_candidate"
            )
            public_value = "[MASKED]"
        public_cell = {
            "cell_ref": _cell_ref(
                sheet_index=sheet_index,
                row_index=row_index,
                column_index=column_index,
                refs=refs,
            ),
            "column_track": column_refs.setdefault(
                column_index, f"col_k{len(column_refs) + 1}"
            ),
            "value_kind": value_kind,
            "merged_span": sum(
                1
                for following in grid.rows[row_index][column_index + 1 :]
                if following.is_merged_fill and following.value == cell.value
            ),
        }
        if public_value != "[MASKED]":
            public_cell["value"] = public_value
        text_shape = (
            _masked_text_shape(cell.value) if value_kind == "text_candidate" else None
        )
        if text_shape is not None:
            public_cell["text_shape"] = text_shape
            if projection == "axis_header" and text_shape == "short_label":
                # A compact unit heading is structural axis metadata, not a
                # product value.  Keeping it on the opaque column track lets
                # later matrix blocks inherit an explicit source unit.
                public_cell["value"] = str(cell.value)
        cells.append(public_cell)
    return {
        "row_track": row_refs.setdefault(row_index, f"row_r{len(row_refs) + 1}"),
        "cells": cells,
    }


def _refs_for_columns(
    row: dict[str, Any],
    column_indexes: set[int],
    refs: dict[str, dict[str, int]],
) -> list[str]:
    return [
        str(cell["cell_ref"])
        for cell in row.get("cells", [])
        if isinstance(cell, dict)
        and isinstance(cell.get("cell_ref"), str)
        and refs.get(str(cell["cell_ref"]), {}).get("column_index") in column_indexes
    ]


def _categorical_matrix_contexts(grids: list[SheetGrid]) -> list[dict[str, Any]]:
    """Find dimension-by-variant matrices with text column headings."""

    contexts: list[dict[str, Any]] = []
    for sheet_index, grid in enumerate(grids):
        for header_row_index, header_row in enumerate(grid.rows):
            masters = _master_cells(header_row)
            if len(masters) < 4:
                continue
            row_axis_column = masters[0][0]
            measure_columns = [
                column_index
                for column_index, cell in masters[1:]
                if isinstance(cell.value, str) and cell.value.strip()
            ]
            if len(measure_columns) < 3:
                continue

            def is_data_row(row_index: int) -> bool:
                row = grid.rows[row_index]
                return (
                    row_axis_column < len(row)
                    and not row[row_axis_column].is_merged_fill
                    and _row_axis_like(row[row_axis_column].value)
                    and sum(
                        1
                        for column_index in measure_columns
                        if column_index < len(row)
                        and not row[column_index].is_merged_fill
                        and _price_like(row[column_index].value)
                    )
                    >= 3
                )

            initial_rows = [
                row_index
                for row_index in range(
                    header_row_index + 1,
                    min(len(grid.rows), header_row_index + 4),
                )
                if is_data_row(row_index)
            ]
            if len(initial_rows) < 2:
                continue
            caption_row_index = next(
                (
                    row_index
                    for row_index in range(
                        header_row_index - 1,
                        max(-1, header_row_index - 4),
                        -1,
                    )
                    if len(_master_cells(grid.rows[row_index])) == 1
                    and isinstance(_master_cells(grid.rows[row_index])[0][1].value, str)
                ),
                None,
            )
            if caption_row_index is None:
                continue

            grouped_rows: list[tuple[int, list[int]]] = []
            current_caption = caption_row_index
            current_rows: list[int] = []
            row_index = header_row_index + 1
            while row_index < len(grid.rows):
                if is_data_row(row_index):
                    current_rows.append(row_index)
                    row_index += 1
                    continue
                row_masters = _master_cells(grid.rows[row_index])
                next_is_data = row_index + 1 < len(grid.rows) and is_data_row(row_index + 1)
                if (
                    len(row_masters) == 1
                    and isinstance(row_masters[0][1].value, str)
                    and next_is_data
                ):
                    if current_rows:
                        grouped_rows.append((current_caption, current_rows))
                    current_caption = row_index
                    current_rows = []
                    row_index += 1
                    continue
                break
            if current_rows:
                grouped_rows.append((current_caption, current_rows))
            if not grouped_rows:
                continue

            refs: dict[str, dict[str, int]] = {}
            column_refs: dict[int, str] = {}
            row_refs: dict[int, str] = {}
            semantic_header = _public_row(
                grid=grid,
                sheet_index=sheet_index,
                row_index=header_row_index,
                refs=refs,
                column_refs=column_refs,
                row_refs=row_refs,
                projection="semantic_header",
            )
            blocks: list[dict[str, Any]] = []
            for group_caption, data_rows in grouped_rows:
                caption = _public_row(
                    grid=grid,
                    sheet_index=sheet_index,
                    row_index=group_caption,
                    refs=refs,
                    column_refs=column_refs,
                    row_refs=row_refs,
                    projection="caption",
                )
                axis_header = _public_row(
                    grid=grid,
                    sheet_index=sheet_index,
                    row_index=header_row_index,
                    refs=refs,
                    column_refs=column_refs,
                    row_refs=row_refs,
                    projection="semantic_header",
                )
                representative_rows = [
                    _public_row(
                        grid=grid,
                        sheet_index=sheet_index,
                        row_index=candidate,
                        refs=refs,
                        column_refs=column_refs,
                        row_refs=row_refs,
                        exclude_columns=set(measure_columns),
                    )
                    for candidate in data_rows[:2]
                ]
                caption_refs = _refs_for_columns(caption, {row_axis_column}, refs)
                blocks.append(
                    {
                        "block_ref": f"b{len(blocks) + 1}",
                        "kind": "two_axis_matrix",
                        "caption": caption,
                        "axis_header": axis_header,
                        "representative_rows": representative_rows,
                        "private_data_row_indexes": data_rows,
                        "role_refs": {
                            "name_candidate": caption_refs,
                            "category_candidate": caption_refs,
                            "row_axis_candidate": [
                                ref
                                for row in representative_rows
                                for ref in _refs_for_columns(row, {row_axis_column}, refs)
                            ],
                            "column_axis_candidate": _refs_for_columns(
                                axis_header, set(measure_columns), refs
                            ),
                        },
                    }
                )
            region_start = min(caption for caption, _ in grouped_rows)
            region_end = max(row for _, rows in grouped_rows for row in rows) + 1
            identity = {
                "sheet_index": sheet_index,
                "region_start_row_index": region_start,
                "region_end_row_index": region_end,
                "blocks": [block["block_ref"] for block in blocks],
            }
            contexts.append(
                {
                    "schema_version": MATRIX_PROMPT_VERSION,
                    "proposal_contract": MATRIX_PROPOSAL_CONTRACT,
                    "section_id": _section_id(identity),
                    "loader_scope": "matrix",
                    "sheet_index": sheet_index,
                    "sheet_name": grid.name,
                    "region_start_row_index": region_start,
                    "region_end_row_index": region_end,
                    "semantic_header": semantic_header,
                    "blocks": blocks,
                    "private_ref_map": refs,
                }
            )
            break
    return contexts


def build_matrix_section_contexts(grids: list[SheetGrid]) -> list[dict[str, Any]]:
    """Find conservative, non-overlapping matrix candidates in loaded grids."""

    contexts: list[dict[str, Any]] = []
    for sheet_index, grid in enumerate(grids):
        axis_headers = []
        for row_index, row in enumerate(grid.rows):
            columns = _axis_columns(row)
            if len(columns) >= 3 and _has_following_matrix_values(grid, row_index, columns):
                axis_headers.append(row_index)
        if not axis_headers:
            continue
        first_axis = axis_headers[0]
        following_headers = [
            detection.header_row_index
            for detection in detect_header_candidates(grid)
            if detection.header_row_index is not None
            and detection.header_row_index > first_axis
        ]
        region_end = min(following_headers) if following_headers else len(grid.rows)
        captions = [
            row_index
            for row_index in range(first_axis + 1, region_end)
            if _is_wide_caption(grid.rows[row_index], grid.n_cols)
        ]
        if not captions:
            continue

        refs: dict[str, dict[str, int]] = {}
        column_refs: dict[int, str] = {}
        row_refs: dict[int, str] = {}
        semantic_header = _public_row(
            grid=grid,
            sheet_index=sheet_index,
            row_index=max(0, first_axis - 1),
            refs=refs,
            column_refs=column_refs,
            row_refs=row_refs,
            projection="semantic_header",
        )
        blocks: list[dict[str, Any]] = []
        for block_index, caption_row in enumerate(captions):
            block_end = captions[block_index + 1] if block_index + 1 < len(captions) else region_end
            row_indexes = [
                index
                for index in range(caption_row + 1, block_end)
                if _master_cells(grid.rows[index])
            ]
            if not row_indexes:
                continue
            local_axis_rows = [
                index
                for index in row_indexes
                if len(_axis_columns(grid.rows[index])) >= 3
                and _has_later_matrix_values_in_block(
                    grid,
                    index,
                    row_indexes,
                    _axis_columns(grid.rows[index]),
                )
            ]
            axis_row = local_axis_rows[0] if local_axis_rows else first_axis
            measure_columns = set(_axis_columns(grid.rows[axis_row]))
            data_rows = [index for index in row_indexes if index != axis_row]
            if not data_rows and axis_row != row_indexes[0]:
                data_rows = row_indexes
            if not _has_block_measure_values(grid, data_rows, measure_columns):
                continue
            has_row_axis = any(
                len(
                    [
                        column
                        for column in _axis_columns(grid.rows[index])
                        if column not in measure_columns
                    ]
                )
                == 1
                for index in data_rows
            )
            blocks.append(
                {
                    "block_ref": f"b{len(blocks) + 1}",
                    "kind": "two_axis_matrix" if has_row_axis else "one_axis_matrix",
                    "caption": _public_row(
                        grid=grid,
                        sheet_index=sheet_index,
                        row_index=caption_row,
                        refs=refs,
                        column_refs=column_refs,
                        row_refs=row_refs,
                        projection="caption",
                    ),
                    "axis_header": _public_row(
                        grid=grid,
                        sheet_index=sheet_index,
                        row_index=axis_row,
                        refs=refs,
                        column_refs=column_refs,
                        row_refs=row_refs,
                        projection="axis_header",
                    ),
                    "representative_rows": [
                        _public_row(
                            grid=grid,
                            sheet_index=sheet_index,
                            row_index=index,
                            refs=refs,
                            column_refs=column_refs,
                            row_refs=row_refs,
                            exclude_columns=measure_columns,
                        )
                        for index in data_rows[:2]
                    ],
                    "private_data_row_indexes": data_rows,
                }
            )

        if not blocks:
            continue
        start = max(0, first_axis - 1)
        identity = {
            "sheet_index": sheet_index,
            "region_start_row_index": start,
            "region_end_row_index": region_end,
            "blocks": [block["block_ref"] for block in blocks],
        }
        contexts.append(
            {
                "schema_version": MATRIX_PROMPT_VERSION,
                "proposal_contract": MATRIX_PROPOSAL_CONTRACT,
                "section_id": _section_id(identity),
                "loader_scope": "matrix",
                "sheet_index": sheet_index,
                "sheet_name": grid.name,
                "region_start_row_index": start,
                "region_end_row_index": region_end,
                "semantic_header": semantic_header,
                "blocks": blocks,
                "private_ref_map": refs,
            }
        )
    for candidate in _categorical_matrix_contexts(grids):
        if any(
            existing["sheet_index"] == candidate["sheet_index"]
            and candidate["region_start_row_index"] < existing["region_end_row_index"]
            and existing["region_start_row_index"] < candidate["region_end_row_index"]
            for existing in contexts
        ):
            continue
        contexts.append(candidate)
    contexts.sort(
        key=lambda context: (
            int(context["sheet_index"]),
            int(context["region_start_row_index"]),
        )
    )
    return contexts


def _axis_unit_hints(source_context: dict[str, Any]) -> dict[str, str]:
    hints: dict[str, str] = {}
    conflicts: set[str] = set()
    for block in source_context.get("blocks", []):
        if not isinstance(block, dict):
            continue
        for cell in (block.get("axis_header") or {}).get("cells", []):
            if not isinstance(cell, dict) or cell.get("text_shape") != "short_label":
                continue
            track = cell.get("column_track")
            value = cell.get("value")
            if not isinstance(track, str) or not isinstance(value, str) or not value.strip():
                continue
            normalized = " ".join(value.split())
            if track in hints and hints[track].casefold() != normalized.casefold():
                conflicts.add(track)
            else:
                hints[track] = normalized
    return {track: value for track, value in hints.items() if track not in conflicts}


def _row_axis_unit_hint(
    source_context: dict[str, Any], block: dict[str, Any]
) -> str | None:
    hints = _axis_unit_hints(source_context)
    values = {
        hints[track]
        for track in _role_tracks(block, "row_axis_candidate")
        if track in hints
    }
    return next(iter(values)) if len(values) == 1 else None


def _public_context(source_context: dict[str, Any]) -> dict[str, Any]:
    semantic_header = source_context.get("semantic_header")
    public_header = {
        "cells": [
            {
                key: value
                for key, value in cell.items()
                if key in {"column_track", "value_kind", "value", "merged_span"}
            }
            for cell in (
                semantic_header.get("cells", [])
                if isinstance(semantic_header, dict)
                else []
            )
            if isinstance(cell, dict)
        ]
    }
    blocks = [
        {
            "block_ref": block.get("block_ref"),
            "kind": block.get("kind"),
            "identity_mode": _detected_identity_mode(block),
            "article_detected": bool(_role_refs(block, "article_candidate")),
            "category_detected": bool(_role_refs(block, "category_candidate")),
            "row_axis_tracks": _role_tracks(block, "row_axis_candidate"),
            "row_axis_unit_hint": _row_axis_unit_hint(source_context, block),
            "column_axis": [
                {
                    key: value
                    for key, value in cell.items()
                    if key in {"column_track", "value"}
                }
                for cell in (block.get("axis_header") or {}).get("cells", [])
                if isinstance(cell, dict)
                and cell.get("cell_ref") in _role_refs(block, "column_axis_candidate")
            ],
            "data_row_count": len(block.get("private_data_row_indexes", [])),
        }
        for block in source_context.get("blocks", [])
        if isinstance(block, dict)
    ]
    return {
        "section_id": source_context.get("section_id"),
        "semantic_header": public_header,
        "blocks": blocks,
    }


def build_matrix_rule_generation_prompt(
    *,
    source_context: dict[str, Any],
    loader: str,
    model: str,
    attribute_catalog: list[dict[str, Any]] | None = None,
) -> str:
    """Render only the safe public projection of one matrix candidate."""

    del loader, model
    catalog = sorted(attribute_catalog or [], key=lambda item: str(item.get("code") or ""))
    return (
        "Контракт предложения: matrix-unfold-v2. Для каждого block_ref опиши только "
        "семантику осей и типа цены. Физические bindings уже определены source profiler. "
        "Для `two_axis_matrix` row_axis обязателен, для `one_axis_matrix` он равен null. "
        "Если `row_axis_unit_hint` задан, это явная единица оси строк из источника. "
        "Установи `requires_manual_review=true`: правило всегда подтверждает оператор. "
        "Не возвращай refs, значения товаров или цен. Каталог характеристик: "
        + json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
        + "\nСтруктурный контекст:\n"
        + json.dumps(_public_context(source_context), ensure_ascii=False, separators=(",", ":"))
    )


def matrix_proposal_json_schema() -> dict[str, Any]:
    schema = MatrixRuleProposal.model_json_schema()

    def close(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
                properties = node.get("properties")
                if isinstance(properties, dict):
                    node["required"] = list(properties)
            node.pop("default", None)
            for value in node.values():
                close(value)
        elif isinstance(node, list):
            for value in node:
                close(value)

    close(schema)
    return schema


def parse_matrix_proposal(payload: object) -> MatrixRuleProposal:
    try:
        return MatrixRuleProposal.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(
            f"Ответ модели не соответствует matrix-контракту: {exc.errors()[0]['msg']}"
        ) from exc


def _binding(context: dict[str, Any], ref: str) -> dict[str, int]:
    bindings = context.get("private_ref_map")
    binding = bindings.get(ref) if isinstance(bindings, dict) else None
    if not isinstance(binding, dict) or not all(
        isinstance(binding.get(key), int)
        for key in ("sheet_index", "row_index", "column_index")
    ):
        raise ValueError(f"Неизвестный source ref: {ref}")
    return binding


def _block_refs(block: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    for row_key in ("caption", "axis_header"):
        row = block.get(row_key)
        if isinstance(row, dict):
            refs.update(
                str(cell["cell_ref"])
                for cell in row.get("cells", [])
                if isinstance(cell, dict) and isinstance(cell.get("cell_ref"), str)
            )
    for row in block.get("representative_rows", []):
        if isinstance(row, dict):
            refs.update(
                str(cell["cell_ref"])
                for cell in row.get("cells", [])
                if isinstance(cell, dict) and isinstance(cell.get("cell_ref"), str)
            )
    return refs


def _role_refs(block: dict[str, Any], role_hint: str) -> set[str]:
    explicit = block.get("role_refs")
    if isinstance(explicit, dict) and isinstance(explicit.get(role_hint), list):
        return {
            str(ref)
            for ref in explicit[role_hint]
            if isinstance(ref, str)
        }
    rows_by_role = {
        "category_candidate": [block.get("caption")],
        "column_axis_candidate": [block.get("axis_header")],
        "row_axis_candidate": list(block.get("representative_rows", [])),
        "name_candidate": [
            block.get("axis_header"),
            *block.get("representative_rows", []),
        ],
        "article_candidate": [
            block.get("axis_header"),
            *block.get("representative_rows", []),
        ],
        "unit_candidate": [
            block.get("axis_header"),
            *block.get("representative_rows", []),
        ],
    }
    refs: set[str] = set()
    for row in rows_by_role[role_hint]:
        if not isinstance(row, dict):
            continue
        refs.update(
            str(cell["cell_ref"])
            for cell in row.get("cells", [])
            if isinstance(cell, dict)
            and isinstance(cell.get("cell_ref"), str)
            and (
                (role_hint == "category_candidate")
                or (
                    role_hint == "column_axis_candidate"
                    and cell.get("value_kind") == "axis_value"
                )
                or (
                    role_hint == "row_axis_candidate"
                    and cell.get("value_kind") == "numeric_candidate"
                )
                or (
                    role_hint == "name_candidate"
                    and cell.get("text_shape") == "long_text"
                )
                or (
                    role_hint == "article_candidate"
                    and cell.get("text_shape") == "short_code"
                )
                or (
                    role_hint == "unit_candidate"
                    and cell.get("text_shape") == "short_label"
                )
            )
        )
    return refs


def _all_public_cells(block: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [block.get("caption"), block.get("axis_header")]
    rows.extend(block.get("representative_rows", []))
    return [
        cell
        for row in rows
        if isinstance(row, dict)
        for cell in row.get("cells", [])
        if isinstance(cell, dict)
    ]


def _role_tracks(block: dict[str, Any], role: str) -> list[str]:
    role_refs = _role_refs(block, role)
    return list(
        dict.fromkeys(
            str(cell["column_track"])
            for cell in _all_public_cells(block)
            if cell.get("cell_ref") in role_refs
            and isinstance(cell.get("column_track"), str)
        )
    )


def _detected_identity_mode(block: dict[str, Any]) -> Literal["block", "row"]:
    name_refs = _role_refs(block, "name_candidate")
    representative_refs = {
        str(cell["cell_ref"])
        for row in block.get("representative_rows", [])
        if isinstance(row, dict)
        for cell in row.get("cells", [])
        if isinstance(cell, dict) and isinstance(cell.get("cell_ref"), str)
    }
    return "row" if name_refs & representative_refs else "block"


def _detected_binding(
    *,
    source_context: dict[str, Any],
    source_block: dict[str, Any],
    role: str,
    required: bool,
) -> dict[str, int] | None:
    refs = _role_refs(source_block, role)
    if not refs:
        if required:
            raise ValueError(
                f"Source profiler не нашёл однозначный {role} в блоке {source_block.get('block_ref')}"
            )
        return None
    bindings = [_binding(source_context, ref) for ref in refs]
    columns = {binding["column_index"] for binding in bindings}
    if len(columns) != 1:
        raise ValueError(
            f"Source profiler нашёл несколько колонок {role} в блоке {source_block.get('block_ref')}"
        )
    return min(bindings, key=lambda binding: binding["row_index"])


def compile_matrix_proposal(
    proposal: MatrixRuleProposal,
    *,
    source_context: dict[str, Any],
    grids: list[SheetGrid],
) -> dict[str, Any]:
    """Resolve opaque refs and reject any proposal that escapes its block."""

    if proposal.decision != "propose":
        raise ValueError("Матричная секция не содержит подтверждённого предложения")
    if not proposal.requires_manual_review:
        raise ValueError("requires_manual_review должен быть true")
    if proposal.source_section_id != source_context.get("section_id"):
        raise ValueError("source_section_id не соответствует исходной секции")
    sheet_index = source_context.get("sheet_index")
    if not isinstance(sheet_index, int) or not (0 <= sheet_index < len(grids)):
        raise ValueError("Исходный лист матричной секции не найден")
    source_blocks = {
        str(block.get("block_ref")): block
        for block in source_context.get("blocks", [])
        if isinstance(block, dict) and isinstance(block.get("block_ref"), str)
    }
    proposed_refs = [block.block_ref for block in proposal.blocks]
    if len(proposed_refs) != len(set(proposed_refs)):
        raise ValueError("Предложение содержит дублирующий matrix block")
    if set(proposed_refs) != set(source_blocks):
        raise ValueError("Предложение должно покрывать все matrix blocks секции")

    compiled_blocks: list[dict[str, Any]] = []
    used_measures: set[tuple[int, int]] = set()
    grid = grids[sheet_index]
    for proposed in proposal.blocks:
        source_block = source_blocks[proposed.block_ref]
        source_kind = source_block.get("kind")
        if source_kind == "two_axis_matrix" and proposed.row_axis is None:
            raise ValueError(
                f"Для структурно двухосного блока {proposed.block_ref} обязательна ось строк"
            )
        if source_kind == "one_axis_matrix" and proposed.row_axis is not None:
            raise ValueError(
                f"Для структурно одноосного блока {proposed.block_ref} ось строк недопустима"
            )

        name = _detected_binding(
            source_context=source_context,
            source_block=source_block,
            role="name_candidate",
            required=True,
        )
        article = _detected_binding(
            source_context=source_context,
            source_block=source_block,
            role="article_candidate",
            required=False,
        )
        category = _detected_binding(
            source_context=source_context,
            source_block=source_block,
            role="category_candidate",
            required=True,
        )
        row_axis = _detected_binding(
            source_context=source_context,
            source_block=source_block,
            role="row_axis_candidate",
            required=source_kind == "two_axis_matrix",
        )
        column_bindings = sorted(
            (
                _binding(source_context, ref)
                for ref in _role_refs(source_block, "column_axis_candidate")
            ),
            key=lambda binding: binding["column_index"],
        )
        if not column_bindings:
            raise ValueError(f"У блока {proposed.block_ref} отсутствует ось колонок")
        unit = None
        axis_rows = {binding["row_index"] for binding in column_bindings}
        if len(axis_rows) != 1:
            raise ValueError("Все значения оси колонок должны быть из одной строки")
        measure_columns = [binding["column_index"] for binding in column_bindings]
        if len(measure_columns) != len(set(measure_columns)):
            raise ValueError("Ось колонок содержит дубли")
        if row_axis is not None and row_axis["column_index"] in measure_columns:
            raise ValueError("Ось строк не может совпадать с ценовой колонкой")
        data_rows = source_block.get("private_data_row_indexes")
        if not isinstance(data_rows, list) or not all(isinstance(row, int) for row in data_rows):
            raise ValueError("Контекст блока не содержит проверенных строк данных")
        if row_axis is not None:
            row_axis_values = [
                grid.rows[row_index][row_axis["column_index"]].value
                for row_index in data_rows
                if 0 <= row_index < len(grid.rows)
                and row_axis["column_index"] < len(grid.rows[row_index])
                and not grid.rows[row_index][row_axis["column_index"]].is_merged_fill
                and grid.rows[row_index][row_axis["column_index"]].value is not None
            ]
            if not row_axis_values or any(
                not _row_axis_like(value) for value in row_axis_values
            ):
                raise ValueError("Ось строк не подтверждена значениями исходной матрицы")

        row_axis_definition = (
            proposed.row_axis.model_dump(mode="json")
            if proposed.row_axis is not None
            else None
        )
        source_unit_hint = _row_axis_unit_hint(source_context, source_block)
        if row_axis_definition is not None:
            proposed_unit = canonicalize_inline_unit(
                row_axis_definition.get("canonical_unit")
            )
            source_unit = canonicalize_inline_unit(source_unit_hint)
            if proposed_unit and source_unit and proposed_unit != source_unit:
                raise ValueError(
                    f"Единица оси строк блока {proposed.block_ref} противоречит источнику"
                )
            row_axis_definition["canonical_unit"] = source_unit or proposed_unit

        column_axis_definition = proposed.column_axis.model_dump(mode="json")
        column_axis_definition["canonical_unit"] = canonicalize_inline_unit(
            column_axis_definition.get("canonical_unit")
        )

        proven = 0
        for row_index in data_rows:
            for column_index in measure_columns:
                if not (0 <= row_index < len(grid.rows)) or column_index >= len(grid.rows[row_index]):
                    continue
                cell = grid.rows[row_index][column_index]
                if cell.is_merged_fill or not _price_like(cell.value):
                    continue
                coordinate = (row_index, column_index)
                if coordinate in used_measures:
                    raise ValueError("Одна measure-ячейка используется двумя matrix blocks")
                used_measures.add(coordinate)
                proven += 1
        if proven == 0:
            raise ValueError(f"Блок {proposed.block_ref} не содержит проверенных цен")

        compiled_blocks.append(
            {
                "block_ref": proposed.block_ref,
                "identity_mode": _detected_identity_mode(source_block),
                "name": name,
                "article": article,
                "unit": unit,
                "category": category,
                "row_axis_source": row_axis,
                "row_axis": row_axis_definition,
                "column_axis_row_index": next(iter(axis_rows)),
                "column_axis_columns": measure_columns,
                "column_axis": column_axis_definition,
                "data_row_indexes": data_rows,
                "price_descriptor": proposed.price_descriptor.model_dump(mode="json"),
            }
        )
    compiled_region = {
        "format": MATRIX_PROPOSAL_CONTRACT,
        "source_section_id": proposal.source_section_id,
        "match": {"name_pattern": grid.name, "or_index": sheet_index},
        "region_start_row_index": source_context["region_start_row_index"],
        "region_end_row_index": source_context["region_end_row_index"],
        "blocks": compiled_blocks,
        "validated_measure_cells": len(used_measures),
    }
    compiled_region["structure_signature"] = _matrix_structure_signature(
        grids, compiled_region, source_context=source_context
    )
    return compiled_region


def _signature_value(value: object | None) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).casefold()


def _semantic_cells(row: object) -> list[dict[str, Any]]:
    if not isinstance(row, dict):
        return []
    return [
        {
            "column_track": str(cell.get("column_track") or ""),
            "value_kind": str(cell.get("value_kind") or ""),
            "value": _signature_value(cell.get("value")),
            "merged_span": int(cell.get("merged_span") or 0),
        }
        for cell in row.get("cells", [])
        if isinstance(cell, dict) and cell.get("value") is not None
    ]


def _axis_semantic_signature(source_context: dict[str, Any]) -> dict[str, Any]:
    """Keep source evidence that gives otherwise numeric axes their meaning."""

    blocks = []
    for block in source_context.get("blocks", []):
        if not isinstance(block, dict):
            continue
        blocks.append(
            {
                "kind": str(block.get("kind") or ""),
                "axis_labels": [
                    cell
                    for cell in _semantic_cells(block.get("axis_header"))
                    if cell["value_kind"] != "axis_value"
                ],
            }
        )
    return {
        "semantic_header": _semantic_cells(source_context.get("semantic_header")),
        "blocks": blocks,
    }


def _matrix_structure_signature(
    grids: list[SheetGrid],
    region: dict[str, Any],
    *,
    source_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    match = region.get("match") or {}
    sheet_index = match.get("or_index")
    if not isinstance(sheet_index, int) or not (0 <= sheet_index < len(grids)):
        raise ValueError("Лист matrix region не найден")
    grid = grids[sheet_index]
    signatures: list[dict[str, Any]] = []
    for block in region.get("blocks", []):
        axis_row = block.get("column_axis_row_index")
        axis_columns = block.get("column_axis_columns")
        data_rows = block.get("data_row_indexes")
        if (
            not isinstance(axis_row, int)
            or not isinstance(axis_columns, list)
            or not all(isinstance(value, int) for value in axis_columns)
            or not isinstance(data_rows, list)
            or not all(isinstance(value, int) for value in data_rows)
        ):
            raise ValueError("Matrix region не содержит проверенной структуры осей")
        column_values = [
            _signature_value(grid.rows[axis_row][column].value)
            for column in axis_columns
            if 0 <= axis_row < len(grid.rows)
            and 0 <= column < len(grid.rows[axis_row])
        ]
        if len(column_values) != len(axis_columns):
            raise ValueError("Ось колонок matrix region выходит за границы листа")
        row_binding = block.get("row_axis_source")
        row_values: list[str] = []
        if isinstance(row_binding, dict):
            row_column = row_binding.get("column_index")
            if not isinstance(row_column, int):
                raise ValueError("Ось строк matrix region повреждена")
            row_values = [
                _signature_value(grid.rows[row][row_column].value)
                for row in data_rows
                if 0 <= row < len(grid.rows) and 0 <= row_column < len(grid.rows[row])
            ]
            if len(row_values) != len(data_rows):
                raise ValueError("Ось строк matrix region выходит за границы листа")
        signatures.append(
            {
                "identity_mode": block.get("identity_mode"),
                "has_row_axis": isinstance(row_binding, dict),
                "column_axis_values": column_values,
                "row_axis_values": row_values,
            }
        )
    return {
        "version": "matrix-structure-v2",
        "axis_semantics": (
            _axis_semantic_signature(source_context)
            if source_context is not None
            else None
        ),
        "blocks": signatures,
    }


def _current_matrix_context(
    grids: list[SheetGrid], region: dict[str, Any]
) -> dict[str, Any]:
    match = region.get("match") or {}
    wanted_name = normalize_header(match.get("name_pattern") or "")
    sheet_index = match.get("or_index")
    candidates = [
        context
        for context in build_matrix_section_contexts(grids)
        if (
            wanted_name
            and normalize_header(context.get("sheet_name") or "") == wanted_name
        )
        or (
            not wanted_name
            and isinstance(sheet_index, int)
            and context.get("sheet_index") == sheet_index
        )
    ]
    if len(candidates) != 1:
        raise MatrixSchemaChangedError(
            "Схема матрицы изменилась: исходная матричная секция не найдена однозначно"
        )
    return candidates[0]


def _recompile_matrix_region(
    grids: list[SheetGrid], region: dict[str, Any]
) -> dict[str, Any]:
    context = _current_matrix_context(grids, region)
    saved_blocks = region.get("blocks")
    current_blocks = context.get("blocks")
    if (
        not isinstance(saved_blocks, list)
        or not isinstance(current_blocks, list)
        or len(saved_blocks) != len(current_blocks)
    ):
        raise MatrixSchemaChangedError(
            "Схема матрицы изменилась: изменилось количество матричных блоков"
        )
    proposal_blocks = []
    for saved, current in zip(saved_blocks, current_blocks, strict=True):
        if not isinstance(saved, dict) or not isinstance(current, dict):
            raise MatrixSchemaChangedError("Схема матрицы изменилась: блок повреждён")
        proposal_blocks.append(
            {
                "block_ref": current.get("block_ref"),
                "row_axis": saved.get("row_axis"),
                "column_axis": saved.get("column_axis"),
                "price_descriptor": saved.get("price_descriptor"),
            }
        )
    try:
        proposal = MatrixRuleProposal.model_validate(
            {
                "schema_version": "matrix-unfold-proposal-v2",
                "decision": "propose",
                "rule_name": "Перепривязка матрицы",
                "source_section_id": context["section_id"],
                "blocks": proposal_blocks,
                "unresolved": [],
                "confidence": 1.0,
                "requires_manual_review": True,
            }
        )
        rebound = compile_matrix_proposal(
            proposal, source_context=context, grids=grids
        )
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, MatrixSchemaChangedError):
            raise
        raise MatrixSchemaChangedError(
            f"Схема матрицы изменилась: {exc}"
        ) from exc
    rebound["source_section_id"] = region.get("source_section_id")
    return rebound


def rebind_matrix_regions(
    grids: list[SheetGrid], regions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Rebuild only matrix source bindings while retaining reviewed semantics."""

    return [_recompile_matrix_region(grids, region) for region in regions]


def resolve_matrix_regions(
    grids: list[SheetGrid], regions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Resolve current coordinates and reject axis/schema drift before parsing."""

    resolved: list[dict[str, Any]] = []
    for region in regions:
        expected = region.get("structure_signature")
        unsigned_legacy = not isinstance(expected, dict)
        if not isinstance(expected, dict):
            # Legacy regions predate stored signatures. Derive their expected
            # axes from the saved coordinates only when those coordinates
            # still describe the current source. Any shift or axis addition
            # fails safe and can be migrated with matrix-only rebind.
            try:
                expected = _matrix_structure_signature(grids, region)
            except ValueError as exc:
                raise MatrixSchemaChangedError(
                    "Схема матрицы изменилась или старое правило не содержит "
                    "устойчивой привязки; перепривяжите только матрицу"
                ) from exc
        rebound = _recompile_matrix_region(grids, region)
        current = rebound.get("structure_signature")
        expected_version = expected.get("version")
        compatible_version = expected_version in {
            "matrix-structure-v1",
            "matrix-structure-v2",
        }
        same_blocks = (
            isinstance(current, dict)
            and current.get("version") == "matrix-structure-v2"
            and current.get("blocks") == expected.get("blocks")
        )
        # Unsigned and v1 rules may not contain semantic-header evidence. In
        # that case their saved axis/value bindings are the strongest baseline
        # available. Once a v2 signature exists, semantic evidence is compared
        # strictly as well.
        compare_semantics = (
            not unsigned_legacy
            and (
                expected_version == "matrix-structure-v2"
                or "axis_semantics" in expected
            )
        )
        same_semantics = (
            not compare_semantics
            or (
                isinstance(current, dict)
                and current.get("axis_semantics") == expected.get("axis_semantics")
            )
        )
        if not (compatible_version and same_blocks and same_semantics):
            raise MatrixSchemaChangedError(
                "Схема матрицы изменилась: изменились значения или состав осей; "
                "перепривяжите только матрицу и повторите dry-run"
            )
        resolved.append(rebound)
    return resolved


def _source_value(grid: SheetGrid, binding: dict[str, int] | None, row: int | None = None) -> object | None:
    if binding is None:
        return None
    row_index = binding["row_index"] if row is None else row
    column_index = binding["column_index"]
    if not (0 <= row_index < len(grid.rows)) or not (0 <= column_index < len(grid.rows[row_index])):
        return None
    cell = grid.rows[row_index][column_index]
    if cell.is_merged_fill and row is not None:
        return None
    return cell.value


def _attribute(
    definition: dict[str, Any], raw_value: object, column_index: int, ordinal: int
) -> ParsedAttribute:
    return ParsedAttribute(
        source_header=str(definition["display_name"]),
        raw_value=str(raw_value),
        role="attribute",
        attribute_code=str(definition["attribute_code"]),
        value_kind="decimal" if _axis_like(raw_value) else "text",
        canonical_unit=definition.get("canonical_unit"),
        ordinal=ordinal,
        source_column_index=column_index,
    )


def parse_matrix_regions(
    grids: list[SheetGrid], regions: list[dict[str, Any]]
) -> ParseResult:
    """Execute compiled regions without any model access."""

    regions = resolve_matrix_regions(grids, regions)
    result = ParseResult(coordinate_coverage_verified=True)
    consumed: set[tuple[int, int, int]] = set()
    for region in regions:
        if region.get("format") != MATRIX_PROPOSAL_CONTRACT:
            raise ValueError("Неизвестный формат matrix region")
        match = region.get("match") or {}
        sheet_index = match.get("or_index")
        if not isinstance(sheet_index, int) or not (0 <= sheet_index < len(grids)):
            raise ValueError("Лист matrix region не найден")
        grid = grids[sheet_index]
        if match.get("name_pattern") != grid.name:
            raise ValueError("Имя листа matrix region изменилось")
        for block in region.get("blocks", []):
            price_descriptor = block["price_descriptor"]
            for row_index in block["data_row_indexes"]:
                name_binding = block["name"]
                identity_row = (
                    row_index if block["identity_mode"] == "row" else name_binding["row_index"]
                )
                raw_name = _source_value(grid, name_binding, identity_row)
                if raw_name is None:
                    raise ValueError(f"У matrix item отсутствует имя в блоке {block['block_ref']}")
                article_binding = block.get("article")
                raw_article = _source_value(
                    grid,
                    article_binding,
                    row_index if block["identity_mode"] == "row" else None,
                )
                raw_unit = _source_value(
                    grid,
                    block.get("unit"),
                    row_index if block["identity_mode"] == "row" else None,
                )
                raw_category = _source_value(grid, block.get("category"))
                row_axis_binding = block.get("row_axis_source")
                raw_row_axis = _source_value(grid, row_axis_binding, row_index)
                for column_index in block["column_axis_columns"]:
                    if column_index >= len(grid.rows[row_index]):
                        continue
                    measure = grid.rows[row_index][column_index]
                    if measure.is_merged_fill:
                        continue
                    parsed = parse_price(
                        # Stored value, not the displayed one: a matrix cell
                        # under a zero-decimal format would otherwise persist
                        # as a rounded price.
                        measure.value if measure.value is not None else measure.display_value,
                        allow_qualified=True,
                    )
                    if parsed.status not in {"ok", "on_request"}:
                        continue
                    coordinate = (sheet_index, row_index, column_index)
                    if coordinate in consumed:
                        raise ValueError("Одна measure-ячейка породила несколько позиций")
                    consumed.add(coordinate)
                    axis_row = block["column_axis_row_index"]
                    raw_column_axis = grid.rows[axis_row][column_index].value
                    attributes = []
                    if block.get("row_axis") is not None and raw_row_axis is not None:
                        attributes.append(
                            _attribute(
                                block["row_axis"],
                                raw_row_axis,
                                row_axis_binding["column_index"],
                                len(attributes),
                            )
                        )
                    attributes.append(
                        _attribute(
                            block["column_axis"],
                            raw_column_axis,
                            column_index,
                            len(attributes),
                        )
                    )
                    format_currency = currency_from_number_format(measure.number_format)
                    value = ParsedValue(
                        kind=price_descriptor["kind"],
                        amount=parsed.amount,
                        vat_mode=price_descriptor["vat_mode"],
                        is_on_request=parsed.status == "on_request",
                        source_label=str(block["column_axis"]["display_name"]),
                        qualifier=parsed.qualifier,
                        amount_from=parsed.amount_from,
                        amount_to=parsed.amount_to,
                        currency=parsed.currency or format_currency,
                        currency_source=(
                            parsed.currency_source
                            if parsed.currency is not None
                            else ("number_format" if format_currency else "unresolved")
                        ),
                        basis_quantity=parsed.basis_quantity,
                        basis_unit=parsed.basis_unit,
                        raw_value=parsed.raw_value,
                        source_column_index=column_index,
                    )
                    if parsed.amount == Decimal("0.00"):
                        result.issues.append(
                            ParseIssue(
                                severity="warning",
                                code="ZERO_PRICE",
                                sheet=grid.name,
                                row_no=row_index + 1,
                                source_value=str(measure.value),
                                message="Нулевая цена",
                            )
                        )
                    result.items.append(
                        ParsedItem(
                            source_name=str(raw_name).strip(),
                            normalized_name=normalize_name(str(raw_name)),
                            source_article=(
                                str(raw_article).strip() if raw_article is not None else None
                            ),
                            normalized_article=normalize_article(raw_article) if raw_article is not None else None,
                            unit_code=normalize_unit(raw_unit),
                            category_path=(
                                str(raw_category).strip() if raw_category is not None else None
                            ),
                            dimensions=None,
                            description=None,
                            attribute_values=attributes,
                            source_sheet=grid.name,
                            source_row=row_index + 1,
                            source_section_id=region.get("source_section_id"),
                            source_column_start=column_index,
                            source_column_end=column_index + 1,
                            values=[value],
                            source_header_cells=[[column_index, str(raw_column_axis)]],
                            source_data_cells=[[column_index, str(measure.value)]],
                        )
                    )
    result.rows_accepted = len(result.items)
    return result
