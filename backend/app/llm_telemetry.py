"""Shared structured and Phoenix telemetry for LLM requests.

Events are persisted with the rule-generation attempt and emitted as one JSON
log line for OpenObserve.  That event remains content-free.  When the explicit
Phoenix flag is enabled, the same event is also converted to an OpenInference
span and enriched with the request/response payload for deep diagnostics.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


logger = logging.getLogger("llm.telemetry")
_jsonl_lock = threading.Lock()
_jsonl_path: Path | None = None


def configure_llm_telemetry_jsonl(path: str | Path | None) -> Path | None:
    """Configure a durable content-free JSONL sink for subsequent requests.

    The logger and Phoenix exporter are delivery channels, not durable storage:
    a CLI may have no INFO handler and an OTLP collector may be unavailable.
    Acceptance jobs therefore configure this sink before their first paid call.
    """

    global _jsonl_path
    with _jsonl_lock:
        _jsonl_path = Path(path) if path is not None else None
        if _jsonl_path is not None:
            _jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    return _jsonl_path


def append_llm_request_event_jsonl(
    event: dict[str, Any], *, path: str | Path | None = None
) -> None:
    """Append one event atomically enough for concurrent in-process workers."""

    target = Path(path) if path is not None else _jsonl_path
    if target is None:
        return
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)
    with _jsonl_lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()


def normalise_model_usage(raw_usage: object) -> dict[str, int | float]:
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}

    def counter(name: str) -> int:
        value = usage.get(name)
        return value if isinstance(value, int) and value >= 0 else 0

    input_tokens = counter("prompt_tokens")
    output_tokens = counter("completion_tokens")
    total_tokens = counter("total_tokens") or input_tokens + output_tokens
    cached = details.get("cached_tokens")
    if cached is None:
        # DeepSeek exposes automatic context-cache accounting at the top
        # level, unlike OpenAI/Yandex's prompt_tokens_details shape.
        cached = usage.get("prompt_cache_hit_tokens")
    cached_tokens = cached if isinstance(cached, int) and 0 <= cached <= input_tokens else 0
    result: dict[str, int | float] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_input_tokens": cached_tokens,
        "billable_input_tokens": input_tokens - cached_tokens,
        "cache_hit_ratio": round(cached_tokens / input_tokens, 6) if input_tokens else 0.0,
    }
    return result


def aggregate_model_usage(usages: list[dict[str, int | float]]) -> dict[str, int | float]:
    """Aggregate initial + semantic-repair calls without spreading maths."""

    integer_keys = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "billable_input_tokens",
    )
    result: dict[str, int | float] = {
        key: sum(
            int(usage.get(key, 0))
            for usage in usages
            if isinstance(usage.get(key, 0), (int, float))
        )
        for key in integer_keys
    }
    input_tokens = int(result["input_tokens"])
    result["cache_hit_ratio"] = (
        round(int(result["cached_input_tokens"]) / input_tokens, 6)
        if input_tokens
        else 0.0
    )
    return result


def build_llm_request_event(
    *,
    operation_id: str,
    provider: str,
    model: str,
    started_at: datetime,
    finished_at: datetime,
    http_attempts: int,
    http_status_code: int | None,
    finish_reason: str | None,
    response_id: str | None,
    usage: dict[str, int | float],
    dimensions: dict[str, str] | None = None,
    request_settings: dict[str, Any] | None = None,
    raw_usage: dict[str, Any] | None = None,
    http_attempt_details: list[dict[str, Any]] | None = None,
    error_type: str | None = None,
) -> dict[str, Any]:
    duration_ms = max(0, round((finished_at - started_at).total_seconds() * 1000))
    output_tokens = usage.get("output_tokens", 0)
    output_tps = (
        round(float(output_tokens) / (duration_ms / 1000), 6)
        if duration_ms and isinstance(output_tokens, (int, float))
        else 0.0
    )
    return {
        "schema_version": "llm_request_v1",
        "event_name": "llm.request.failed" if error_type else "llm.request.completed",
        "operation_id": operation_id,
        "provider": provider,
        "model": model,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_ms": duration_ms,
        "output_tokens_per_second": output_tps,
        "http_attempts": http_attempts,
        "retry_count": max(0, http_attempts - 1),
        "http_status_code": http_status_code,
        "finish_reason": finish_reason,
        "response_id": response_id,
        "success": error_type is None,
        "error_type": error_type,
        "usage": dict(usage),
        "raw_usage": dict(raw_usage or {}),
        "http_attempt_details": list(http_attempt_details or []),
        "request_settings": dict(request_settings or {}),
        "dimensions": dict(dimensions or {}),
    }


def emit_llm_request_event(
    event: dict[str, Any],
    *,
    request_body: dict[str, Any] | None = None,
    response_body: object = None,
) -> None:
    """Emit the common event and, when enabled, its rich Phoenix span.

    The import is intentionally lazy so a disabled deployment pays no SDK
    startup, thread, batching, or network cost.
    """

    logger.info(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
    append_llm_request_event_jsonl(event)
    from app.phoenix_telemetry import emit_phoenix_llm_span

    emit_phoenix_llm_span(
        event,
        request_body=request_body,
        response_body=response_body,
    )
