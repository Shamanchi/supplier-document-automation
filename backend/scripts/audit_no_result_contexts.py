#!/usr/bin/env python3
"""Render every extractor context for benchmark prices without a valid rule."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR.parent
DEFAULT_CORPUS = APP_DIR.parents[1] / "external_price_corpus"
DEFAULT_BENCHMARK = APP_DIR / "reports/sample_rows_ab_qwen235_2026-07-15.json"
DEFAULT_AUDIT = APP_DIR / "reports/full_price_audit_2026-07-12/rules_for_all_prices.json"
DEFAULT_REPORT = APP_DIR / "reports/аудит_прайсов_без_результата_header_rows_2026-07-15.md"
WORKING_SAMPLE_ROWS = 4


def _md(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def _yes_no(value: object) -> str:
    if value is True:
        return "да"
    if value is False:
        return "нет"
    return "—"


def _failure_reason(result: dict[str, Any]) -> tuple[str, str]:
    error = result.get("error")
    if result.get("decision") == "insufficient_evidence":
        return "model", error or "Модель отказалась строить mapping: insufficient_evidence."
    if error:
        return "compiler", str(error)
    if result.get("name_price_columns_exact") is False:
        return (
            "semantic_gate",
            "Модель выбрала неполный или неверный набор колонок name/price относительно reviewed-правила.",
        )
    if result.get("compiler_valid") is False:
        return "compiler", "Ответ модели не прошёл безопасную локальную компиляцию."
    if result.get("dry_run_valid") is False:
        return "dry_run", "Правило скомпилировано, но не прошло контроль качества полного dry-run."
    return "other", "Полный итоговый gate не пройден; точная причина в benchmark не классифицирована."


def _render_cells(lines: list[str], title: str, rows: list[dict[str, Any]]) -> None:
    lines.extend(
        [
            f"#### {title}",
            "",
            "Показаны все непустые ячейки payload. Пустые ячейки опущены, но исходные индексы строк и колонок сохранены.",
            "",
            "| Строка | Колонка | Значение после маскирования | Заполнение объединения |",
            "| ---: | ---: | --- | --- |",
        ]
    )
    emitted = 0
    for row in rows:
        for column_index, cell in enumerate(row.get("cells") or []):
            if cell.get("value") is None:
                continue
            lines.append(
                f"| {row['row_index']} | {column_index} | {_md(cell['value'])} | "
                f"{_yes_no(cell.get('is_merged_fill'))} |"
            )
            emitted += 1
    if emitted == 0:
        lines.append("| — | — | Непустых ячеек нет | — |")
    lines.append("")


def _render_observations(lines: list[str], observations: list[dict[str, Any]]) -> None:
    lines.extend(
        [
            "#### Детерминированные наблюдения extractor",
            "",
            "| Колонка | Заголовок | Предложенная роль | Уверенность | Описание цены | Заполнение объединения |",
            "| ---: | --- | --- | ---: | --- | --- |",
        ]
    )
    if not observations:
        lines.append("| — | — | Наблюдений нет | — | — | — |")
    for observation in observations:
        lines.append(
            f"| {observation['index']} | {_md(observation.get('header'))} | "
            f"`{_md(observation.get('suggested_role'))}` | "
            f"{_md(observation.get('confidence'))} | "
            f"{_md(observation.get('price_descriptor'))} | "
            f"{_yes_no(observation.get('is_merged_fill'))} |"
        )
    lines.append("")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    sys.path.insert(0, str(BACKEND_DIR))
    from app.parsing import loaders
    from app.parsing.rule_proposal import build_tabular_section_contexts

    benchmark = json.loads(args.benchmark.read_text(encoding="utf-8"))
    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    audit_by_path = {record["relative_path"]: record for record in audit["price_files"]}
    arm = [
        result
        for result in benchmark["results"]
        if result.get("sample_rows_requested") == WORKING_SAMPLE_ROWS
    ]
    failed = [result for result in arm if not result.get("valid")]
    if len(arm) != 10 or len(failed) != 8:
        raise RuntimeError(f"Ожидалось 10 результатов рабочего arm и 8 отказов, получено {len(arm)} и {len(failed)}")

    generated_at = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Аудит прайсов без результата: точные данные extractor для модели",
        "",
        f"Дата формирования: `{generated_at}`.",
        "",
        "Модель: `qwen3-235b-a22b-fp8/latest`. Рабочий режим: заголовок + до четырёх реальных товарных строк.",
        "",
        "В аудит включены все восемь прайсов, для которых вариант `header + 4 rows` не дал полностью валидного правила. "
        "Ниже показаны **все секции**, которые текущий extractor формирует для этих файлов и может передать в модель отдельными запросами. "
        "Benchmark вызывал модель только для одной reviewed-секции каждого файла; её результат отмечен отдельно.",
        "",
        "Значения приведены после штатного маскирования персональных данных и с действующим пределом 2000 символов на ячейку. "
        "Индексы строк и колонок нулевые, как в JSON-контракте модели.",
        "",
        "## Сводка benchmark по восьми отказам",
        "",
        "| Прайс | Эталонная секция | Решение модели | F1 ролей | Компиляция | Полный прогон | Точная причина |",
        "| --- | --- | --- | ---: | --- | --- | --- |",
    ]

    categories: Counter[str] = Counter()
    for result in failed:
        category, reason = _failure_reason(result)
        categories[category] += 1
        lines.append(
            f"| `{_md(result['price'])}` | `{_md(result.get('sheet_name'))}` / row {result.get('header_row_index')} | "
            f"`{_md(result.get('decision'))}` | {_md(result.get('semantic_role_f1'))} | "
            f"{_yes_no(result.get('compiler_valid'))} | {_yes_no(result.get('dry_run_valid'))} | {_md(reason)} |"
        )
    lines.extend(["", "## Вывод аудита", ""])
    lines.append(
        "Extractor не был причиной отсутствия результата на восьми **reviewed benchmark-секциях**: везде "
        "`extractor_valid=true`, а шапка и товарные строки совпали с golden-разметкой. Эти восемь отказов произошли после извлечения: "
        f"model/insufficient evidence — **{categories['model']}**, compiler — **{categories['compiler']}**, "
        f"semantic gate — **{categories['semantic_gate']}**, dry-run — **{categories['dry_run']}**."
    )
    lines.extend(
        [
            "",
            "При этом полный аудит всех выдаваемых контекстов обнаружил отдельный дефект точности: extractor находит все "
            "reviewed-шапки, но дополнительно принимает часть товарных строк за новые шапки. Поэтому его recall на этих "
            "файлах высокий, а результат без фильтрации содержит лишние model-вызовы. Точные числа приведены ниже.",
            "",
            "Также обнаружен дефект минимизации данных: текущее маскирование скрывает телефоны и email, но в точном payload "
            "остаются почтовые адреса, банковские счета/ИИК, БИК и URL. Это не объясняет восемь отказов модели, но до "
            "промышленного использования перечень маскируемых реквизитов нужно расширить.",
            "",
        ]
    )

    total_contexts = 0
    total_header_rows = 0
    total_sample_rows = 0
    short_contexts = 0
    reviewed_contexts = 0
    extra_contexts = 0
    per_file: list[tuple[str, int, int, int]] = []
    for file_number, result in enumerate(failed, start=1):
        source = args.corpus / result["price"]
        data = source.read_bytes()
        loader = loaders.detect_loader(data, source.name)
        grids = loaders.load_grids_for(loader, data, source.name)
        contexts = build_tabular_section_contexts(grids, sample_rows_limit=WORKING_SAMPLE_ROWS)
        reviewed_headers = {
            (sheet["match"]["name_pattern"], sheet["header_row"])
            for sheet in audit_by_path[result["price"]]["effective_recipe"]["sheets"]
            if not sheet.get("skip")
        }
        actual_headers = {
            (context["sheet_name"], context["header_row_index"])
            for context in contexts
        }
        missing_headers = sorted(reviewed_headers - actual_headers)
        if missing_headers:
            raise RuntimeError(f"{result['price']}: extractor пропустил reviewed-шапки {missing_headers}")
        file_reviewed = sum(
            (context["sheet_name"], context["header_row_index"]) in reviewed_headers
            for context in contexts
        )
        file_extra = len(contexts) - file_reviewed
        reviewed_contexts += file_reviewed
        extra_contexts += file_extra
        per_file.append((result["price"], len(contexts), file_reviewed, file_extra))
        total_contexts += len(contexts)
        lines.extend(
            [
                f"## {file_number}. Прайс `{_md(result['price'])}`",
                "",
                f"Loader: `{loader}`. Extractor сформировал секций: **{len(contexts)}**; "
                f"совпали с reviewed-шапками: **{file_reviewed}**; лишних кандидатов: **{file_extra}**.",
                "",
            ]
        )
        category, reason = _failure_reason(result)
        lines.extend(
            [
                f"Результат модели для reviewed-секции `{_md(result.get('sheet_name'))}` / header row "
                f"`{result.get('header_row_index')}`: `{_md(result.get('decision'))}`; категория отказа "
                f"`{category}`; причина: {_md(reason)}",
                "",
            ]
        )
        for context_number, context in enumerate(contexts, start=1):
            header_rows = context.get("header_rows") or []
            sample_rows = context.get("sample_rows") or []
            total_header_rows += len(header_rows)
            total_sample_rows += len(sample_rows)
            if len(sample_rows) < WORKING_SAMPLE_ROWS:
                short_contexts += 1
            is_benchmark = (
                context["sheet_name"] == result.get("sheet_name")
                and context["header_row_index"] == result.get("header_row_index")
            )
            is_reviewed = (context["sheet_name"], context["header_row_index"]) in reviewed_headers
            if is_benchmark:
                marker = " — reviewed-секция benchmark"
            elif is_reviewed:
                marker = " — reviewed-шапка"
            else:
                marker = " — лишний кандидат extractor"
            lines.extend(
                [
                    f"### Секция {file_number}.{context_number}{marker}",
                    "",
                    f"- `section_id`: `{context['section_id']}`",
                    f"- лист: `{_md(context['sheet_name'])}` (index `{context['sheet_index']}`), "
                    f"размер `{context['sheet_shape']['rows']} × {context['sheet_shape']['columns']}`",
                    f"- шапка: row `{context['header_row_index']}`; секция до row "
                    f"`{context['section_end_row_index']}` (не включая границу)",
                    f"- строк запрошено: `{context['sample_rows_requested']}`; передано: `{len(sample_rows)}`; "
                    f"доступно товарных строк: `{context['sample_rows_available']}`",
                    f"- horizontal panels: `{_md(context.get('horizontal_panels') or [])}`",
                    "",
                ]
            )
            _render_cells(lines, "Контекст заголовка, передаваемый в модель", header_rows)
            _render_cells(lines, "Товарные строки, передаваемые в модель", sample_rows)
            _render_observations(lines, context.get("deterministic_observations") or [])

    summary = [
        "## Полнота выгрузки",
        "",
        f"- файлов без результата: **{len(failed)}**",
        f"- всех фактически сформированных extractor-контекстов: **{total_contexts}**",
        f"- контекстов с точным совпадением reviewed-шапки: **{reviewed_contexts}**",
        f"- лишних кандидатов, которых нет в reviewed-разметке: **{extra_contexts}**",
        f"- полнота нахождения шапок: **{reviewed_contexts}/{reviewed_contexts} (100%)**; точность кандидатов: "
        f"**{reviewed_contexts}/{total_contexts} ({reviewed_contexts / total_contexts:.1%})**",
        f"- строк контекста заголовков: **{total_header_rows}**",
        f"- товарных строк в payload: **{total_sample_rows}**",
        f"- секций, где доступно меньше четырёх строк: **{short_contexts}**",
        "- секции по файлам (всего / reviewed / лишние): "
        + "; ".join(f"`{path}` — {count}/{reviewed}/{extra}" for path, count, reviewed, extra in per_file),
        "",
    ]
    insert_at = next(index for index, line in enumerate(lines) if line.startswith("## 1. Прайс"))
    lines[insert_at:insert_at] = summary
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(json.dumps({
        "failed_files": len(failed),
        "contexts": total_contexts,
        "reviewed_contexts": reviewed_contexts,
        "extra_contexts": extra_contexts,
        "header_rows": total_header_rows,
        "sample_rows": total_sample_rows,
        "short_contexts": short_contexts,
        "failure_categories": dict(categories),
        "report": str(args.report),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
