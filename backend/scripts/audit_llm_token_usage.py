#!/usr/bin/env python3
"""Audit persisted LLM operations without double-counting copied checkpoints."""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "billable_input_tokens",
)
CURRENT_FLASH_PRICES_USD_PER_MILLION = {
    "cached_input_tokens": 0.0028,
    "billable_input_tokens": 0.14,
    "output_tokens": 0.28,
}


def _events(payload: dict[str, Any]) -> Iterable[tuple[dict[str, Any], str, str]]:
    for request in payload.get("requests", []):
        if not isinstance(request, dict):
            continue
        price = str(request.get("price") or "unknown")
        for event in request.get("telemetry_history") or []:
            if isinstance(event, dict):
                yield event, price, "history"
        event = request.get("telemetry")
        if isinstance(event, dict):
            yield event, price, "current"


def _usage_sum(events: Iterable[dict[str, Any]]) -> dict[str, int]:
    result = Counter()
    for event in events:
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        for key in USAGE_KEYS:
            value = usage.get(key)
            if isinstance(value, int):
                result[key] += value
    return {key: result[key] for key in USAGE_KEYS}


def _cost_estimate(usage: dict[str, int]) -> dict[str, float]:
    parts = {
        key: round(usage.get(key, 0) / 1_000_000 * rate, 6)
        for key, rate in CURRENT_FLASH_PRICES_USD_PER_MILLION.items()
    }
    parts["total"] = round(sum(parts.values()), 6)
    return parts


def _number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def audit(report_dir: Path, current_path: Path) -> dict[str, Any]:
    paths = sorted(report_dir.glob("acceptance_deepseek*.json"))
    unique: dict[str, tuple[dict[str, Any], str, str]] = {}
    artifact_rows: list[dict[str, Any]] = []
    reference_count = 0
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        before = len(unique)
        references = 0
        for event, price, position in _events(payload):
            references += 1
            reference_count += 1
            operation_id = str(event.get("operation_id") or "")
            if operation_id:
                unique.setdefault(operation_id, (event, price, position))
        artifact_rows.append(
            {
                "artifact": path.name,
                "event_references": references,
                "new_unique_operations": len(unique) - before,
            }
        )

    unique_events = [record[0] for record in unique.values()]
    all_usage = _usage_sum(unique_events)
    all_by_price: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_date: Counter[str] = Counter()
    for event, price, _ in unique.values():
        all_by_price[price].append(event)
        started_at = str(event.get("started_at") or "unknown")
        by_date[started_at[:10]] += 1
    top_prices = []
    for price, events in all_by_price.items():
        usage = _usage_sum(events)
        top_prices.append(
            {"price": price, "operations": len(events), "usage": usage}
        )
    top_prices.sort(key=lambda row: row["usage"]["total_tokens"], reverse=True)

    current = json.loads(current_path.read_text(encoding="utf-8"))
    current_unique: dict[str, tuple[dict[str, Any], str, str]] = {}
    current_refs = 0
    request_operation_counts: list[tuple[dict[str, Any], int, int]] = []
    for request in current.get("requests", []):
        request_events: list[dict[str, Any]] = []
        if isinstance(request, dict):
            request_events.extend(
                event
                for event in (request.get("telemetry_history") or [])
                if isinstance(event, dict)
            )
            if isinstance(request.get("telemetry"), dict):
                request_events.append(request["telemetry"])
        total = _usage_sum(request_events)["total_tokens"]
        request_operation_counts.append((request, len(request_events), total))
        for event in request_events:
            current_refs += 1
            operation_id = str(event.get("operation_id") or "")
            if operation_id:
                current_unique.setdefault(
                    operation_id,
                    (event, str(request.get("price") or "unknown"), "request"),
                )
    current_events = [record[0] for record in current_unique.values()]
    current_usage = _usage_sum(current_events)
    current_operations: dict[str, dict[str, Any]] = {}
    history_operations: dict[str, dict[str, Any]] = {}
    for request in current.get("requests", []):
        if not isinstance(request, dict):
            continue
        event = request.get("telemetry")
        if isinstance(event, dict) and event.get("operation_id"):
            current_operations.setdefault(str(event["operation_id"]), event)
        for event in request.get("telemetry_history") or []:
            if isinstance(event, dict) and event.get("operation_id"):
                history_operations.setdefault(str(event["operation_id"]), event)
    current_generation_usage = _usage_sum(current_operations.values())
    regeneration_usage = _usage_sum(history_operations.values())
    history_distribution = Counter(
        len(request.get("telemetry_history") or [])
        for request in current.get("requests", [])
        if isinstance(request, dict)
    )
    request_operation_counts.sort(key=lambda row: row[2], reverse=True)
    requests_with_repeats = [row for row in request_operation_counts if row[1] > 1]
    adjustment = current.get("telemetry_adjustment") or {}
    adjustment_usage = adjustment.get("usage") if isinstance(adjustment, dict) else {}
    final_usage = {
        key: current_usage.get(key, 0)
        + (adjustment_usage.get(key, 0) if isinstance(adjustment_usage.get(key), int) else 0)
        for key in USAGE_KEYS
    }

    failures = sum(not bool(event.get("success")) for event in unique_events)
    http_attempts = sum(int(event.get("http_attempts") or 0) for event in unique_events)
    http_retries = sum(int(event.get("retry_count") or 0) for event in unique_events)
    latency_ms = sum(int(event.get("duration_ms") or 0) for event in unique_events)
    return {
        "method": {
            "artifact_glob": str(report_dir / "acceptance_deepseek*.json"),
            "dedupe_key": "operation_id",
            "current_checkpoint": str(current_path),
        },
        "all_saved_artifacts": {
            "artifacts": len(paths),
            "event_references": reference_count,
            "unique_operations": len(unique),
            "duplicate_references": reference_count - len(unique),
            "successful_operations": len(unique) - failures,
            "failed_operations": failures,
            "http_attempts": http_attempts,
            "http_retries": http_retries,
            "duration_ms": latency_ms,
            "usage": all_usage,
            "cache_hit_ratio": round(
                all_usage["cached_input_tokens"] / all_usage["input_tokens"], 6
            ) if all_usage["input_tokens"] else 0,
            "estimated_cost_at_current_flash_prices_usd": _cost_estimate(all_usage),
            "operations_by_start_date": dict(sorted(by_date.items())),
            "top_prices": top_prices[:20],
            "artifacts_detail": artifact_rows,
        },
        "current_final_checkpoint": {
            "sections": len(current.get("requests", [])),
            "event_references": current_refs,
            "unique_operations": len(current_unique),
            "usage_before_adjustment": current_usage,
            "telemetry_adjustment": adjustment,
            "usage_with_adjustment": final_usage,
            "estimated_cost_at_current_flash_prices_usd": _cost_estimate(final_usage),
            "requests_with_more_than_one_generation": len(requests_with_repeats),
            "current_unique_operations": len(current_operations),
            "historical_regeneration_operations": len(history_operations),
            "current_generation_usage": current_generation_usage,
            "regeneration_usage": regeneration_usage,
            "history_length_distribution": dict(sorted(history_distribution.items())),
            "largest_repeated_sections": [
                {
                    "price": row[0].get("price"),
                    "section_id": row[0].get("section_id"),
                    "passed": bool(row[0].get("passed")),
                    "operations": row[1],
                    "total_tokens": row[2],
                }
                for row in requests_with_repeats[:20]
            ],
        },
    }


def render_markdown(payload: dict[str, Any], provider_total: int | None) -> str:
    saved = payload["all_saved_artifacts"]
    current = payload["current_final_checkpoint"]
    usage = saved["usage"]
    lines = [
        "# Аудит расхода DeepSeek и телеметрии",
        "",
        "## Короткий вывод",
        "",
        f"В локальных acceptance-артефактах найдено **{_number(saved['event_references'])} ссылок на события**, "
        f"но после дедупликации по `operation_id` это **{_number(saved['unique_operations'])} реальных операций**. "
        f"Повторных ссылок из копий checkpoint-файлов — **{_number(saved['duplicate_references'])}**; они в расход повторно не включены.",
        "",
        f"Сохранённый подтверждённый расход: **{_number(usage['total_tokens'])} токенов**: "
        f"input {_number(usage['input_tokens'])}, output {_number(usage['output_tokens'])}. "
        f"Из input cache hit — {_number(usage['cached_input_tokens'])}, cache miss/billable input — "
        f"{_number(usage['billable_input_tokens'])}.",
        "",
        "В модель отправлялся **не весь Excel-файл**, а один ограниченный контекст секции за запрос: "
        "координаты/заголовки, до 5 реальных строк на логическую таблицу или панель, каталог атрибутов "
        "и универсальные инструкции. Однако системный prompt вместе с JSON Schema занимает около 8,6 тыс. "
        "символов, а типичный user prompt — около 20–27 тыс. символов. Этот фиксированный контекст "
        "повторялся для каждой из тысяч секций.",
        "",
        "## Почему расход вырос",
        "",
        f"В финальном checkpoint **{_number(current['sections'])} секций**. Для "
        f"**{_number(current['requests_with_more_than_one_generation'])} секций** имеется история повторной генерации. "
        f"Уникальных текущих операций — **{_number(current['current_unique_operations'])}**, "
        f"исторических regeneration-операций — **{_number(current['historical_regeneration_operations'])}**. "
        f"Текущие операции израсходовали **{_number(current['current_generation_usage']['total_tokens'])}**, "
        f"предыдущие regeneration — **{_number(current['regeneration_usage']['total_tokens'])} токенов**.",
        "Итого финального checkpoint: "
        f"{_number(current['current_generation_usage']['total_tokens'])} + "
        f"{_number(current['regeneration_usage']['total_tokens'])} + "
        f"{_number(int((current.get('telemetry_adjustment') or {}).get('usage', {}).get('total_tokens', 0)))} "
        f"(утраченное при миграции контекста событие) = **{_number(current['usage_with_adjustment']['total_tokens'])} токенов**.",
        "",
        f"Транспортных HTTP-retry в сохранённых логах всего **{saved['http_retries']}**. Основной перерасход — "
        "не сетевые retry, а повторные model repair/regeneration при повторных запусках acceptance: прежний "
        "runner автоматически снова ставил cached failure в платную очередь.",
        "",
        "## Что исправлено",
        "",
        "- каждая LLM-операция теперь немедленно дописывается в отдельный content-free `*.telemetry.jsonl`; "
        "событие сохраняется независимо от Phoenix, stdout и последующей перезаписи checkpoint;",
        "- восстановлены отсутствовавшие в локальном `.venv` зависимости OpenTelemetry;",
        "- повтор failed-секции теперь запрещён по умолчанию: требуется явный `--retry-failed`;",
        "- добавлен межзапусковый `--max-generations-per-section` (по умолчанию 2), который считает также `telemetry_history`;",
        "- аудит дедуплицирует события строго по `operation_id`, поэтому копии checkpoint не считаются расходом.",
        "",
        "## Метрики сохранённых операций",
        "",
        f"- операций: **{_number(saved['unique_operations'])}**; успешных: **{_number(saved['successful_operations'])}**; "
        f"неуспешных: **{_number(saved['failed_operations'])}**;",
        f"- cache hit ratio по input: **{saved['cache_hit_ratio']:.2%}**;",
        f"- суммарная request latency: **{_number(saved['duration_ms'])} мс**;",
        f"- оценка по текущим тарифам Flash: **${saved['estimated_cost_at_current_flash_prices_usd']['total']:.2f}** "
        "(это ориентир, не бухгалтерская сверка исторического тарифа).",
        "",
        "## Самые дорогие прайсы в сохранённых логах",
        "",
        "| Прайс | Операций | Total tokens |",
        "|---|---:|---:|",
    ]
    for row in saved["top_prices"][:15]:
        lines.append(
            f"| `{row['price']}` | {_number(row['operations'])} | {_number(row['usage']['total_tokens'])} |"
        )
    lines.extend(
        [
            "",
            "## Секции с наибольшим числом повторов",
            "",
            "| Прайс | Section | Генераций | Tokens | Прошла |",
            "|---|---|---:|---:|---|",
        ]
    )
    for row in current["largest_repeated_sections"][:15]:
        lines.append(
            f"| `{row['price']}` | `{row['section_id']}` | {row['operations']} | "
            f"{_number(row['total_tokens'])} | {'да' if row['passed'] else 'нет'} |"
        )
    if provider_total is not None:
        gap = provider_total - usage["total_tokens"]
        lines.extend(
            [
                "",
                "## Сверка с кабинетом DeepSeek",
                "",
                f"Ориентир из комментария: **≈{_number(provider_total)}**, локально доказано: "
                f"**{_number(usage['total_tokens'])}**, ориентировочный несверенный остаток: "
                f"**≈{_number(gap)} токенов**.",
                "",
                "Этот остаток нельзя честно распределить по файлам по текущим локальным данным. Вероятные источники: "
                "прерванные процессы до записи checkpoint, операции из перезаписанных/удалённых промежуточных "
                "артефактов и ответы, по которым клиент не успел сохранить provider usage. Для точной финансовой "
                "сверки нужен CSV Export из DeepSeek Usage; его строки следует соединить с JSONL по времени/API key, "
                "а после внедрённого JSONL новые операции уже не будут теряться между checkpoint-файлами.",
            ]
        )
    lines.extend(
        [
            "",
            "## Методика",
            "",
            "Просканированы все локальные `acceptance_deepseek*.json`. Взяты текущие `telemetry` и "
            "`telemetry_history`; копии одного события в разных артефактах исключены по `operation_id`. "
            "Содержимое prompt/response не читается и в structured JSONL не записывается.",
            "",
            "Источники семантики токенов и тарифов: "
            "[DeepSeek Context Caching](https://api-docs.deepseek.com/guides/kv_cache), "
            "[Chat Completion usage](https://api-docs.deepseek.com/api/create-chat-completion/), "
            "[Usage CSV Export](https://api-docs.deepseek.com/faq), "
            "[Flash pricing](https://api-docs.deepseek.com/quick_start/pricing?article_id=article_1779470751466_8).",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-md", type=Path, required=True)
    parser.add_argument(
        "--provider-total",
        type=int,
        default=None,
        help="Optional dashboard total for an explicit local-vs-provider gap.",
    )
    args = parser.parse_args()
    payload = audit(args.report_dir, args.current)
    args.output_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.output_md.write_text(
        render_markdown(payload, args.provider_total), encoding="utf-8"
    )
    print(json.dumps(payload["all_saved_artifacts"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
