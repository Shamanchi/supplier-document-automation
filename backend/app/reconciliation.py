"""Production, source-coordinate based completeness and money gates."""
from __future__ import annotations

from collections import Counter

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.currencies import currency_directory_map, default_currency
from app.models import (
    ParseRule,
    PriceParseIssue,
    PriceVersion,
    RuleGenerationAttempt,
    SupplierPriceItem,
    SupplierPriceValue,
)
from app.parsing import loaders
from app.parsing.headers import detect_header_candidates
from app.parsing.normalize import is_price_evidence
from app.parsing.pdf_layout import is_pdf_layout_recipe
from app.parsing.rule_proposal import build_tabular_section_contexts
from app.parsing.workbook_layout import (
    workbook_layout_header_coordinates,
)


MONEY_GATE_CODES = {
    "PRICE_CURRENCY_UNRESOLVED",
    "PRICE_SEMANTICS_UNPARSED",
    "PRICE_RANGE_INVALID",
}
MONEY_PARSE_GATE_CODES = {"PRICE_CURRENCY_CONFLICT", "PRICE_TIER_AMBIGUOUS"}
CARD_MARKERS = ("Р В±Р С‘Р Р…", "Р С‘Р С‘Р С”", "Р В±Р С‘Р С”", "РЎвЂљР ВµР В»", "Р В°Р Т‘РЎР‚Р ВµРЎРѓ", "e-mail", "email", "Р С—Р С•РЎвЂЎРЎвЂљ", "РЎРѓР В°Р в„–РЎвЂљ")
NOTE_MARKERS = ("Р С—РЎР‚Р С‘Р СР ВµРЎвЂЎ", "РЎС“РЎРѓР В»Р С•Р Р†Р С‘РЎРЏ", "Р Т‘Р С•РЎРѓРЎвЂљР В°Р Р†Р С”Р В°", "Р С‘РЎвЂљР С•Р С–Р С•", "Р Р†РЎРѓР ВµР С–Р С•", "РЎРѓР С”Р С‘Р Т‘Р С”Р В°", "РЎРѓРЎР‚Р С•Р С” Р Т‘Р ВµР в„–РЎРѓРЎвЂљР Р†Р С‘РЎРЏ")


def _nonempty_values(row) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for index, cell in enumerate(row):
        if cell.is_merged_fill or cell.value is None:
            continue
        value = str(cell.display_value if cell.display_value is not None else cell.value).strip()
        if value:
            result.append((index, value))
    return result


async def apply_money_gate(
    session: AsyncSession,
    *,
    version: PriceVersion,
    currency_hint: str | None,
    currency_source: str = "request_hint",
) -> dict[str, int]:
    await session.execute(
        delete(PriceParseIssue).where(
            PriceParseIssue.price_version_id == version.id,
            PriceParseIssue.code.in_(MONEY_GATE_CODES),
        )
    )
    rows = (
        await session.execute(
            select(SupplierPriceValue, SupplierPriceItem)
            .join(SupplierPriceItem, SupplierPriceValue.item_id == SupplierPriceItem.id)
            .where(SupplierPriceItem.price_version_id == version.id)
        )
    ).all()
    counts: Counter[str] = Counter()
    existing_gate_issues = (
        await session.execute(
            select(PriceParseIssue).where(
                PriceParseIssue.price_version_id == version.id,
                PriceParseIssue.code.in_(MONEY_PARSE_GATE_CODES),
            )
        )
    ).scalars().all()
    counts["currency_conflict"] = sum(
        issue.code == "PRICE_CURRENCY_CONFLICT" for issue in existing_gate_issues
    )
    counts["tier_ambiguous"] = sum(
        issue.code == "PRICE_TIER_AMBIGUOUS" for issue in existing_gate_issues
    )
    hint = currency_hint.upper() if currency_hint else None
    directory = await currency_directory_map(session)
    fallback = await default_currency(session)
    for value, item in rows:
        has_amount = value.amount is not None or value.amount_from is not None
        if has_amount and value.currency is None and hint:
            value.currency = hint
            value.currency_source = currency_source
        if has_amount and value.currency is None and fallback is not None:
            value.currency = fallback.code
            value.currency_source = "default"
        if value.currency is not None and value.currency_id is None:
            directory_row = directory.get(value.currency.upper())
            if directory_row is not None:
                value.currency_id = directory_row.id
        if has_amount and value.currency is None:
            counts["currency_unresolved"] += 1
            session.add(
                PriceParseIssue(
                    price_version_id=version.id,
                    severity="error",
                    code="PRICE_CURRENCY_UNRESOLVED",
                    sheet=item.source_sheet,
                    row_no=item.source_row,
                    column_ref=(
                        str(value.source_column_index + 1)
                        if value.source_column_index is not None
                        else None
                    ),
                    source_value=value.raw_value,
                    message="Р вЂќР В»РЎРЏ Р Т‘Р ВµР Р…Р ВµР В¶Р Р…Р С•Р С–Р С• Р В·Р Р…Р В°РЎвЂЎР ВµР Р…Р С‘РЎРЏ Р Р…Р Вµ Р Т‘Р С•Р С”Р В°Р В·Р В°Р Р…Р В° Р Р†Р В°Р В»РЎР‹РЎвЂљР В°",
                )
            )
        if value.qualifier == "unparsed":
            counts["semantics_unparsed"] += 1
            session.add(
                PriceParseIssue(
                    price_version_id=version.id,
                    severity="error",
                    code="PRICE_SEMANTICS_UNPARSED",
                    sheet=item.source_sheet,
                    row_no=item.source_row,
                    source_value=value.raw_value,
                    message="Р вЂќР ВµР Р…Р ВµР В¶Р Р…Р В°РЎРЏ РЎРѓР ВµР СР В°Р Р…РЎвЂљР С‘Р С”Р В° РЎРѓР С•РЎвЂ¦РЎР‚Р В°Р Р…Р ВµР Р…Р В° Р С”Р В°Р С” raw Р С‘ РЎвЂљРЎР‚Р ВµР В±РЎС“Р ВµРЎвЂљ Р С—РЎР‚Р С•Р Р†Р ВµРЎР‚Р С”Р С‘",
                )
            )
        if (
            value.qualifier == "range"
            and value.amount_from is not None
            and value.amount_to is not None
            and value.amount_to < value.amount_from
        ):
            counts["invalid_range"] += 1
            session.add(
                PriceParseIssue(
                    price_version_id=version.id,
                    severity="error",
                    code="PRICE_RANGE_INVALID",
                    sheet=item.source_sheet,
                    row_no=item.source_row,
                    source_value=value.raw_value,
                    message="Р вЂ™Р ВµРЎР‚РЎвЂ¦Р Р…РЎРЏРЎРЏ Р С–РЎР‚Р В°Р Р…Р С‘РЎвЂ Р В° РЎвЂ Р ВµР Р…РЎвЂ№ Р СР ВµР Р…РЎРЉРЎв‚¬Р Вµ Р Р…Р С‘Р В¶Р Р…Р ВµР в„–",
                )
            )
    return {
        "currency_unresolved": counts["currency_unresolved"],
        "semantics_unparsed": counts["semantics_unparsed"],
        "invalid_range": counts["invalid_range"],
        "currency_conflict": counts["currency_conflict"],
        "tier_ambiguous": counts["tier_ambiguous"],
        "failures": sum(counts.values()),
    }


async def reconcile_version(
    session: AsyncSession,
    *,
    version: PriceVersion,
    data: bytes,
    filename: str,
    non_price_decisions: list[dict] | None = None,
) -> dict:
    loader = loaders.detect_loader(data, filename)
    grids = loaders.load_grids_for(loader, data, filename)
    items = (
        await session.execute(
            select(SupplierPriceItem).where(SupplierPriceItem.price_version_id == version.id)
        )
    ).scalars().all()
    if version.parse_rule_id is not None:
        rule = await session.get(ParseRule, version.parse_rule_id)
        if rule is not None and is_pdf_layout_recipe(rule.recipe):
            # PDF layout recipes parse from their own text-line grid and already
            # passed a layout-scoped quality gate inside parse_pdf_layout. The
            # loader grid rebuilt below comes from raw PDF lines (wrapped names,
            # section captions, stand-alone row numbers), so its rows never map
            # 1:1 onto layout items: without this branch every layout-rule parse
            # would be flagged with hundreds of missed rows and stuck in
            # review despite a correct result. Treat a priced, error-free
            # layout parse as complete.
            priced_items = int(
                await session.scalar(
                    select(func.count(func.distinct(SupplierPriceValue.item_id)))
                    .join(
                        SupplierPriceItem,
                        SupplierPriceValue.item_id == SupplierPriceItem.id,
                    )
                    .where(SupplierPriceItem.price_version_id == version.id)
                )
                or 0
            )
            nonempty_rows = sum(
                1
                for grid in grids
                for row in grid.rows
                if _nonempty_values(row)
            )
            return {
                "non_empty_physical_rows": nonempty_rows,
                "parsed_items": len(items),
                "unique_item_rows": len(items),
                "unique_item_panels": len(items),
                "unique_header_rows": 0,
                "expected_non_position_rows": 0,
                "extractor_expected_panel_positions": priced_items,
                "confirmed_missed_physical_rows": 0,
                "confirmed_missed_positions": 0,
                "unclassified_rows": 0,
                "classifications": {},
                "missed_coordinates": [],
                "passed": len(items) > 0 and priced_items > 0 and version.errors_count == 0,
            }
    issues = (
        await session.execute(
            select(PriceParseIssue).where(PriceParseIssue.price_version_id == version.id)
        )
    ).scalars().all()
    attempts = (
        await session.execute(
            select(RuleGenerationAttempt).where(
                RuleGenerationAttempt.price_version_id == version.id
            )
        )
    ).scalars().all()
    non_price_sections = {
        attempt.section_id
        for attempt in attempts
        if attempt.status in {"catalog_without_price", "skipped_non_price"}
    }
    manual_decisions = non_price_decisions or []

    def manually_non_price(sheet: str, row: int, panel: int | None = None) -> bool:
        return any(
            decision.get("sheet") == sheet
            and int(decision.get("row_start", 0)) <= row <= int(decision.get("row_end", -1))
            and (decision.get("panel_index") is None or decision.get("panel_index") == panel)
            for decision in manual_decisions
        )
    issue_rows: dict[tuple[str, int], set[str]] = {}
    for issue in issues:
        if issue.sheet and issue.row_no:
            issue_rows.setdefault((issue.sheet, issue.row_no), set()).add(issue.code)

    nonempty: set[tuple[str, int]] = set()
    header_rows: set[tuple[str, int]] = set()
    row_values: dict[tuple[str, int], list[tuple[int, str]]] = {}
    for grid in grids:
        for row_index, row in enumerate(grid.rows):
            values = _nonempty_values(row)
            if values:
                coordinate = (grid.name, row_index + 1)
                nonempty.add(coordinate)
                row_values[coordinate] = values
        for detection in detect_header_candidates(grid):
            if detection.header_row_index is not None:
                header_rows.add((grid.name, detection.header_row_index + 1))

    contexts = build_tabular_section_contexts(grids)
    for context in contexts:
        sheet = str(context["sheet_name"])
        for row_index in [
            *(context.get("column_header_row_indexes") or []),
            *(context.get("category_row_indexes") or []),
        ]:
            header_rows.add((sheet, int(row_index) + 1))
    builtin_workbook_recipe = None  # Р Р†РЎРѓРЎвЂљРЎР‚Р С•Р ВµР Р…Р Р…РЎвЂ№РЎвЂ¦ Р С—РЎР‚Р С•РЎвЂћР С‘Р В»Р ВµР в„– Р Р…Р ВµРЎвЂљ: Р С—РЎР‚Р В°Р Р†Р С‘Р В»Р В° Р В¶Р С‘Р Р†РЎС“РЎвЂљ Р Р† parse_rules
    if builtin_workbook_recipe is not None:
        header_rows.update(
            workbook_layout_header_coordinates(grids, builtin_workbook_recipe)
        )

    item_rows = {(item.source_sheet, item.source_row) for item in items}
    glued_rows: set[tuple[str, int]] = set()
    for item in items:
        for glued_row in item.glued_source_rows or []:
            if isinstance(glued_row, int) and not isinstance(glued_row, bool):
                glued_rows.add((item.source_sheet, glued_row))
    item_panels = {
        (item.source_sheet, item.source_row, item.source_panel_index or 0) for item in items
    }
    items_by_row: dict[tuple[str, int], list[SupplierPriceItem]] = {}
    for item in items:
        items_by_row.setdefault((item.source_sheet, item.source_row), []).append(item)
    remaining = sorted(nonempty - header_rows - item_rows - glued_rows)
    classes: Counter[str] = Counter()
    missed_by_position: dict[tuple[str, int, int | None], dict] = {}
    for coordinate in remaining:
        values = row_values[coordinate]
        if manually_non_price(coordinate[0], coordinate[1]):
            classes["manual_non_price"] += 1
            continue
        text = " ".join(value.casefold().replace("РЎвЂ", "Р Вµ") for _, value in values)
        codes = issue_rows.get(coordinate, set())
        if "NO_PRICE" in codes:
            classes["empty_price"] += 1
            continue
        if any(marker in text for marker in CARD_MARKERS):
            classes["supplier_card"] += 1
            continue
        if any(marker in text for marker in NOTE_MARKERS):
            classes["note_footer"] += 1
            continue
        price_cells = [
            (index, value)
            for index, value in values
            if is_price_evidence(value)
        ]
        name_cells = [(index, value) for index, value in values if (index, value) not in price_cells]
        if price_cells and name_cells:
            classes["parser_issue"] += 1
            missed_by_position[(coordinate[0], coordinate[1], None)] = {
                "sheet": coordinate[0],
                "row": coordinate[1],
                "panel_index": None,
                "price_columns": [index for index, _ in price_cells],
                "name_columns": [index for index, _ in name_cells],
            }
        elif price_cells:
            classes["parser_issue"] += 1
        else:
            classes["catalog_without_price"] += 1

    # A physical row can contain several independent horizontal products. A
    # parsed left panel must not hide an unparsed right panel from the quality
    # gate, so compare each extractor panel with item-level column provenance.
    expected_panel_positions = 0
    for context in contexts:
        if context.get("section_id") in non_price_sections:
            continue
        if context.get("section_kind") not in {"price_rows", "ambiguous_tabular_rows"}:
            continue
        sheet = str(context["sheet_name"])
        candidate_rows = [
            int(row_index)
            for row_index in (context.get("price_presence") or {}).get(
                "candidate_row_indexes", []
            )
        ]
        panels = context.get("horizontal_panels") or []
        if not panels:
            expected_panel_positions += len(candidate_rows)
            for row_index in candidate_rows:
                coordinate = (sheet, row_index + 1)
                if (
                    coordinate in header_rows
                    or coordinate in items_by_row
                    or coordinate in glued_rows
                ):
                    continue
                # The generic all-column pass above records the exact evidence.
                # Avoid inventing a panel identity when the extractor itself
                # could not establish horizontal boundaries.
            continue
        grid = next((candidate for candidate in grids if candidate.name == sheet), None)
        if grid is None:
            continue
        for row_index in candidate_rows:
            if row_index >= len(grid.rows):
                continue
            row = grid.rows[row_index]
            # Ladder rows absorbed into a parent by tier-glue carry no
            # separate item: their prices live on the parent position.
            if (sheet, row_index + 1) in glued_rows:
                continue
            # Panel-specific evidence supersedes the generic one-position
            # classification for this physical row.
            missed_by_position.pop((sheet, row_index + 1, None), None)
            for panel_index, panel in enumerate(panels):
                if manually_non_price(sheet, row_index + 1, panel_index):
                    continue
                name_columns = [int(value) for value in panel.get("name_columns", [])]
                price_columns = [int(value) for value in panel.get("price_columns", [])]
                has_name = any(
                    column < len(row)
                    and not row[column].is_merged_fill
                    and row[column].value is not None
                    and bool(str(row[column].value).strip())
                    for column in name_columns
                )
                price_evidence = [
                    column
                    for column in price_columns
                    if column < len(row)
                    and not row[column].is_merged_fill
                    and is_price_evidence(
                        row[column].display_value
                        if row[column].display_value is not None
                        else row[column].value,
                    )
                ]
                if not has_name or not price_evidence:
                    continue
                expected_panel_positions += 1
                coordinate = (sheet, row_index + 1)
                start = int(panel.get("start_column_index", min(name_columns or [0])))
                end = int(panel.get("end_column_index", max(price_columns or [start]) + 1))
                covered = any(
                    (
                        item.source_panel_index == panel_index
                        and item.source_column_start == start
                    )
                    or (
                        item.source_column_start is not None
                        and item.source_column_end is not None
                        and item.source_column_start <= min(price_evidence)
                        < item.source_column_end
                    )
                    for item in items_by_row.get(coordinate, [])
                )
                if covered:
                    continue
                missed_by_position[(sheet, row_index + 1, panel_index)] = {
                    "sheet": sheet,
                    "row": row_index + 1,
                    "panel_index": panel_index,
                    "panel_column_start": start,
                    "panel_column_end": end,
                    "price_columns": price_evidence,
                    "name_columns": name_columns,
                }
                classes["parser_issue"] += 1

    missed_rows = list(missed_by_position.values())
    missed_physical_rows = {
        (row["sheet"], row["row"]) for row in missed_rows
    }

    summary = {
        "non_empty_physical_rows": len(nonempty),
        "parsed_items": len(items),
        "unique_item_rows": len(item_rows),
        "unique_item_panels": len(item_panels),
        "unique_header_rows": len(header_rows),
        "expected_non_position_rows": sum(
            count for name, count in classes.items() if name != "parser_issue"
        ),
        "extractor_expected_panel_positions": expected_panel_positions,
        "confirmed_missed_physical_rows": len(missed_physical_rows),
        "confirmed_missed_positions": len(missed_rows),
        "unclassified_rows": 0,
        "classifications": dict(sorted(classes.items())),
        "missed_coordinates": missed_rows[:100],
    }
    summary["passed"] = summary["confirmed_missed_positions"] == 0
    return summary
