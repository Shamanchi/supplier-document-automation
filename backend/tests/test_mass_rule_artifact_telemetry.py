"""Telemetry accounting must not call every event a retry."""
from scripts.finalize_mass_rule_artifacts import telemetry_totals


def _event(
    operation_id: str,
    *,
    attempts: int,
    retries: int = 0,
    error_type: str | None = None,
    tokens: int = 0,
) -> dict:
    return {
        "operation_id": operation_id,
        "http_attempts": attempts,
        "retry_count": retries,
        "error_type": error_type,
        "duration_ms": 10,
        "usage": {
            "input_tokens": tokens,
            "output_tokens": 0,
            "total_tokens": tokens,
            "cached_input_tokens": 0,
            "billable_input_tokens": tokens,
        },
    }


def test_telemetry_separates_events_http_retry_and_regeneration() -> None:
    summary = telemetry_totals(
        [
            {
                "telemetry": _event("current", attempts=2, retries=1, tokens=100),
                "telemetry_history": [
                    _event("old", attempts=1, tokens=80),
                    _event(
                        "config",
                        attempts=0,
                        error_type="configuration_error",
                    ),
                ],
            },
            # A copied operation id must not be charged twice.
            {"telemetry": _event("current", attempts=2, retries=1, tokens=100)},
        ]
    )

    assert summary["llm_event_records"] == 3
    assert summary["llm_http_requests"] == 2
    assert summary["llm_http_attempts"] == 3
    assert summary["llm_transport_retries"] == 1
    assert summary["llm_preflight_failures"] == 1
    assert summary["llm_historical_regenerations"] == 2
    assert summary["usage"]["total_tokens"] == 180
