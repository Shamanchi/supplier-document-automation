#!/usr/bin/env python3
"""Render every semantic header/category/items context for the ten-price corpus."""
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
DEFAULT_REPORT = APP_DIR / "reports/аудит_смысловых_кусков_10_прайсов_2026-07-15.md"
SAMPLE_ROWS = 4
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


def _md(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def _nonempty_preview(grid, limit: int = 3) -> str:
    values: list[str] = []
    for row in grid.rows:
        for cell in row:
            if cell.is_merged_fill or cell.value is None:
                continue
            text = " ".join(str(cell.value).split())
            if text:
                values.append(text[:120])
            if len(values) == limit:
                return " · ".join(values)
    return " · ".join(values) if values else "пустой лист"


def _render_rows(lines: list[str], title: str, rows: list[dict[str, Any]]) -> int:
    lines.extend(
        [
            f"#### {title}",
            "",
            "| Строка | Колонка | Значение после маскирования | Жирный | Заполнение объединения |",
            "| ---: | ---: | --- | --- | --- |",
        ]
    )
    emitted = 0
    for row in rows:
        for column_index, cell in enumerate(row.get("cells") or []):
            if cell.get("value") is None:
                continue
            lines.append(
                f"| {row['row_index']} | {column_index} | {_md(cell['value'])} | "
                f"{'да' if cell.get('is_bold') else 'нет'} | "
                f"{'да' if cell.get('is_merged_fill') else 'нет'} |"
            )
            emitted += 1
    if not emitted:
        lines.append("| — | — | Непустых ячеек нет | — | — |")
    lines.append("")
    return emitted


def _render_observations(lines: list[str], observations: list[dict[str, Any]]) -> None:
    lines.extend(
        [
            "#### Роли колонок, найденные до вызова модели",
            "",
            "| Колонка | Исходная строка шапки | Заголовок | Роль | Уверенность | Цена |",
            "| ---: | ---: | --- | --- | ---: | --- |",
        ]
    )
    for observation in observations:
        lines.append(
            f"| {observation['index']} | {_md(observation.get('source_header_row_index'))} | "
            f"{_md(observation.get('header'))} | `{_md(observation.get('suggested_role'))}` | "
            f"{_md(observation.get('confidence'))} | {_md(observation.get('price_descriptor'))} |"
        )
    lines.append("")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    sys.path.insert(0, str(BACKEND_DIR))
    from app.parsing import loaders
    from app.parsing.rule_proposal import build_tabular_section_contexts

    files: list[dict[str, Any]] = []
    total_contexts = 0
    total_samples = 0
    total_categories = 0
    total_excluded_sheets = 0
    for relative_path in BENCHMARK_FILES:
        source = args.corpus / relative_path
        data = source.read_bytes()
        loader = loaders.detect_loader(data, source.name)
        grids = loaders.load_grids_for(loader, data, source.name)
        contexts = build_tabular_section_contexts(grids, sample_rows_limit=SAMPLE_ROWS)
        selected_sheets = {context["sheet_name"] for context in contexts}
        excluded = [grid for grid in grids if grid.name not in selected_sheets]
        files.append(
            {
                "path": relative_path,
                "loader": loader,
                "grids": grids,
                "contexts": contexts,
                "excluded": excluded,
            }
        )
        total_contexts += len(contexts)
        total_samples += sum(len(context["sample_rows"]) for context in contexts)
        total_categories += sum(len(context["category_rows"]) for context in contexts)
        total_excluded_sheets += len(excluded)

    generated_at = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Аудит смысловых кусков десяти тестовых прайсов",
        "",
        f"Сформировано: `{generated_at}`. Модель: `qwen3-235b-a22b-fp8/latest`.",
        "",
        "Единица передачи в модель: **структурная шапка колонок + категория/подзаголовок + до четырёх "
        "первых реальных строк позиций**. Один лист может дать много независимых кусков. Карточки поставщика, "
        "контактные и иллюстрационные листы без безопасно найденной пары name+price не передаются.",
        "",
        "Индексы строк и колонок нулевые. Показаны все непустые значения точного контекста модели; пустые "
        "ячейки не печатаются, но координаты остальных сохраняются. Значения проходят штатное маскирование.",
        "",
        "## Общая сводка",
        "",
        f"- файлов: **{len(files)}**",
        f"- смысловых кусков: **{total_contexts}**",
        f"- строк категорий/подзаголовков: **{total_categories}**",
        f"- товарных строк, фактически передаваемых в модель: **{total_samples}**",
        f"- исключённых непрайсовых листов: **{total_excluded_sheets}**",
        "",
        "| Прайс | Всего листов | Прайсовых листов | Кусков | Категорий | Строк позиций |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for file in files:
        contexts = file["contexts"]
        lines.append(
            f"| `{_md(file['path'])}` | {len(file['grids'])} | "
            f"{len({context['sheet_name'] for context in contexts})} | {len(contexts)} | "
            f"{sum(len(context['category_rows']) for context in contexts)} | "
            f"{sum(len(context['sample_rows']) for context in contexts)} |"
        )
    lines.append("")

    for file_number, file in enumerate(files, start=1):
        contexts = file["contexts"]
        by_sheet = Counter(context["sheet_name"] for context in contexts)
        lines.extend(
            [
                f"## {file_number}. `{_md(file['path'])}`",
                "",
                f"Loader: `{file['loader']}`. Прайсовых листов: **{len(by_sheet)}**. "
                f"Смысловых кусков: **{len(contexts)}**.",
                "",
                "Куски по листам: "
                + "; ".join(f"`{_md(name)}` — {count}" for name, count in by_sheet.items())
                + ".",
                "",
            ]
        )
        if file["excluded"]:
            lines.extend(
                [
                    "### Исключённые листы",
                    "",
                    "Эти листы не передаются модели: extractor не нашёл безопасную структурную пару name+price.",
                    "",
                    "| Лист | Размер | Первые значения |",
                    "| --- | --- | --- |",
                ]
            )
            for grid in file["excluded"]:
                lines.append(
                    f"| `{_md(grid.name)}` | {len(grid.rows)} × {grid.n_cols} | "
                    f"{_md(_nonempty_preview(grid))} |"
                )
            lines.append("")

        sheet_ordinals: Counter[str] = Counter()
        for context_number, context in enumerate(contexts, start=1):
            sheet_ordinals[context["sheet_name"]] += 1
            lines.extend(
                [
                    f"### Кусок {file_number}.{context_number}: лист `{_md(context['sheet_name'])}`, "
                    f"№ {sheet_ordinals[context['sheet_name']]}",
                    "",
                    f"- `section_id`: `{context['section_id']}`",
                    f"- структурные строки шапки: `{context['column_header_row_indexes']}`; "
                    f"основная строка: `{context['header_row_index']}`",
                    f"- строки категории: `{context['category_row_indexes']}`",
                    f"- границы куска: `[{context['section_start_row_index']}, "
                    f"{context['section_end_row_index']})`",
                    f"- строк позиций доступно: `{context['sample_rows_available']}`; "
                    f"передано: `{len(context['sample_rows'])}` из лимита `{context['sample_rows_requested']}`",
                    f"- горизонтальные панели: `{_md(context['horizontal_panels'])}`",
                    "",
                ]
            )
            _render_rows(lines, "Структурная шапка колонок", context["column_header_rows"])
            _render_rows(lines, "Категория и подзаголовок", context["category_rows"])
            _render_rows(lines, "Первые строки позиций", context["sample_rows"])
            _render_observations(lines, context["deterministic_observations"])

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "files": len(files),
                "contexts": total_contexts,
                "category_rows": total_categories,
                "sample_rows": total_samples,
                "excluded_sheets": total_excluded_sheets,
                "report": str(args.report),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
