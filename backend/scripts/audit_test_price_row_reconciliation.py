#!/usr/bin/env python3
"""Reconcile every non-empty physical row in selected spreadsheet prices.

The report deliberately keeps physical-row coverage separate from ParsedItem
count: one horizontal source row may legally produce several independent
items.  Existing successful model proposals are compiled and replayed; no LLM
request is made by this script.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR.parent
DEFAULT_CORPUS = APP_DIR.parents[1] / "external_price_corpus"
DEFAULT_PRIMARY = APP_DIR / "reports/acceptance_deepseek_v4_flash_v3_10_prices_2026-07-15.json"
DEFAULT_REPAIR = APP_DIR / "reports/acceptance_deepseek_v4_flash_v4_repair_10_prices_2026-07-15.json"
DEFAULT_JSON = (
    APP_DIR
    / f"reports/построчная_сверка_10_тестовых_прайсов_{date.today().isoformat()}.json"
)
REVIEWED_TEN = [
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


def _selected_files(args: argparse.Namespace) -> list[str]:
    if args.price:
        return sorted(dict.fromkeys(args.price))
    if not args.all_spreadsheets:
        return list(REVIEWED_TEN)
    files = [
        path.relative_to(args.corpus).as_posix()
        for directory in ("xls", "xlsx", "other")
        for path in sorted((args.corpus / directory).glob("*"))
        if path.is_file() and path.suffix.casefold() in {".xls", ".xlsx"}
    ]
    if args.exclude_reviewed_ten:
        reviewed = set(REVIEWED_TEN)
        files = [relative_path for relative_path in files if relative_path not in reviewed]
    return files


def _cell_value(cell: Any) -> Any:
    value = cell.display_value if cell.display_value is not None else cell.value
    if isinstance(value, str):
        value = " ".join(value.split())
        return value or None
    return value


def _row_values(row: list[Any]) -> list[dict[str, Any]]:
    return [
        {"column_index": index, "value": value}
        for index, cell in enumerate(row)
        if not cell.is_merged_fill and (value := _cell_value(cell)) is not None
    ]


def _passed_requests(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [
        row
        for row in (payload.get("requests") or payload.get("attempts") or [])
        if row.get("passed") and isinstance(row.get("proposal"), dict)
    ]


def _generated_recipe(path: Path, data: bytes) -> dict[str, Any] | None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    recipe = payload.get("recipe")
    if payload.get("source_sha256") != sha256(data).hexdigest():
        return None
    return recipe if isinstance(recipe, dict) else None


def _request_for_context(
    relative_path: str,
    context: dict[str, Any],
    request_sets: list[list[dict[str, Any]]],
) -> dict[str, Any] | None:
    """Match a verified response after source-section hash version changes.

    Section IDs include the context contract version, so an extractor-only
    change can legitimately alter the ID while retaining the same source
    boundary.  Evidence coordinates are the stable source identity.
    """

    section_start = int(context["section_start_row_index"])
    section_end = int(context["section_end_row_index"])
    allowed_decisions = {
        "price_rows": {"propose"},
        "name_rows_without_price": {"propose", "catalog_without_price"},
        "ambiguous_tabular_rows": {"propose", "not_price_section"},
    }.get(context.get("section_kind"), {"propose"})
    for requests in request_sets:  # repaired responses are considered first
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in requests:
            if row.get("price") != relative_path or row.get("sheet_name") != context["sheet_name"]:
                continue
            if row.get("header_row_index") != context.get("header_row_index"):
                continue
            proposal = row["proposal"]
            if proposal.get("decision") not in allowed_decisions:
                continue
            evidence_rows = {
                int(evidence["row_index"])
                for evidence in proposal.get("evidence", [])
                if isinstance(evidence, dict) and isinstance(evidence.get("row_index"), int)
            }
            interior = {index for index in evidence_rows if section_start <= index < section_end}
            exact_section = row.get("section_id") == context.get("section_id")
            if not interior and not exact_section:
                continue
            score = 10 * len(interior)
            if exact_section:
                score += 1000
            mapping = proposal.get("mapping") or {}
            if mapping.get("header_row_index") == context.get("header_row_index"):
                score += 5
            scored.append((score, row))
        if scored:
            scored.sort(key=lambda pair: pair[0], reverse=True)
            if len(scored) > 1 and scored[0][0] == scored[1][0]:
                top = [row for score, row in scored if score == scored[0][0]]
                mappings = {
                    json.dumps(row["proposal"].get("mapping"), sort_keys=True, default=str)
                    for row in top
                }
                if len(mappings) != 1:
                    return None
            return scored[0][1]
    return None


def _classify_row(
    coordinate: tuple[str, int],
    *,
    item_rows: set[tuple[str, int]],
    header_rows: set[tuple[str, int]],
    catalog_rows: set[tuple[str, int]],
    not_price_rows: set[tuple[str, int]],
    issue_rows: set[tuple[str, int]],
    pending_rows: set[tuple[str, int]],
) -> str:
    if coordinate in item_rows:
        return "parsed_item"
    if coordinate in header_rows:
        return "header"
    if coordinate in catalog_rows:
        return "catalog_without_price"
    if coordinate in not_price_rows:
        return "not_price_section"
    if coordinate in issue_rows:
        return "parser_issue"
    if coordinate in pending_rows:
        return "pending_model_rule"
    return "unclassified"


def run(args: argparse.Namespace) -> dict[str, Any]:
    sys.path.insert(0, str(BACKEND_DIR))
    from app.parsing import loaders
    from app.parsing.normalize import parse_price
    from app.parsing.pipeline import ParseResult
    from app.parsing.rule_proposal import (
        build_tabular_section_contexts,
        compile_batch_proposals,
        compile_section_proposal,
        dry_run_decision,
        parse_proposal,
    )
    from app.parsing.workbook_layout import parse_workbook_layout

    request_sets = [_passed_requests(args.repair), _passed_requests(args.primary)]
    prices: list[dict[str, Any]] = []

    for relative_path in _selected_files(args):
        source = args.corpus / relative_path
        try:
            data = source.read_bytes()
            loader = loaders.detect_loader(data, source.name)
            grids = loaders.load_grids_for(loader, data, source.name)
            contexts = build_tabular_section_contexts(grids)
        except Exception as exc:
            prices.append(
                {
                    "price": relative_path,
                    "source_error": " ".join(str(exc).split())[:1000],
                    **{
                        key: 0
                        for key in (
                            "nonempty_physical_rows",
                            "parsed_items",
                            "parsed_item_rows",
                            "unique_item_rows",
                            "multi_item_physical_rows",
                            "extractor_contexts",
                            "panel_contexts",
                            "logical_candidate_rows",
                            "panel_physical_candidate_rows",
                            "panel_logical_rows",
                            "unique_extractor_header_rows",
                            "header_rows",
                            "catalog_without_price_rows",
                            "not_price_section_rows",
                            "parser_issue_rows",
                            "pending_model_rows",
                            "pending_model_candidate_rows",
                            "pending_rows_already_parsed",
                            "unclassified_rows",
                            "unclassified_price_like_rows",
                        )
                    },
                    "rows": [],
                }
            )
            continue
        panel_contexts = [context for context in contexts if context.get("horizontal_panels")]
        panel_candidate_rows = {
            (str(context["sheet_name"]), int(row_index) + 1)
            for context in panel_contexts
            for row_index in context.get("price_presence", {}).get(
                "candidate_row_indexes", []
            )
        }
        panel_logical_rows = sum(
            int(context.get("unfolded_panel_rows_available") or 0)
            for context in panel_contexts
        )
        logical_candidate_rows = sum(
            int(context.get("unfolded_panel_rows_available") or 0)
            if context.get("horizontal_panels")
            else len(
                context.get("price_presence", {}).get("candidate_row_indexes", [])
            )
            for context in contexts
        )
        matched_requests = {
            context["section_id"]: _request_for_context(relative_path, context, request_sets)
            for context in contexts
        }
        missing = [section_id for section_id, row in matched_requests.items() if row is None]
        generated_recipe = next(
            (
                recipe
                for report_path in (args.repair, args.primary)
                if (recipe := _generated_recipe(report_path, data)) is not None
            ),
            None,
        )
        migrated_proposals: dict[str, dict[str, Any]] = {}
        matched_contexts = [
            context for context in contexts if matched_requests[context["section_id"]] is not None
        ]
        for context in matched_contexts:
            proposal = dict(matched_requests[context["section_id"]]["proposal"])
            # The source-bound matcher above proves the physical section.  The
            # compiler still requires the current hash to prevent cross-call
            # replay, so migrate only that derived identity field.
            proposal["source_section_id"] = context["section_id"]
            migrated_proposals[context["section_id"]] = proposal

        verified_contexts: list[dict[str, Any]] = []
        for context in matched_contexts:
            try:
                compile_section_proposal(
                    parse_proposal(migrated_proposals[context["section_id"]]),
                    source_context=context,
                    loader=loader,
                    grids=grids,
                )
            except Exception:
                missing.append(context["section_id"])
            else:
                verified_contexts.append(context)
        missing = sorted(set(missing))
        verified_decisions = {
            str(migrated_proposals[context["section_id"]].get("decision") or "")
            for context in verified_contexts
        }
        if generated_recipe is not None:
            result = parse_workbook_layout(grids, generated_recipe)
            validation = {
                "passed": not result.needs_mapping and bool(result.items),
                "item_count": len(result.items),
                "reason": "replayed complete generated workbook recipe",
            }
            missing = []
        elif verified_contexts and not verified_decisions <= {
            "catalog_without_price",
            "not_price_section",
        }:
            decision = compile_batch_proposals(
                (
                    (migrated_proposals[context["section_id"]], context)
                    for context in verified_contexts
                ),
                loader=loader,
                grids=grids,
            )
            result, validation = dry_run_decision(grids, decision)
            if not validation["passed"]:
                raise RuntimeError(f"{relative_path}: combined dry-run failed: {validation}")
        else:
            # A readable workbook may legitimately contain no price context
            # (for example, a supplier card or an HTML download page saved
            # with an .xlsx suffix).  It still belongs in the physical-row
            # audit and must not abort reconciliation of the remaining files.
            result = ParseResult()
            validation = {
                "passed": True,
                "item_count": 0,
                "reason": (
                    "extractor produced no price context"
                    if not verified_contexts
                    else "all verified contexts contain no executable price table"
                ),
            }

        item_counts = Counter((item.source_sheet, item.source_row) for item in result.items)
        item_rows = set(item_counts)
        header_rows = {
            (str(context["sheet_name"]), int(row_index) + 1)
            for context in contexts
            for key in ("column_header_row_indexes", "category_row_indexes")
            for row_index in context.get(key, [])
        }
        if generated_recipe is not None:
            for region in generated_recipe.get("matrix_regions", []):
                if not isinstance(region, dict):
                    continue
                for block in region.get("blocks", []):
                    if not isinstance(block, dict):
                        continue
                    sheet_index = (block.get("name") or {}).get("sheet_index")
                    if not isinstance(sheet_index, int) or not 0 <= sheet_index < len(grids):
                        continue
                    sheet_name = grids[sheet_index].name
                    for row_index in (
                        block.get("column_axis_row_index"),
                        (block.get("category") or {}).get("row_index"),
                    ):
                        if isinstance(row_index, int):
                            header_rows.add((sheet_name, row_index + 1))
        catalog_rows = {
            (str(context["sheet_name"]), int(row_index) + 1)
            for context in verified_contexts
            if matched_requests[context["section_id"]]["proposal"].get("decision")
            == "catalog_without_price"
            for row_index in context.get("price_presence", {}).get("candidate_row_indexes", [])
        }
        not_price_rows = {
            (str(context["sheet_name"]), int(row_index) + 1)
            for context in verified_contexts
            if matched_requests[context["section_id"]]["proposal"].get("decision")
            == "not_price_section"
            for row_index in context.get("price_presence", {}).get("candidate_row_indexes", [])
        }
        pending_rows = {
            (str(context["sheet_name"]), int(row_index) + 1)
            for context in contexts
            if context["section_id"] in missing
            for row_index in context.get("price_presence", {}).get("candidate_row_indexes", [])
        }
        issue_counts = Counter(
            (issue.sheet, issue.row_no)
            for issue in result.issues
            if issue.sheet is not None and issue.row_no is not None
        )
        issue_rows = set(issue_counts)

        rows: list[dict[str, Any]] = []
        for grid in grids:
            for row_index, row in enumerate(grid.rows):
                values = _row_values(row)
                if not values:
                    continue
                coordinate = (grid.name, row_index + 1)
                row_class = _classify_row(
                    coordinate,
                    item_rows=item_rows,
                    header_rows=header_rows,
                    catalog_rows=catalog_rows,
                    not_price_rows=not_price_rows,
                    issue_rows=issue_rows,
                    pending_rows=pending_rows,
                )
                has_price_like_value = any(
                    parse_price(value["value"], allow_qualified=True).status
                    in {"ok", "on_request"}
                    for value in values
                )
                has_text_identity = any(
                    isinstance(value["value"], str)
                    and any(char.isalpha() for char in value["value"])
                    and parse_price(value["value"], allow_qualified=True).status
                    not in {"ok", "on_request"}
                    for value in values
                )
                rows.append(
                    {
                        "sheet": grid.name,
                        "excel_row": row_index + 1,
                        "class": row_class,
                        "item_count": item_counts.get(coordinate, 0),
                        "issue_count": issue_counts.get(coordinate, 0),
                        "unclassified_price_like": bool(
                            row_class == "unclassified"
                            and has_price_like_value
                            and has_text_identity
                        ),
                        "values": values,
                    }
                )

        class_counts = Counter(row["class"] for row in rows)
        prices.append(
            {
                "price": relative_path,
                "loader": loader,
                "sheet_count": len(grids),
                "extractor_contexts": len(contexts),
                "panel_contexts": len(panel_contexts),
                "logical_candidate_rows": logical_candidate_rows,
                "panel_physical_candidate_rows": len(panel_candidate_rows),
                "panel_logical_rows": panel_logical_rows,
                "nonempty_physical_rows": len(rows),
                "parsed_items": len(result.items),
                "parsed_item_rows": class_counts["parsed_item"],
                "unique_item_rows": len(item_rows),
                "multi_item_physical_rows": sum(count > 1 for count in item_counts.values()),
                "unique_extractor_header_rows": len(header_rows),
                "header_rows": class_counts["header"],
                "catalog_without_price_rows": class_counts["catalog_without_price"],
                "not_price_section_rows": class_counts["not_price_section"],
                "parser_issue_rows": class_counts["parser_issue"],
                "pending_model_rows": class_counts["pending_model_rule"],
                "pending_model_candidate_rows": len(pending_rows),
                "pending_rows_already_parsed": len(pending_rows & item_rows),
                "unclassified_rows": class_counts["unclassified"],
                "unclassified_price_like_rows": sum(
                    row["unclassified_price_like"] for row in rows
                ),
                "pending_section_ids": missing,
                "issue_codes": dict(Counter(issue.code for issue in result.issues)),
                "rows": rows,
            }
        )

    payload = {
        "generated_on": date.today().isoformat(),
        "method": (
            "Every non-empty physical row is matched by (sheet, 1-based Excel row) "
            "against replayed parsed items, extractor headers, catalog-without-price "
            "candidates, parser issues, or left for manual classification."
        ),
        "prices": prices,
        "totals": {
            key: sum(int(price[key]) for price in prices)
            for key in (
                "nonempty_physical_rows",
                "parsed_items",
                "parsed_item_rows",
                "unique_item_rows",
                "multi_item_physical_rows",
                "extractor_contexts",
                "panel_contexts",
                "logical_candidate_rows",
                "panel_physical_candidate_rows",
                "panel_logical_rows",
                "unique_extractor_header_rows",
                "header_rows",
                "catalog_without_price_rows",
                "not_price_section_rows",
                "parser_issue_rows",
                "pending_model_rows",
                "pending_model_candidate_rows",
                "pending_rows_already_parsed",
                "unclassified_rows",
                "unclassified_price_like_rows",
            )
        },
    }
    payload["totals"]["prices"] = len(prices)
    payload["totals"]["source_errors"] = sum("source_error" in price for price in prices)
    args.output_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--primary", type=Path, default=DEFAULT_PRIMARY)
    parser.add_argument("--repair", type=Path, default=DEFAULT_REPAIR)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--price", action="append", default=[])
    parser.add_argument("--all-spreadsheets", action="store_true")
    parser.add_argument("--exclude-reviewed-ten", action="store_true")
    args = parser.parse_args()
    payload = run(args)
    for price in payload["prices"]:
        print(
            price["price"],
            "items=", price["parsed_items"],
            "item_rows=", price["unique_item_rows"],
            "headers=", price["header_rows"],
            "catalog=", price["catalog_without_price_rows"],
            "issues=", price["parser_issue_rows"],
            "pending=", price["pending_model_rows"],
            "manual=", price["unclassified_rows"],
        )


if __name__ == "__main__":
    main()
