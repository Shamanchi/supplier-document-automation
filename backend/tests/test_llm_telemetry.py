"""Shared structured and Phoenix LLM telemetry."""
from __future__ import annotations

from datetime import datetime, timezone

from app.config import Settings
from app.llm_telemetry import (
    append_llm_request_event_jsonl,
    build_llm_request_event,
    configure_llm_telemetry_jsonl,
    normalise_model_usage,
)
from app.phoenix_telemetry import (
    build_phoenix_export_headers,
    build_phoenix_span_attributes,
)


def test_usage_normalisation_exposes_billable_and_cached_input_tokens() -> None:
    usage = normalise_model_usage(
        {
            "prompt_tokens": 2440,
            "completion_tokens": 1073,
            "total_tokens": 3513,
            "prompt_tokens_details": {"cached_tokens": 528},
        }
    )

    assert usage == {
        "input_tokens": 2440,
        "output_tokens": 1073,
        "total_tokens": 3513,
        "cached_input_tokens": 528,
        "billable_input_tokens": 1912,
        "cache_hit_ratio": 0.216393,
    }


def test_usage_normalisation_supports_deepseek_cache_counters() -> None:
    usage = normalise_model_usage(
        {
            "prompt_tokens": 3200,
            "completion_tokens": 400,
            "total_tokens": 3600,
            "prompt_cache_hit_tokens": 2500,
            "prompt_cache_miss_tokens": 700,
        }
    )

    assert usage["cached_input_tokens"] == 2500
    assert usage["billable_input_tokens"] == 700
    assert usage["cache_hit_ratio"] == 0.78125


def test_request_event_has_operation_dimensions_latency_retries_and_no_content() -> None:
    event = build_llm_request_event(
        operation_id="op-1",
        provider="yandexgpt",
        model="qwen3-235b-a22b-fp8/latest",
        started_at=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 7, 15, 12, 0, 2, 345000, tzinfo=timezone.utc),
        http_attempts=2,
        http_status_code=200,
        finish_reason="stop",
        response_id="response-1",
        usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        dimensions={"batch_id": "batch-1", "section_id": "sec-1"},
    )

    assert event["schema_version"] == "llm_request_v1"
    assert event["event_name"] == "llm.request.completed"
    assert event["duration_ms"] == 2345
    assert event["http_attempts"] == 2
    assert event["retry_count"] == 1
    assert event["output_tokens_per_second"] == 8.528785
    assert event["dimensions"] == {"batch_id": "batch-1", "section_id": "sec-1"}
    assert "prompt" not in str(event).lower()
    assert "content" not in str(event).lower()


def test_phoenix_attributes_include_content_cache_tokens_and_diagnostics() -> None:
    event = build_llm_request_event(
        operation_id="op-2",
        provider="deepseek",
        model="deepseek-v4-flash",
        started_at=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 7, 15, 12, 0, 2, tzinfo=timezone.utc),
        http_attempts=2,
        http_status_code=200,
        finish_reason="stop",
        response_id="response-2",
        usage={
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cached_input_tokens": 80,
            "billable_input_tokens": 20,
            "cache_hit_ratio": 0.8,
        },
        raw_usage={"prompt_cache_hit_tokens": 80},
        http_attempt_details=[{"attempt": 1, "http_status_code": 503}],
        dimensions={"section_id": "sheet-1"},
        request_settings={"temperature": 0},
    )

    attributes = build_phoenix_span_attributes(
        event,
        request_body={"messages": [{"role": "user", "content": "request text"}]},
        response_body={"choices": [{"message": {"content": "response text"}}]},
        content_limit=10_000,
    )

    assert attributes["openinference.span.kind"] == "LLM"
    assert attributes["llm.provider"] == "deepseek"
    assert attributes["llm.token_count.prompt"] == 100
    assert attributes["llm.token_count.completion"] == 20
    assert attributes["llm.token_count.prompt_details.cache_read"] == 80
    assert attributes["llm.cache.hit_ratio"] == 0.8
    assert attributes["llm.throughput.output_tokens_per_second"] == 10.0
    assert attributes["llm.dimension.section_id"] == "sheet-1"
    assert "request text" in attributes["input.value"]
    assert "response text" in attributes["output.value"]
    assert "prompt_cache_hit_tokens" in attributes["llm.usage.raw"]


def test_phoenix_content_is_bounded() -> None:
    event = build_llm_request_event(
        operation_id="op-3",
        provider="openai",
        model="gpt-4o",
        started_at=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 7, 15, 12, 0, 1, tzinfo=timezone.utc),
        http_attempts=1,
        http_status_code=200,
        finish_reason="stop",
        response_id="response-3",
        usage={},
    )

    attributes = build_phoenix_span_attributes(
        event,
        request_body={"prompt": "x" * 100},
        response_body=None,
        content_limit=20,
    )

    assert attributes["input.value"].endswith("…")
    assert len(attributes["input.value"]) == 21


def test_phoenix_exporter_uses_bearer_auth_when_configured() -> None:
    assert build_phoenix_export_headers("admin-secret") == {
        "Authorization": "Bearer admin-secret"
    }
    assert build_phoenix_export_headers(None) == {}


def test_phoenix_is_enabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("PHOENIX_ENABLED", raising=False)

    assert Settings().phoenix_enabled is True


def test_jsonl_sink_persists_content_free_events(tmp_path) -> None:
    path = tmp_path / "telemetry" / "llm.jsonl"
    event = {"schema_version": "llm_request_v1", "operation_id": "op-jsonl"}
    configure_llm_telemetry_jsonl(path)
    try:
        append_llm_request_event_jsonl(event)
    finally:
        configure_llm_telemetry_jsonl(None)

    assert path.read_text(encoding="utf-8") == (
        '{"schema_version":"llm_request_v1","operation_id":"op-jsonl"}\n'
    )
