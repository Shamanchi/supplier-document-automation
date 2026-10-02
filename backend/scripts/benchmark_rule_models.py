#!/usr/bin/env python3
"""Compare configured Yandex Qwen models on ten reviewed price-list rules."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
DEFAULT_JSON_REPORT = APP_DIR / "reports/rule_model_benchmark_2026-07-15.json"
DEFAULT_MD_REPORT = APP_DIR / "reports/сравнение_qwen_для_генерации_правил_2026-07-15.md"


def _model_uri(model: str, folder_id: str) -> str:
    return model if model.startswith("gpt://") else f"gpt://{folder_id}/{model.lstrip('/')}"


def _short_model(model: str) -> str:
    return model.rsplit("/", 2)[-2] if model.startswith("gpt://") else model.removesuffix("/latest")


def _load_cases(audit_path: Path, corpus_dir: Path) -> list[dict[str, Any]]:
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    by_path = {record["relative_path"]: record for record in audit["price_files"]}
    cases: list[dict[str, Any]] = []
    for relative_path in DEFAULT_FILES:
        record = by_path.get(relative_path)
        if not record or not record.get("effective_recipe"):
            raise RuntimeError(f"Нет reviewed recipe для {relative_path}")
        source = corpus_dir / relative_path
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
    from app.parsing import loaders
    from app.parsing.mapping import MappingDecision
    from app.parsing.rule_proposal import (
        build_tabular_rule_generation_prompt,
        build_tabular_section_contexts,
        compile_section_proposal,
        dry_run_decision,
        parse_proposal,
    )
    from app.rule_model_benchmark import choose_winner, score_mapping
    from app.rule_proposal_service import request_rule_proposal

    folder_id = os.environ.get("YANDEXGPT_FOLDER_ID", "").strip()
    if not folder_id:
        raise RuntimeError("YANDEXGPT_FOLDER_ID не задан")
    configured_models = [
        value.strip()
        for value in os.environ.get("YANDEXGPT_RULE_MODELS", "").split(",")
        if value.strip()
    ]
    if len(configured_models) != 2:
        raise RuntimeError("YANDEXGPT_RULE_MODELS должен содержать ровно две модели")
    models = [_model_uri(model, folder_id) for model in configured_models]
    cases = _load_cases(args.audit, args.corpus)
    results: list[dict[str, Any]] = []

    for case in cases:
        data = case["source"].read_bytes()
        loader = loaders.detect_loader(data, case["source"].name)
        grids = loaders.load_grids_for(loader, data, case["source"].name)
        contexts = build_tabular_section_contexts(grids)
        try:
            gold_sheet, context = _find_reviewed_section(
                case["record"]["effective_recipe"], contexts
            )
        except Exception as exc:  # extraction failure is a benchmark result
            for model in models:
                results.append(
                    {
                        "price": case["relative_path"],
                        "model": _short_model(model),
                        "loader": loader,
                        "stage": "section_extraction",
                        "valid": False,
                        "error": str(exc),
                    }
                )
            continue

        for model in models:
            row: dict[str, Any] = {
                "price": case["relative_path"],
                "model": _short_model(model),
                "loader": loader,
                "sheet_name": context["sheet_name"],
                "header_row_index": context["header_row_index"],
                "section_id": context["section_id"],
                "horizontal_panels": len(context.get("horizontal_panels") or []),
                "stage": "request",
                "valid": False,
            }
            prompt = build_tabular_rule_generation_prompt(
                source_context=context, loader=loader, model=model
            )
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
                score = score_mapping(gold_sheet, proposal.mapping.model_dump(mode="json"))
                row.update(score)
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
                row["table_count"] = validation["table_count"]
                row["valid"] = bool(
                    row["header_row_exact"]
                    and row["name_price_columns_exact"]
                    and row["compiler_valid"]
                    and row["dry_run_valid"]
                )
                row["stage"] = "complete"
            except Exception as exc:  # retain one concise, non-secret diagnostic
                row.setdefault("latency_seconds", round(time.perf_counter() - started, 3))
                row["error"] = " ".join(str(exc).split())[:500]
            results.append(row)
            print(
                f"{row['model']:<26} {case['relative_path']:<52} "
                f"valid={row['valid']} stage={row['stage']}",
                flush=True,
            )

    summary: dict[str, dict[str, Any]] = {}
    for model in map(_short_model, models):
        model_rows = [row for row in results if row["model"] == model]
        scored = [row for row in model_rows if "semantic_role_f1" in row]
        timed = [row for row in model_rows if "latency_seconds" in row]
        summary[model] = {
            "cases": len(model_rows),
            "valid_cases": sum(bool(row.get("valid")) for row in model_rows),
            "schema_valid_cases": sum(bool(row.get("schema_valid")) for row in model_rows),
            "compiler_valid_cases": sum(bool(row.get("compiler_valid")) for row in model_rows),
            "dry_run_valid_cases": sum(bool(row.get("dry_run_valid")) for row in model_rows),
            "exact_name_price_cases": sum(
                bool(row.get("name_price_columns_exact")) for row in model_rows
            ),
            "mean_semantic_role_f1": round(
                statistics.fmean(row["semantic_role_f1"] for row in scored), 4
            )
            if scored
            else 0.0,
            "mean_latency_seconds": round(
                statistics.fmean(row["latency_seconds"] for row in timed), 3
            )
            if timed
            else 0.0,
        }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "contract": "tabular-section-mapping-v1",
        "cases_count": len(cases),
        "models": list(map(_short_model, models)),
        "winner": choose_winner(summary),
        "selection_order": [
            "valid_cases",
            "mean_semantic_role_f1",
            "mean_latency_seconds (tie-breaker)",
        ],
        "summary": summary,
        "results": results,
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Сравнение Qwen для генерации правил прайс-листов",
        "",
        f"Дата фактического прогона: {report['generated_at']}",
        "",
        "Обе модели получили одинаковые ограниченные секции из 10 прайсов с уже проверенными рецептами. "
        "Оценка учитывает соответствие name/price эталону, локальную компиляцию и полный dry-run; "
        "ответ модели сам по себе правило не активирует.",
        "",
        "| Модель | Валидно | JSON Schema | Компилятор | Dry-run | Точные name/price | Средний F1 ролей | Среднее время, с |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for model, value in report["summary"].items():
        lines.append(
            f"| `{model}` | {value['valid_cases']}/{value['cases']} | "
            f"{value['schema_valid_cases']}/{value['cases']} | "
            f"{value['compiler_valid_cases']}/{value['cases']} | "
            f"{value['dry_run_valid_cases']}/{value['cases']} | "
            f"{value['exact_name_price_cases']}/{value['cases']} | "
            f"{value['mean_semantic_role_f1']:.4f} | {value['mean_latency_seconds']:.3f} |"
        )
    lines.extend(
        [
            "",
            f"Рекомендованная модель по заранее заданному safety-first порядку: `{report['winner']}`.",
            "",
            "## Результаты по прайсам",
            "",
            "| Прайс | Модель | Секция | Горизонтальных блоков | Валидно | F1 ролей | Время, с | Диагностика |",
            "| --- | --- | --- | ---: | --- | ---: | ---: | --- |",
        ]
    )
    for row in report["results"]:
        diagnostic = str(row.get("error", "—")).replace("|", "\\|")
        lines.append(
            f"| `{row['price']}` | `{row['model']}` | "
            f"{row.get('sheet_name', '—')} / {row.get('header_row_index', '—')} | "
            f"{row.get('horizontal_panels', 0)} | {'да' if row.get('valid') else 'нет'} | "
            f"{row.get('semantic_role_f1', 0):.4f} | {row.get('latency_seconds', 0):.3f} | {diagnostic} |"
        )
    lines.extend(
        [
            "",
            "## Проверяемый контур",
            "",
            "1. Детерминированный extractor находит лист, повторные шапки, границы вертикальных секций и независимые горизонтальные панели.",
            "2. Модель получает только redacted header rows и ограниченную выборку строк секции.",
            "3. Structured Output разрешает закрытый набор ролей и координаты только исходной секции.",
            "4. Локальный компилятор отклоняет выдуманные листы, строки, колонки, дубли и неполную пару name/price.",
            "5. Полный dry-run по исходному файлу применяет quality gate. Результат остаётся draft до ручной проверки и активации.",
            "",
            "Машиночитаемые результаты находятся в соседнем JSON-отчёте.",
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
    args.md_report.write_text(_markdown(report), encoding="utf-8")
    print(f"winner={report['winner']}")
    print(args.json_report)
    print(args.md_report)


if __name__ == "__main__":
    main()
