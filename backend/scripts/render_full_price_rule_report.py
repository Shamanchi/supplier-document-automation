#!/usr/bin/env python3
"""Render a reproducible Russian report from acceptance and row-audit JSON."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _n(value: Any) -> str:
    return f"{int(value or 0):,}".replace(",", " ")


def _cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def render(acceptance: dict[str, Any], audit: dict[str, Any]) -> str:
    acceptance_by_price = {row["price"]: row for row in acceptance.get("prices", [])}
    audit_by_price = {row["price"]: row for row in audit.get("prices", [])}
    prices = sorted(set(acceptance_by_price) | set(audit_by_price))
    totals = acceptance.get("totals", {})
    audit_totals = audit.get("totals", {})
    usage = totals.get("usage", {})
    lines = [
        "# Полный отчёт генерации правил для прайсов вне тестовой десятки",
        "",
        "## Результат",
        "",
        f"- файлов выбрано: **{_n(totals.get('prices_selected', len(prices)))}**;",
        f"- файлов загружено: **{_n(totals.get('prices_loaded'))}**;",
        f"- секций extractor: **{_n(totals.get('sections'))}**;",
        f"- записей telemetry (базовые + regeneration + локальные ошибки): **{_n(totals.get('llm_event_records', totals.get('llm_operations')))}**;",
        f"- фактических запросов к LLM API: **{_n(totals.get('llm_http_requests', totals.get('llm_operations')))}**; HTTP attempts: **{_n(totals.get('llm_http_attempts', totals.get('llm_operations')))}**;",
        f"- транспортных HTTP retry: **{_n(totals.get('llm_transport_retries'))}**; исторических regeneration: **{_n(totals.get('llm_historical_regenerations'))}**; локальных preflight failures без HTTP: **{_n(totals.get('llm_preflight_failures'))}**;",
        f"- успешно скомпилировано и проверено: **{_n(totals.get('passed'))}**;",
        f"- отклонено compiler/dry-run: **{_n(totals.get('failed'))}**;",
        f"- не отправлено: **{_n(totals.get('pending'))}**;",
        f"- итоговых позиций: **{_n(audit_totals.get('parsed_items'))}**;",
        f"- непустых физических строк: **{_n(audit_totals.get('nonempty_physical_rows'))}**;",
        f"- уникальных физических item-строк: **{_n(audit_totals.get('unique_item_rows'))}**; строк с дополнительными позициями из панелей/матриц: **{_n(audit_totals.get('multi_item_physical_rows'))}**;",
        f"- уникальных строк заголовков: **{_n(audit_totals.get('header_rows'))}**;",
        f"- строк секций, оставшихся pending из-за безопасного отклонения предложения: **{_n(audit_totals.get('pending_model_rows'))}**;",
        f"- неклассифицированный price-like остаток: **{_n(audit_totals.get('unclassified_price_like_rows'))}** строк;",
        f"- input tokens: **{_n(usage.get('input_tokens'))}**, output: **{_n(usage.get('output_tokens'))}**, total: **{_n(usage.get('total_tokens') or int(usage.get('input_tokens') or 0) + int(usage.get('output_tokens') or 0))}**;",
        f"- cached input: **{_n(usage.get('cached_input_tokens'))}**, billable input: **{_n(usage.get('billable_input_tokens'))}**;",
        f"- суммарная request latency с повторами: **{_n(totals.get('duration_ms'))} мс**.",
        "",
        "`ParsedItem` и физическая строка не взаимозаменяемы: одна строка с несколькими горизонтальными панелями может дать несколько позиций. Поэтому остаток ниже рассчитан по координатам лист+строка, а не вычитанием числа позиций.",
        "",
        "## По каждому файлу",
        "",
        "| Прайс | Непустых строк | Заголовков | Item-строк | Позиции | Каталог без цены | Pending-строк | Price-like остаток | Секций | Прошло | Ошибки | Batch dry-run | Вердикт |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    attention: list[tuple[str, str]] = []
    for price in prices:
        accepted = acceptance_by_price.get(price, {})
        checked = audit_by_price.get(price, {})
        reasons: list[str] = []
        if checked.get("source_error") or accepted.get("source_error"):
            reasons.append("ошибка загрузки")
        if int(accepted.get("failed") or 0):
            reasons.append("есть отклонённые секции")
        if int(checked.get("pending_model_rows") or 0):
            reasons.append(f"pending item-строк: {checked['pending_model_rows']}")
        if int(checked.get("unclassified_price_like_rows") or 0):
            reasons.append(
                f"price-like остаток: {checked['unclassified_price_like_rows']}"
            )
        if reasons:
            verdict = "требует проверки/повтора"
        elif not int(accepted.get("sections") or 0):
            verdict = "ценовые секции не найдены"
        else:
            verdict = "пройден"
        if reasons:
            attention.append((price, "; ".join(reasons)))
        lines.append(
            "| `{}` | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                _cell(price),
                _n(checked.get("nonempty_physical_rows")),
                _n(checked.get("header_rows")),
                _n(checked.get("parsed_item_rows", checked.get("unique_item_rows"))),
                _n(accepted.get("item_count", checked.get("parsed_items"))),
                _n(checked.get("catalog_without_price_rows")),
                _n(checked.get("pending_model_rows")),
                _n(checked.get("unclassified_price_like_rows")),
                _n(accepted.get("sections")),
                _n(accepted.get("passed")),
                _n(accepted.get("failed")),
                ("да" if accepted.get("batch_dry_run_passed") else "нет") if int(accepted.get("sections") or 0) else "н/п",
                verdict,
            )
        )

    lines.extend(["", "## Файлы, требующие внимания", ""])
    if attention:
        lines.extend(f"- `{_cell(price)}` — {reason}." for price, reason in attention)
    else:
        lines.append("После повторной генерации и построчной сверки отклонений не осталось.")

    failures = Counter(
        str(row.get("error") or "неизвестная ошибка")
        for row in acceptance.get("requests", [])
        if not row.get("passed")
    )
    lines.extend(["", "## Отклонения модели/compiler", ""])
    if failures:
        lines.extend(f"- **{_n(count)}** — {_cell(reason)}." for reason, count in failures.most_common())
    else:
        lines.append("Все полученные предложения прошли Pydantic schema, source compiler и dry-run.")

    lines.extend(
        [
            "",
            "## Разбор остатка",
            "",
            "`Price-like` — консервативный сигнал наличия числа или ценоподобного текста, а не доказательство потерянной цены. Крупнейшие проверенные ложные срабатывания: справочник компаний/контактов и почтовые индексы в `xlsx/032_Russcat.xlsx`, нумерованные наименования без цен в `xlsx/014_NFCom.xlsx`, оглавление со ссылками вида `стр.2` в `xls/049_ОПС_КД.xls`. Реальные строки с `по заявке` и составными ценами через `/` или `\\` исправлены универсально и повторно разобраны.",
            "",
            f"Остаток не записывается в БД молча: {_n(totals.get('failed'))} ответов модели, не прошедших Pydantic/source compiler/dry-run, сохранены как безопасно отклонённые; связанные строки остаются pending для последующего исправления и повторной генерации.",
            "",
            "## Метод сверки строк",
            "",
            "Для каждого файла production-loader перечисляет непустые координаты `(лист, Excel-строка)`. Затем точные принятые правила повторно компилируются и запускаются без записи в БД. Отдельно учитываются item-строки, уникальные строки заголовков, подтверждённый каталог без цены, непрайсовые секции, parser issues и остаток для ручной проверки. Горизонтальные панели разворачиваются независимо, поэтому одна физическая строка может законно создать несколько позиций.",
            "",
            "Полные предложения, координаты строк, compiler validation, usage и structured telemetry сохранены в локальных JSON-артефактах; в пользовательский отчёт секреты и содержимое ключей не включаются.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--acceptance", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    acceptance = json.loads(args.acceptance.read_text(encoding="utf-8"))
    audit = json.loads(args.audit.read_text(encoding="utf-8"))
    args.output.write_text(render(acceptance, audit), encoding="utf-8")


if __name__ == "__main__":
    main()
