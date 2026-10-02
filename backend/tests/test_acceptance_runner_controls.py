"""Cost-safety controls for the checkpointed paid acceptance runner."""
import pytest

from scripts.acceptance_qwen_rule_pipeline import (
    _generation_history_entry,
    _require_paid_work_api_key,
    _should_schedule_model_call,
)


def test_missing_section_is_scheduled_without_retry_flag() -> None:
    assert _should_schedule_model_call(
        cached=None,
        cached_passed=False,
        retry_failed=False,
        max_generations_per_section=2,
    ) == (True, "missing")


def test_cached_failure_requires_explicit_retry() -> None:
    cached = {"passed": False, "telemetry": {"operation_id": "op-1"}}

    assert _should_schedule_model_call(
        cached=cached,
        cached_passed=False,
        retry_failed=False,
        max_generations_per_section=2,
    ) == (False, "cached_failed_retry_disabled")


def test_explicit_retry_still_obeys_cross_run_generation_cap() -> None:
    cached = {
        "passed": False,
        "telemetry": {"operation_id": "op-2"},
        "telemetry_history": [{"operation_id": "op-1"}],
    }

    assert _should_schedule_model_call(
        cached=cached,
        cached_passed=False,
        retry_failed=True,
        max_generations_per_section=2,
    ) == (False, "generation_cap_reached")


def test_generation_history_keeps_rejection_reason_without_prompt_content() -> None:
    entry = _generation_history_entry(
        {
            "key": "price::section::prompt-hash",
            "passed": False,
            "error": "section dry-run quality gate failed",
            "proposal": {"decision": "propose", "mapping": {"columns": []}},
            "validation": {"passed": False, "item_count": 0},
            "telemetry": {"operation_id": "op-1", "usage": {"total_tokens": 100}},
        }
    )

    assert entry == {
        "operation_id": "op-1",
        "passed": False,
        "error": "section dry-run quality gate failed",
        "proposal_decision": "propose",
        "validation": {"passed": False, "item_count": 0},
        "request_key": "price::section::prompt-hash",
    }
    assert "mapping" not in str(entry)


def test_paid_work_fails_once_when_provider_key_is_missing() -> None:
    with pytest.raises(RuntimeError, match="has no API key"):
        _require_paid_work_api_key(work_count=77, api_key=None, provider="deepseek")

    _require_paid_work_api_key(work_count=0, api_key=None, provider="deepseek")
