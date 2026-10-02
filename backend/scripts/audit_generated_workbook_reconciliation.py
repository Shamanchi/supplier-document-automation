#!/usr/bin/env python3
"""Physical-row reconciliation for a compiled generated workbook recipe."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))


def _value(cell: Any) -> Any:
    value = cell.display_value if cell.display_value is not None else cell.value
    if isinstance(value, str):
        return " ".join(value.split()) or None
    return value


def main() -> int:
    from app.parsing import loaders
    from app.parsing.normalize import parse_price
    from app.parsing.rule_proposal_handlers import build_rule_section_contexts
    from app.parsing.workbook_layout import parse_workbook_layout

    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("acceptance", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    data = args.source.read_bytes()
    loader = loaders.detect_loader(data, args.source.name)
    grids = loaders.load_grids_for(loader, data, args.source.name)
    acceptance = json.loads(args.acceptance.read_text(encoding="utf-8"))
    result = parse_workbook_layout(grids, acceptance["recipe"])
    contexts = build_rule_section_contexts(grids)
    context_by_id = {str(context["section_id"]): context for context in contexts}

    item_counts = Counter((item.source_sheet, item.source_row) for item in result.items)
    item_rows = set(item_counts)
    issue_counts = Counter(
        (issue.sheet, issue.row_no)
        for issue in result.issues
        if issue.sheet is not None and issue.row_no is not None
    )
    header_rows = {
        (str(context["sheet_name"]), int(row_index) + 1)
        for context in contexts
        for key in ("column_header_row_indexes", "category_row_indexes")
        for row_index in context.get(key, [])
    }
    catalog_rows: set[tuple[str, int]] = set()
    not_price_rows: set[tuple[str, int]] = set()
    for attempt in acceptance.get("attempts", []):
        context = context_by_id.get(str(attempt.get("section_id")))
        if context is None:
            continue
        decision = (attempt.get("proposal") or {}).get("decision")
        target = catalog_rows if decision == "catalog_without_price" else not_price_rows if decision == "not_price_section" else None
        if target is not None:
            target.update(
                (str(context["sheet_name"]), int(row_index) + 1)
                for row_index in (context.get("price_presence") or {}).get("candidate_row_indexes", [])
            )

    rows: list[dict[str, Any]] = []
    for grid in grids:
        for row_index, row in enumerate(grid.rows):
            values = [
                {"column_index": column_index, "value": value}
                for column_index, cell in enumerate(row)
                if not cell.is_merged_fill and (value := _value(cell)) is not None
            ]
            if not values:
                continue
            coordinate = (grid.name, row_index + 1)
            row_class = (
                "parsed_item" if coordinate in item_rows else
                "header" if coordinate in header_rows else
                "catalog_without_price" if coordinate in catalog_rows else
                "not_price_section" if coordinate in not_price_rows else
                "parser_issue" if coordinate in issue_counts else
                "unclassified"
            )
            price_like = any(
                parse_price(value["value"], allow_qualified=True).status in {"ok", "on_request"}
                for value in values
            )
            text_identity = any(
                isinstance(value["value"], str)
                and any(character.isalpha() for character in value["value"])
                and parse_price(value["value"], allow_qualified=True).status not in {"ok", "on_request"}
                for value in values
            )
            rows.append({
                "sheet": grid.name,
                "excel_row": row_index + 1,
                "class": row_class,
                "item_count": item_counts.get(coordinate, 0),
                "issue_count": issue_counts.get(coordinate, 0),
                "unclassified_price_like": row_class == "unclassified" and price_like and text_identity,
                "values": values,
            })

    counts = Counter(row["class"] for row in rows)
    report = {
        "source": args.source.name,
        "loader": loader,
        "nonempty_physical_rows": len(rows),
        "parsed_items": len(result.items),
        "unique_item_rows": len(item_rows),
        "multi_item_physical_rows": sum(count > 1 for count in item_counts.values()),
        "unique_header_rows": len(header_rows),
        "header_rows": counts["header"],
        "catalog_without_price_rows": counts["catalog_without_price"],
        "not_price_section_rows": counts["not_price_section"],
        "parser_issue_rows": counts["parser_issue"],
        "unclassified_rows": counts["unclassified"],
        "unclassified_price_like_rows": sum(row["unclassified_price_like"] for row in rows),
        "issue_codes": dict(Counter(issue.code for issue in result.issues)),
        "rows": rows,
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "rows"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
