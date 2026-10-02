#!/usr/bin/env python3
"""A/B/C/D benchmark of 1, 3, 4 and 5 item rows for fixed Qwen 235B."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROW_COUNTS = (1, 3, 4, 5)
FIXED_MODEL = "qwen3-235b-a22b-fp8/latest"
DEFAULT_FILES = [
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
DEFAULT_JSON_REPORT = APP_DIR / "reports/sample_rows_ab_qwen235_2026-07-15.json"
DEFAULT_MD_REPORT = APP_DIR / "reports/ab_1_3_4_5_строк_qwen235_2026-07-15.md"


def _load_cases(audit_path: Path, corpus_dir: Path) -> list[dict[str, Any]]:
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    by_path = {record["relative_path"]: record for record in audit["price_files"]}
    cases: list[dict[str, Any]] = []
    for relative_path in DEFAULT_FILES:
        record = by_path.get(relative_path)
        source = corpus_dir / relative_path
        if not record or not record.get("effective_recipe"):
            raise RuntimeError(f"Нет reviewed recipe для {relative_path}")
        if not source.is_file():
            raise RuntimeError(f"Не найден исходный прайс {source}")
        cases.append({"relative_path": relative_path, "source": source, "record": record})
    return cases


def _find_reviewed_section(
    recipe: dict[str, Any], contexts: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    for gold_sheet in recipe.get("sheets", []):
        if gold_sheet.get("skip"):
            continue
        match = gold_sheet.get("match") or {}
        for context in contexts:
            if (
                context.get("sheet_name") == match.get("name_pattern")
                and context.get("header_row_index") == gold_sheet.get("header_row")
            ):
                return gold_sheet, context
    raise RuntimeError("Reviewed sheet/header не найден среди извлечённых секций")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    sys.path.insert(0, str(BACKEND_DIR))
    from app.config import settings
    from app.parsing import loaders
    from app.parsing.mapping import MappingDecision
    from app.parsing.rule_proposal import (
        build_tabular_rule_generation_prompt,
        build_tabular_section_contexts,
        compile_section_proposal,
        dry_run_decision,
        parse_proposal,
    )
    from app.rule_model_benchmark import (
        choose_sample_rows_winner,
        score_mapping,
        summarize_by_sample_rows,
    )
    from app.rule_proposal_service import request_rule_proposal

    model = settings.rule_model
    if not model.endswith("/" + FIXED_MODEL):
        raise RuntimeError(f"Benchmark разрешён только для зафиксированной модели {FIXED_MODEL}")
    cases = _load_cases(args.audit, args.corpus)
    results: list[dict[str, Any]] = []

    async def run_arm(
        *, case: dict[str, Any], loader: str, grids, row_count: int
    ) -> dict[str, Any]:
        contexts = build_tabular_section_contexts(grids, sample_rows_limit=row_count)
        gold_sheet, context = _find_reviewed_section(
            case["record"]["effective_recipe"], contexts
        )
        actual_rows = len(context["sample_rows"])
        row: dict[str, Any] = {
            "price": case["relative_path"],
            "model": FIXED_MODEL,
            "loader": loader,
            "sheet_name": context["sheet_name"],
            "header_row_index": context["header_row_index"],
            "section_id": context["section_id"],
            "sample_rows_requested": row_count,
            "sample_rows_actual": actual_rows,
            "sample_rows_available": context["sample_rows_available"],
            "sample_row_indexes": [item["row_index"] for item in context["sample_rows"]],
            "horizontal_panels": len(context.get("horizontal_panels") or []),
            "extractor_valid": actual_rows
            == min(row_count, context["sample_rows_available"])
            and actual_rows > 0,
            "stage": "request",
            "valid": False,
        }
        prompt = build_tabular_rule_generation_prompt(
            source_context=context, loader=loader, model=model
        )
        row["prompt_chars"] = len(prompt)
        started = time.perf_counter()
        try:
            raw = await request_rule_proposal(prompt=prompt, model=model)
            row["latency_seconds"] = round(time.perf_counter() - started, 3)
            row["proposal"] = raw
            proposal = parse_proposal(raw)
            row["schema_valid"] = True
            row["decision"] = proposal.decision
            if proposal.mapping is None:
                raise RuntimeError(f"Модель вернула {proposal.decision} без mapping")
            row.update(score_mapping(gold_sheet, proposal.mapping.model_dump(mode="json")))
            compiled = compile_section_proposal(
                proposal, source_context=context, loader=loader, grids=grids
            )
            row["compiler_valid"] = compiled.table is not None
            if compiled.table is None:
                raise RuntimeError("Локальный компилятор не создал TableMapping")
            _, validation = dry_run_decision(
                grids, MappingDecision(tables=[compiled.table], auto_ok=True)
            )
            row["dry_run_valid"] = bool(validation["passed"])
            row["item_count"] = validation["item_count"]
            row["valid"] = bool(
                row["extractor_valid"]
                and row["header_row_exact"]
                and row["name_price_columns_exact"]
                and row["compiler_valid"]
                and row["dry_run_valid"]
            )
            row["stage"] = "complete"
        except Exception as exc:  # concise non-secret diagnostic in report
            row.setdefault("latency_seconds", round(time.perf_counter() - started, 3))
            row["error"] = " ".join(str(exc).split())[:500]
        print(
            f"rows={row_count} {case['relative_path']:<52} "
            f"extractor={row['extractor_valid']} valid={row['valid']} stage={row['stage']}",
            flush=True,
        )
        return row

    for case in cases:
        data = case["source"].read_bytes()
        loader = loaders.detect_loader(data, case["source"].name)
        grids = loaders.load_grids_for(loader, data, case["source"].name)
        results.extend(
            await asyncio.gather(
                *(
                    run_arm(case=case, loader=loader, grids=grids, row_count=row_count)
                    for row_count in ROW_COUNTS
                )
            )
        )

    summary = summarize_by_sample_rows(results, row_counts=ROW_COUNTS)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": FIXED_MODEL,
        "contract": "tabular-section-mapping-v1",
        "cases_count": len(cases),
        "arms": list(ROW_COUNTS),
        "requests_count": len(results),
        "best_sample_rows": choose_sample_rows_winner(summary),
        "selection_order": [
            "valid_cases",
            "mean_semantic_role_f1",
            "fewer sample rows (tie-breaker)",
        ],
        "summary": summary,
        "results": results,
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# A/B/C/D: 1, 3, 4 или 5 строк прайса",
        "",
        f"Дата фактического прогона: {report['generated_at']}",
        f"Модель: `{report['model']}` (зафиксирована для дальнейшего использования).",
        "",
        "Каждая конфигурация получила тот же reviewed header, но разное число первых реальных "
        "товарных строк. Для короткой секции extractor передаёт все доступные строки.",
        "",
        "| Строк | Extractor | Полностью валидно | JSON Schema | Компилятор | Dry-run | Точные name/price | Средний F1 | Δ F1 к 1 строке | Среднее время, с |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row_count in report["arms"]:
        value = report["summary"][str(row_count)]
        lines.append(
            f"| {row_count} | {value['extractor_valid_cases']}/{value['cases']} | "
            f"{value['valid_cases']}/{value['cases']} | {value['schema_valid_cases']}/{value['cases']} | "
            f"{value['compiler_valid_cases']}/{value['cases']} | {value['dry_run_valid_cases']}/{value['cases']} | "
            f"{value['exact_name_price_cases']}/{value['cases']} | {value['mean_semantic_role_f1']:.4f} | "
            f"{value['semantic_role_f1_delta_vs_one']:+.4f} | {value['mean_latency_seconds']:.3f} |"
        )
    best = report["best_sample_rows"]
    lines.extend(
        [
            "",
            f"Лучший вариант по safety-first критерию: **header + {best} товарных строк**.",
            "",
            "## Результаты по файлам",
            "",
            "| Прайс | Строк | Индексы строк | Extractor | Валидно | F1 | Диагностика |",
            "| --- | ---: | --- | --- | --- | ---: | --- |",
        ]
    )
    for row in report["results"]:
        diagnostic = str(row.get("error", "—")).replace("|", "\\|")
        lines.append(
            f"| `{row['price']}` | {row['sample_rows_requested']} | "
            f"`{row['sample_row_indexes']}` | {'да' if row['extractor_valid'] else 'нет'} | "
            f"{'да' if row['valid'] else 'нет'} | {row.get('semantic_role_f1', 0):.4f} | {diagnostic} |"
        )
    lines.extend(
        [
            "",
            "Полные Structured Output ответы и метрики находятся в соседнем JSON-отчёте. "
            "Отказ или ошибка модели не ослабляет extractor/compiler gate и не активирует правило.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--json-report", type=Path, default=DEFAULT_JSON_REPORT)
    parser.add_argument("--md-report", type=Path, default=DEFAULT_MD_REPORT)
    args = parser.parse_args()
    report = asyncio.run(_run(args))
    args.json_report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # JSON object keys become strings after serialization; use that same
    # representation for the Markdown renderer.
    rendered_report = json.loads(json.dumps(report, ensure_ascii=False))
    args.md_report.write_text(_markdown(rendered_report), encoding="utf-8")
    print(f"best_sample_rows={report['best_sample_rows']}")
    print(args.json_report)
    print(args.md_report)


if __name__ == "__main__":
    main()
