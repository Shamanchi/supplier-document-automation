"""Provider-neutral contract for model-assisted rule generation."""
from __future__ import annotations

import httpx

from app.config import Settings
from app import rule_proposal_service
from app.llm_providers import provider_options, resolve_rule_model_config


def test_yandex_provider_uses_secret_and_full_catalog_model_uri() -> None:
    configured = Settings(
        llm_provider="yandexgpt",
        yandexgpt_api_key="test-key",
        yandexgpt_folder_id="folder-1",
        yandexgpt_rule_model="qwen3.6-35b-a3b/latest",
    )

    assert configured.rule_model_api_key == "test-key"
    assert configured.rule_model_api_base_url == "https://llm.api.cloud.yandex.net/v1"
    assert configured.rule_model == "gpt://folder-1/qwen3.6-35b-a3b/latest"


def test_yandex_provider_defaults_to_fixed_qwen_235b_model() -> None:
    configured = Settings(
        llm_provider="yandexgpt",
        yandexgpt_api_key="test-key",
        yandexgpt_folder_id="folder-1",
    )

    assert configured.rule_model == "gpt://folder-1/qwen3-235b-a22b-fp8/latest"


def test_yandex_request_uses_supported_max_tokens_parameter() -> None:
    body = rule_proposal_service.build_chat_completions_request(
        prompt="map this section",
        model="gpt://folder-1/qwen3.6-35b-a3b/latest",
        provider="yandexgpt",
        max_output_tokens=900,
    )

    assert body["max_tokens"] == 900
    assert "max_completion_tokens" not in body
    assert body["response_format"]["type"] == "json_schema"
    assert body["messages"][0]["role"] == "system"
    assert "определяй роль по полному смыслу заголовка" in body["messages"][0]["content"]
    assert body["messages"][1] == {"role": "user", "content": "map this section"}
    assert "prompt_cache_key" not in body


def test_model_usage_exposes_yandex_cached_prompt_tokens() -> None:
    usage = rule_proposal_service.normalise_model_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 120,
            "total_tokens": 1120,
            "prompt_tokens_details": {"cached_tokens": 760},
        }
    )

    assert usage == {
        "input_tokens": 1000,
        "output_tokens": 120,
        "total_tokens": 1120,
        "cached_input_tokens": 760,
        "billable_input_tokens": 240,
        "cache_hit_ratio": 0.76,
    }


def test_openai_request_keeps_legacy_completion_token_parameter() -> None:
    body = rule_proposal_service.build_chat_completions_request(
        prompt="map this section",
        model="gpt-4o",
        provider="openai",
        max_output_tokens=900,
    )

    assert body["max_completion_tokens"] == 900
    assert "max_tokens" not in body


def test_deepseek_profile_is_allowlisted_and_keeps_secret_server_side() -> None:
    configured = Settings(
        llm_provider="deepseek",
        deepseek_api_key="direct-secret",
        deepseek_rule_models="deepseek-v4-flash,deepseek-v4-pro",
    )

    profile = resolve_rule_model_config(
        provider="deepseek",
        model="deepseek-v4-flash",
        temperature=0.2,
        max_output_tokens=4096,
        thinking="disabled",
        configured=configured,
    )

    assert profile.api_key == "direct-secret"
    assert profile.api_base_url == "https://api.deepseek.com"
    assert profile.model == "deepseek-v4-flash"
    assert profile.persisted_settings()["max_output_tokens"] == 4096
    assert "direct-secret" not in str(provider_options(configured))


def test_deepseek_request_uses_json_object_and_max_tokens() -> None:
    body = rule_proposal_service.build_chat_completions_request(
        prompt="map this section as json",
        model="deepseek-v4-flash",
        provider="deepseek",
        max_output_tokens=4096,
        temperature=0.2,
        top_p=0.8,
        thinking="disabled",
    )

    assert body["max_tokens"] == 4096
    assert body["response_format"] == {"type": "json_object"}
    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0.2
    assert "price-rule-proposal-v1" in body["messages"][0]["content"]
    assert '"required"' in body["messages"][0]["content"]
    assert "tabular-section-mapping-v7" in body["messages"][0]["content"]
    assert body["top_p"] == 0.8
    assert "max_completion_tokens" not in body


def test_deepseek_thinking_omits_ineffective_sampling_settings() -> None:
    body = rule_proposal_service.build_chat_completions_request(
        prompt="map this section as json",
        model="deepseek-v4-pro",
        provider="deepseek",
        max_output_tokens=4096,
        thinking="enabled",
        reasoning_effort="max",
    )

    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "max"
    assert "temperature" not in body
    assert "top_p" not in body


def test_arbitrary_provider_and_model_are_rejected() -> None:
    configured = Settings(deepseek_api_key="test-key")

    for provider, model in (("attacker", "x"), ("deepseek", "unknown-model")):
        try:
            resolve_rule_model_config(
                provider=provider, model=model, configured=configured
            )
        except ValueError:
            pass
        else:  # pragma: no cover - explicit assertion without pytest dependency
            raise AssertionError("provider/model outside the allowlist was accepted")


def test_only_rate_limits_and_server_failures_are_retried() -> None:
    assert rule_proposal_service.is_retryable_model_status(429) is True
    assert rule_proposal_service.is_retryable_model_status(500) is True
    assert rule_proposal_service.is_retryable_model_status(503) is True
    assert rule_proposal_service.is_retryable_model_status(400) is False
    assert rule_proposal_service.is_retryable_model_status(401) is False
    assert rule_proposal_service.is_retryable_model_exception(
        httpx.ConnectError("temporary TLS failure")
    ) is True
    assert rule_proposal_service.is_retryable_model_exception(
        httpx.HTTPStatusError("bad request", request=None, response=None)
    ) is False
