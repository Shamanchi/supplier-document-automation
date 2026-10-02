"""Import-level aggregation over persisted LLM operation telemetry."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RuleGenerationAttempt
from app.llm_telemetry import aggregate_model_usage


ZERO_USAGE = {
    "operations": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0,
    "cached_input_tokens": 0,
    "billable_input_tokens": 0,
    "http_retries": 0,
    "semantic_retries": 0,
    "llm_latency_ms": 0,
}


async def aggregate_import_telemetry(
    session: AsyncSession, price_import_id: uuid.UUID
) -> dict[str, int | float]:
    attempts = (
        await session.execute(
            select(RuleGenerationAttempt).where(
                RuleGenerationAttempt.price_import_id == price_import_id
            )
        )
    ).scalars().all()
    result: dict[str, int | float] = dict(ZERO_USAGE)
    for attempt in attempts:
        validation = attempt.validation if isinstance(attempt.validation, dict) else {}
        usage = validation.get("model_usage")
        events = validation.get("llm_telemetry_events")
        if not isinstance(events, list):
            event = validation.get("llm_telemetry")
            events = [event] if isinstance(event, dict) else []
        if not isinstance(usage, dict):
            usage = aggregate_model_usage(
                [
                    event["usage"]
                    for event in events
                    if isinstance(event, dict) and isinstance(event.get("usage"), dict)
                ]
            )
        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
            "billable_input_tokens",
        ):
            value = usage.get(key, 0)
            if isinstance(value, (int, float)):
                result[key] = int(result[key]) + int(value)
        result["operations"] = int(result["operations"]) + len(events)
        for event in events:
            if not isinstance(event, dict):
                continue
            result["http_retries"] = int(result["http_retries"]) + int(
                event.get("retry_count", 0) or 0
            )
            result["llm_latency_ms"] = int(result["llm_latency_ms"]) + int(
                event.get("duration_ms", 0) or 0
            )
        result["semantic_retries"] = int(result["semantic_retries"]) + int(
            validation.get("semantic_retries", 0) or 0
        )
    input_tokens = int(result["input_tokens"])
    result["cache_hit_ratio"] = (
        round(int(result["cached_input_tokens"]) / input_tokens, 6)
        if input_tokens
        else 0.0
    )
    return result


def telemetry_dimensions(price_import_id: uuid.UUID, **values: object) -> dict[str, str]:
    result = {"price_import_id": str(price_import_id)}
    result.update({key: str(value) for key, value in values.items() if value is not None})
    return result
