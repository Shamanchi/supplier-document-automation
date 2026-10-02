#!/usr/bin/env python3
"""Materialize the 10-file/86-section golden extractor gate."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR.parent
DEFAULT_CORPUS = APP_DIR.parents[1] / "external_price_corpus"
DEFAULT_AUDIT = APP_DIR / "reports/full_price_audit_2026-07-12/rules_for_all_prices.json"
DEFAULT_JSON_REPORT = APP_DIR / "reports/extractor_header_rows_audit_2026-07-15.json"
DEFAULT_MD_REPORT = APP_DIR / "reports/аудит_extractor_header_rows_2026-07-15.md"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--json-report", type=Path, default=DEFAULT_JSON_REPORT)
    parser.add_argument("--md-report", type=Path, default=DEFAULT_MD_REPORT)
    args = parser.parse_args()

    sys.path.insert(0, str(BACKEND_DIR))
    from app.parsing import loaders
    from app.parsing.normalize import parse_price
    from app.parsing.rule_proposal import build_tabular_section_contexts

    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    by_path = {record["relative_path"]: record for record in audit["price_files"]}
    results: list[dict[str, Any]] = []

    def has_value(row, index: int) -> bool:
        return index < len(row) and row[index].value is not None and str(row[index].value).strip() != ""

    for relative_path in BENCHMARK_FILES:
        record = by_path[relative_path]
        source = args.corpus / relative_path
        data = source.read_bytes()
        loader = loaders.detect_loader(data, source.name)
        grids = loaders.load_grids_for(loader, data, source.name)
        grids_by_name = {grid.name: grid for grid in grids}
        contexts = build_tabular_section_contexts(grids, sample_rows_limit=5)
        contexts_by_key = {
            (context["sheet_name"], context["header_row_index"]): context
            for context in contexts
        }
        for gold_sheet in record["effective_recipe"]["sheets"]:
            if gold_sheet.get("skip"):
                continue
            sheet_name = gold_sheet["match"]["name_pattern"]
            header_row = gold_sheet["header_row"]
            context = contexts_by_key.get((sheet_name, header_row))
            row: dict[str, Any] = {
                "price": relative_path,
                "sheet_name": sheet_name,
                "reviewed_header_row_index": header_row,
                "header_exact": context is not None,
                "rows_exact": False,
                "passed": False,
            }
            if context is not None:
                grid = grids_by_name[sheet_name]
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
                expected = [
                    row_index
                    for row_index in range(header_row + 1, context["section_end_row_index"])
                    if any(has_value(grid.rows[row_index], index) for index in name_columns)
                    and any(
                        index < len(grid.rows[row_index])
                        and parse_price(grid.rows[row_index][index].value).status
                        in {"ok", "on_request"}
                        for index in price_columns
                    )
                ][:5]
                actual = [item["row_index"] for item in context["sample_rows"]]
                row.update(
                    {
                        "section_id": context["section_id"],
                        "expected_item_row_indexes": expected,
                        "actual_item_row_indexes": actual,
                        "rows_exact": bool(expected) and actual == expected,
                        "sample_rows_available": context["sample_rows_available"],
                    }
                )
                row["passed"] = row["header_exact"] and row["rows_exact"]
            results.append(row)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": len(BENCHMARK_FILES),
        "reviewed_sections": len(results),
        "passed_sections": sum(bool(row["passed"]) for row in results),
        "failed_sections": sum(not bool(row["passed"]) for row in results),
        "sample_rows_limit": 5,
        "results": results,
    }
    args.json_report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# Аудит extractor: header + товарные строки",
        "",
        f"Дата: {report['generated_at']}",
        "",
        f"Проверено файлов: **{report['files']}**. Reviewed-секций: **{report['reviewed_sections']}**. "
        f"Прошло: **{report['passed_sections']}/{report['reviewed_sections']}**.",
        "",
        "Для каждой секции проверены точные имя листа и индекс reviewed-шапки, затем первые "
        "пять реальных строк с наименованием и распознаваемой ценой. Если строк меньше пяти, "
        "ожидаются все доступные.",
        "",
        "| Прайс | Лист | Header row | Строки | Результат |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for row in results:
        lines.append(
            f"| `{row['price']}` | {str(row['sheet_name']).replace('|', chr(92) + '|')} | "
            f"{row['reviewed_header_row_index']} | `{row.get('actual_item_row_indexes', [])}` | "
            f"{'PASS' if row['passed'] else 'FAIL'} |"
        )
    lines.append("")
    args.md_report.write_text("\n".join(lines), encoding="utf-8")
    print(f"extractor={report['passed_sections']}/{report['reviewed_sections']}")
    print(args.json_report)
    print(args.md_report)


if __name__ == "__main__":
    main()
