"""Opt-in OpenInference spans exported to an Arize Phoenix collector."""
from __future__ import annotations

import atexit
import json
import logging
import threading
from datetime import datetime
from typing import Any

from app.config import settings


logger = logging.getLogger("llm.telemetry.phoenix")
_lock = threading.Lock()
_tracer: Any = None
_provider: Any = None
_initialisation_attempted = False


def build_phoenix_export_headers(api_key: str | None) -> dict[str, str]:
    """Return Phoenix Bearer auth without ever logging the credential."""

    token = (api_key or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _json_text(value: object, *, limit: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        text = str(value)
    if limit > 0 and len(text) > limit:
        return text[:limit] + "…"
    return text


def build_phoenix_span_attributes(
    event: dict[str, Any],
    *,
    request_body: dict[str, Any] | None,
    response_body: object,
    content_limit: int,
) -> dict[str, str | int | float | bool]:
    """Map the shared event to OpenInference plus diagnostic attributes."""

    usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
    request_settings = (
        event.get("request_settings")
        if isinstance(event.get("request_settings"), dict)
        else {}
    )
    attributes: dict[str, str | int | float | bool] = {
        "openinference.span.kind": "LLM",
        "llm.provider": str(event.get("provider") or "unknown"),
        "llm.system": str(event.get("provider") or "unknown"),
        "llm.model_name": str(event.get("model") or "unknown"),
        "llm.operation.id": str(event.get("operation_id") or ""),
        "llm.invocation_parameters": _json_text(request_settings, limit=content_limit),
        "llm.latency.duration_ms": int(event.get("duration_ms") or 0),
        "llm.throughput.output_tokens_per_second": float(
            event.get("output_tokens_per_second") or 0
        ),
        "llm.request.http_attempts": int(event.get("http_attempts") or 0),
        "llm.request.retry_count": int(event.get("retry_count") or 0),
        "llm.request.attempt_details": _json_text(
            event.get("http_attempt_details") or [], limit=content_limit
        ),
        "llm.usage.raw": _json_text(event.get("raw_usage") or {}, limit=content_limit),
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": str(event.get("provider") or "unknown"),
        "gen_ai.request.model": str(event.get("model") or "unknown"),
    }
    if request_body is not None:
        attributes["input.value"] = _json_text(request_body, limit=content_limit)
        attributes["input.mime_type"] = "application/json"
    if response_body is not None:
        attributes["output.value"] = _json_text(response_body, limit=content_limit)
        attributes["output.mime_type"] = "application/json"

    counters = {
        "llm.token_count.prompt": "input_tokens",
        "llm.token_count.completion": "output_tokens",
        "llm.token_count.total": "total_tokens",
        "llm.token_count.prompt_details.cache_read": "cached_input_tokens",
        "llm.token_count.prompt_details.billable": "billable_input_tokens",
        "gen_ai.usage.input_tokens": "input_tokens",
        "gen_ai.usage.output_tokens": "output_tokens",
    }
    for attribute, usage_key in counters.items():
        value = usage.get(usage_key)
        if isinstance(value, int) and value >= 0:
            attributes[attribute] = value
    cache_hit_ratio = usage.get("cache_hit_ratio")
    if isinstance(cache_hit_ratio, (int, float)):
        attributes["llm.cache.hit_ratio"] = float(cache_hit_ratio)

    scalar_fields = {
        "http.response.status_code": "http_status_code",
        "llm.response.id": "response_id",
        "llm.response.finish_reason": "finish_reason",
        "error.type": "error_type",
    }
    for attribute, event_key in scalar_fields.items():
        value = event.get(event_key)
        if isinstance(value, (str, int, float, bool)):
            attributes[attribute] = value
    for key, value in (event.get("dimensions") or {}).items():
        if isinstance(value, (str, int, float, bool)):
            attributes[f"llm.dimension.{key}"] = value
    return attributes


def _get_tracer() -> Any:
    global _initialisation_attempted, _provider, _tracer
    if not settings.phoenix_enabled:
        return None
    if _initialisation_attempted:
        return _tracer
    with _lock:
        if _initialisation_attempted:
            return _tracer
        _initialisation_attempted = True
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            resource = Resource.create(
                {
                    "service.name": "price-loader",
                    "openinference.project.name": settings.phoenix_project_name,
                }
            )
            provider = TracerProvider(resource=resource)
            provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=settings.phoenix_collector_endpoint,
                        headers=build_phoenix_export_headers(settings.phoenix_api_key),
                    )
                )
            )
            _provider = provider
            _tracer = provider.get_tracer("price-loader.llm")
            logger.info(
                "Phoenix telemetry enabled: endpoint=%s project=%s",
                settings.phoenix_collector_endpoint,
                settings.phoenix_project_name,
            )
        except ModuleNotFoundError as exc:
            logger.error(
                "Phoenix telemetry disabled: missing Python package %s; "
                "install backend requirements",
                exc.name,
            )
        except Exception:  # noqa: BLE001 - observability must not break model calls
            logger.exception("Phoenix telemetry initialisation failed; continuing without it")
        return _tracer


def _unix_nanos(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp() * 1_000_000_000)
    except ValueError:
        return None


def emit_phoenix_llm_span(
    event: dict[str, Any],
    *,
    request_body: dict[str, Any] | None,
    response_body: object,
) -> None:
    tracer = _get_tracer()
    if tracer is None:
        return
    try:
        from opentelemetry.trace import Status, StatusCode

        span = tracer.start_span(
            f"{event.get('provider', 'llm')} {event.get('model', 'request')}",
            start_time=_unix_nanos(event.get("started_at")),
        )
        span.set_attributes(
            build_phoenix_span_attributes(
                event,
                request_body=request_body,
                response_body=response_body,
                content_limit=settings.phoenix_max_content_chars,
            )
        )
        if event.get("success"):
            span.set_status(Status(StatusCode.OK))
        else:
            span.set_status(
                Status(StatusCode.ERROR, str(event.get("error_type") or "LLM request failed"))
            )
        span.end(end_time=_unix_nanos(event.get("finished_at")))
    except Exception:  # noqa: BLE001 - exporter errors are never application errors
        logger.exception("Failed to build Phoenix LLM span")


def shutdown_phoenix_telemetry() -> None:
    provider = _provider
    if provider is None:
        return
    try:
        provider.force_flush(timeout_millis=5_000)
        provider.shutdown()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to flush Phoenix telemetry")


atexit.register(shutdown_phoenix_telemetry)
