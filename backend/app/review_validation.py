"""Pure compilation and dry-run checks for a price-import review revision."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import HTTPException

from app.parsing import loaders
from app.parsing.mapping import ROLES, stored_mapping_to_tables
from app.parsing.normalize import is_price_evidence, parse_price
from app.parsing.pdf_layout import is_pdf_layout_recipe, parse_pdf_layout
from app.parsing.pipeline import ParseResult, parse_grids
from app.parsing.rule_proposal import build_tabular_section_contexts
from app.parsing.rules import recipe_to_decision, validation_metrics
from app.parsing.workbook_layout import (
    is_workbook_layout_recipe,
    parse_workbook_layout,
)


def review_state_hash(
    *, mapping: dict | None, recipe: dict | None, decisions: list[dict], currency: str | None
) -> str:
    payload = json.dumps(
        {
            "mapping": mapping,
            "recipe": recipe,
            "decisions": decisions,
            "currency": currency,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def validate_mapping_payload(mapping: dict) -> None:
    tables = mapping.get("tables") if isinstance(mapping, dict) else None
    if not isinstance(tables, list) or not tables:
        raise HTTPException(status_code=422, detail="Маппинг должен содержать хотя бы одну секцию")
    for table in tables:
        columns = table.get("columns") if isinstance(table, dict) else None
        if not isinstance(columns, list):
            raise HTTPException(status_code=422, detail="Некорректная секция маппинга")
        roles = {column.get("role") for column in columns if isinstance(column, dict)}
        unknown = sorted(role for role in roles if role not in ROLES)
        if unknown:
            raise HTTPException(status_code=422, detail=f"Неизвестные роли: {', '.join(unknown)}")
        if not {"name", "price"}.issubset(roles):
            raise HTTPException(
                status_code=422,
                detail="В каждой ценовой секции нужны роли name и price",
            )


def load_source(data: bytes, filename: str) -> tuple[str, list]:
    try:
        loader = loaders.detect_loader(data, filename)
        if loader == loaders.LOADER_PDF_SCAN:
            raise HTTPException(status_code=409, detail="Для review требуется OCR")
        grids = loaders.load_grids_for(loader, data, filename)
    except HTTPException:
        raise
    except (loaders.UnknownFormatError, loaders.DocumentConversionError, loaders.PdfNoTablesError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return loader, grids


def validate_non_price_decisions(
    *, grids: list, decisions: list[dict], allow_override: bool = False
) -> list[dict]:
    """Отвергнуть решения, скрывающие ценовое доказательство.

    Возвращает список переопределённых доказательств: при ``allow_override``
    решение с ``override_guard=True`` не отвергается, а его заблокированные
    ячейки собираются сюда — вызывающий обязан записать их в журнал. Без флага
    и без подтверждения поведение прежнее: 422 ``UNSAFE_NON_PRICE_DECISION``.
    """
    grids_by_name = {grid.name: grid for grid in grids}
    contexts = build_tabular_section_contexts(grids)
    price_columns_by_sheet: dict[str, set[int]] = {}
    for context in contexts:
        columns = price_columns_by_sheet.setdefault(str(context["sheet_name"]), set())
        for panel in context.get("horizontal_panels") or []:
            columns.update(int(index) for index in panel.get("price_columns") or [])
        for index in (context.get("price_presence") or {}).get("price_column_indexes") or []:
            columns.add(int(index))

    overridden: list[dict] = []
    for decision in decisions:
        grid = grids_by_name.get(str(decision["sheet"]))
        if grid is None:
            raise HTTPException(status_code=422, detail="Лист решения не найден в исходном файле")
        start = int(decision["row_start"])
        end = int(decision["row_end"])
        if end < start or end > len(grid.rows):
            raise HTTPException(status_code=422, detail="Диапазон строк решения вне исходного листа")
        known_price_columns = price_columns_by_sheet.get(grid.name, set())
        blocked: list[dict] = []
        for excel_row in range(start, end + 1):
            row = grid.rows[excel_row - 1]
            for column_index, cell in enumerate(row):
                value = cell.display_value if cell.display_value is not None else cell.value
                if value is None:
                    continue
                parsed = parse_price(value, allow_qualified=True)
                # Bare technical numerics outside a detected price column are
                # not sufficient price evidence. Qualified/on-request values
                # are evidence in any source column.
                strong = parsed.status == "on_request" or (
                    isinstance(value, str)
                    and any(char.isalpha() for char in value)
                    and parsed.status == "ok"
                )
                # Реквизиты (ИНН, р/с) парсятся как числа, но ценой не являются:
                # иначе оператор не может скрыть шапку счёта (см. is_price_evidence).
                if is_price_evidence(value) and (
                    column_index in known_price_columns or strong
                ):
                    blocked.append(
                        {
                            "sheet": grid.name,
                            "row": excel_row,
                            "column": column_index + 1,
                            "value": str(value)[:200],
                        }
                    )
        if not blocked:
            continue
        if allow_override and decision.get("override_guard"):
            overridden.extend(blocked)
            continue
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UNSAFE_NON_PRICE_DECISION",
                "message": "Решение скрывает строку с ценовым признаком",
                **blocked[0],
                "evidence": blocked,
            },
        )
    return overridden


def parse_review_draft(
    *,
    data: bytes,
    filename: str,
    mapping: dict | None,
    recipe: dict | None,
) -> tuple[str, list, ParseResult]:
    loader, grids = load_source(data, filename)
    if recipe is not None:
        if is_pdf_layout_recipe(recipe):
            result = parse_pdf_layout(data, recipe)
            return loader, grids, result
        if is_workbook_layout_recipe(recipe):
            return loader, grids, parse_workbook_layout(grids, recipe)
        decision = recipe_to_decision(recipe, grids)
    elif mapping is not None:
        validate_mapping_payload(mapping)
        decision = stored_mapping_to_tables(mapping, grids)
    else:
        builtin_recipe = None  # встроенных профилей нет: правила живут в parse_rules
        if builtin_recipe is not None:
            return loader, grids, parse_workbook_layout(grids, builtin_recipe)
        raise HTTPException(
            status_code=409,
            detail="Сначала сохраните маппинг или типизированное правило",
        )
    if not decision.auto_ok:
        raise HTTPException(status_code=422, detail="Draft не применился к исходному файлу")
    return loader, grids, parse_grids(grids, decision)


def _decision_covers(decisions: list[dict], sheet: str, excel_row: int, panel: int | None) -> bool:
    return any(
        decision.get("sheet") == sheet
        and int(decision.get("row_start", 0)) <= excel_row <= int(decision.get("row_end", -1))
        and (decision.get("panel_index") is None or decision.get("panel_index") == panel)
        for decision in decisions
    )


def dry_run_summary(
    *,
    result: ParseResult,
    grids: list,
    decisions: list[dict],
    currency_override: str | None,
    current_item_count: int,
    has_default_currency: bool = False,
) -> dict[str, Any]:
    quality = validation_metrics(result)
    missing_currency = 0
    price_count = 0
    for item in result.items:
        for value in item.values:
            price_count += 1
            has_amount = value.amount is not None or value.amount_from is not None
            if (
                has_amount
                and value.currency is None
                and currency_override is None
                and not has_default_currency
            ):
                missing_currency += 1

    covered: set[tuple[str, int, int]] = set()
    for item in result.items:
        panel = item.source_panel_index or 0
        covered.add((item.source_sheet, item.source_row, panel))
        for glued_row in item.glued_source_rows or []:
            # Quantity-tier ladders (merged-fill name rows) were glued
            # into the parent item by the parser; treat the absorbed
            # source rows as covered too.
            covered.add((item.source_sheet, glued_row, panel))
    grids_by_name = {grid.name: grid for grid in grids}
    missed: list[dict] = []
    contexts = [] if result.coordinate_coverage_verified else build_tabular_section_contexts(grids)
    for context in contexts:
        if context.get("section_kind") not in {"price_rows", "ambiguous_tabular_rows"}:
            continue
        sheet = str(context["sheet_name"])
        rows = [
            int(value) + 1
            for value in (context.get("price_presence") or {}).get("candidate_row_indexes", [])
        ]
        panels = context.get("horizontal_panels") or []
        grid = grids_by_name.get(sheet)
        for row in rows:
            panel_indexes: list[int]
            if panels and grid is not None and 0 < row <= len(grid.rows):
                source_row = grid.rows[row - 1]
                panel_indexes = []
                for panel_index, panel in enumerate(panels):
                    price_columns = panel.get("price_columns") or []
                    if any(
                        0 <= int(column) < len(source_row)
                        and is_price_evidence(
                            source_row[int(column)].display_value
                            if source_row[int(column)].display_value is not None
                            else source_row[int(column)].value,
                        )
                        for column in price_columns
                    ):
                        panel_indexes.append(panel_index)
            else:
                panel_indexes = [0]
            for panel in panel_indexes:
                if _decision_covers(decisions, sheet, row, panel):
                    continue
                if (sheet, row, panel) not in covered:
                    missed.append({"sheet": sheet, "row": row, "panel_index": panel})

    passed = bool(quality["passed"]) and missing_currency == 0 and not missed
    return {
        "passed": passed,
        "metrics": {
            **quality["metrics"],
            "item_count": len(result.items),
            "price_count": price_count,
            "currency_unresolved": missing_currency,
            "confirmed_missed_positions": len(missed),
            "missed_coordinates": missed[:100],
        },
        "diff": {
            "current_item_count": current_item_count,
            "candidate_item_count": len(result.items),
            "item_count_delta": len(result.items) - current_item_count,
        },
    }
