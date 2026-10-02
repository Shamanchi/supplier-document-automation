"""Pure selection helpers for importing an offline retry set into review."""

from __future__ import annotations

from typing import Any


def select_retry_entries(
    checkpoint: dict[str, Any],
    telemetry: list[dict[str, Any]],
    *,
    operation_count: int,
    excluded_section_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    if operation_count < 1:
        raise ValueError("operation_count должен быть положительным")
    excluded = excluded_section_ids or set()
    keys: list[tuple[str, str]] = []
    for event in telemetry[-operation_count:]:
        dimensions = event.get("dimensions") or {}
        price = str(dimensions.get("price") or "")
        section_id = str(dimensions.get("section_id") or "")
        if price and section_id and section_id not in excluded:
            keys.append((price, section_id))
    if len(keys) != len(set(keys)):
        raise ValueError("Последний retry window содержит дубли section identity")
    request_by_key = {
        (str(row.get("price") or ""), str(row.get("section_id") or "")): row
        for row in checkpoint.get("requests") or []
    }
    missing = [key for key in keys if key not in request_by_key]
    if missing:
        raise ValueError(f"В checkpoint отсутствуют retry sections: {missing[:3]}")
    return [request_by_key[key] for key in keys]
