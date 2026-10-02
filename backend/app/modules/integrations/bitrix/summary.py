"""Текст комментария в таймлайн сделки.

Собирается из snapshot отчёта, а не из живых таблиц: комментарий описывает
именно тот отчёт, который опубликован, и не должен разъезжаться с ним при
пересчёте проверки.
"""

from __future__ import annotations

from typing import Any

#: Статусы строк, которые считаются расхождением для сводки.
MISMATCH_STATUSES = ("NON_COMPLIANT", "NOT_FOUND_IN_FACT", "REVIEW_REQUIRED")

VERDICT_LABELS = {
    "COMPLIANT": "Соответствует",
    "PARTIAL": "Соответствует частично",
    "NON_COMPLIANT": "Не соответствует",
    "PENDING": "Не подтверждён",
}

#: Сколько расхождений перечислять. Комментарий — сводка, а не отчёт: полный
#: перечень лежит в приложенном файле.
MAX_MISMATCHES = 10


def build_comment(payload: dict[str, Any], *, include_key_mismatches: bool) -> str:
    """Сводка по заявке для комментария таймлайна."""
    batch = payload.get("batch") or {}
    verdict = payload.get("verdict") or {}
    items = payload.get("items") or []

    manager = verdict.get("manager") or "PENDING"
    lines = [
        f"Проверка соответствия: {batch.get('title') or 'заявка без названия'}",
        f"Вердикт менеджера: {VERDICT_LABELS.get(manager, manager)}",
        f"Позиций: {batch.get('itemsTotal') or len(items)}",
    ]

    mismatches = _mismatches(items)
    lines.append(f"Расхождений: {len(mismatches)}")
    if include_key_mismatches and mismatches:
        lines.append("Ключевые расхождения:")
        lines.extend(f"— {line}" for line in mismatches[:MAX_MISMATCHES])
        if len(mismatches) > MAX_MISMATCHES:
            lines.append(f"…и ещё {len(mismatches) - MAX_MISMATCHES}")
    if comment := verdict.get("comment"):
        lines.append(f"Комментарий менеджера: {comment}")
    return "\n".join(lines)


def _mismatches(items: list[dict[str, Any]]) -> list[str]:
    """Расхождения в порядке позиций: сначала обязательные, потом остальные."""
    found: list[tuple[int, str]] = []
    for item in items:
        position = item.get("positionNo") or 0
        for row in item.get("rows") or []:
            if row.get("status") not in MISMATCH_STATUSES:
                continue
            weight = 0 if row.get("mandatory") else 1
            found.append((weight, _describe(position, item, row)))
    found.sort(key=lambda pair: pair[0])
    return [text for _weight, text in found]


def _describe(position: int, item: dict[str, Any], row: dict[str, Any]) -> str:
    parameter = row.get("parameter") or "параметр без названия"
    required = _value(row.get("requirementValue"), row.get("requirementUnit"))
    fact = _value(row.get("factValue"), row.get("factUnit"))
    name = item.get("name") or f"позиция {position}"
    if row.get("status") == "NOT_FOUND_IN_FACT":
        return f"{name}: {parameter} — не найдено у поставщика (требуется {required})"
    return f"{name}: {parameter} — требуется {required}, заявлено {fact}"


def _value(value: Any, unit: Any) -> str:
    if value in (None, ""):
        return "—"
    return f"{value} {unit}".strip() if unit else str(value)
