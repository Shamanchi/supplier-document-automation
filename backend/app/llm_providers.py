"""Server-side allowlist and immutable settings for rule-model requests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import Settings, settings


@dataclass(frozen=True)
class RuleModelRequestConfig:
    provider: str
    model: str
    api_key: str | None
    api_base_url: str
    temperature: float
    top_p: float
    max_output_tokens: int
    timeout_seconds: int
    thinking: str
    reasoning_effort: str

    def persisted_settings(self) -> dict[str, Any]:
        """Public, immutable request settings. Never include credentials/URLs."""

        result: dict[str, Any] = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_output_tokens": self.max_output_tokens,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.provider == "deepseek":
            result.update(
                {"thinking": self.thinking, "reasoning_effort": self.reasoning_effort}
            )
        return result


def _models(value: str, fallback: str) -> list[str]:
    result = list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    return result or [fallback]


def rule_generation_stub_enabled(configured: Settings = settings) -> bool:
    """Return True when the deterministic rule-generation stub is forced on.

    This is a dev-only switch: when enabled, rule-model requests return a
    deterministic, schema-conformant stub proposal from local section evidence
    and never reach an external provider. Production deployments keep the flag
    off; inserting a real key restores the unchanged provider workflow.
    """

    return bool(configured.rule_generation_stub_mode)


def provider_options(configured: Settings = settings) -> list[dict[str, Any]]:
    definitions = (
        (
            "yandexgpt",
            "Yandex Cloud",
            configured.yandexgpt_api_key,
            configured.yandexgpt_rule_model,
            _models(configured.yandexgpt_rule_models, configured.yandexgpt_rule_model),
        ),
        (
            "deepseek",
            "DeepSeek API",
            configured.deepseek_api_key,
            configured.deepseek_rule_model,
            _models(configured.deepseek_rule_models, configured.deepseek_rule_model),
        ),
        (
            "openai",
            "OpenAI-compatible",
            configured.openai_api_key,
            configured.openai_rule_model,
            _models(configured.openai_rule_models, configured.openai_rule_model),
        ),
    )
    return [
        {
            "provider": provider,
            "label": label,
            "enabled": bool(api_key),
            "models": models,
            "default_model": default_model if default_model in models else models[0],
            "supports_thinking": provider == "deepseek",
        }
        for provider, label, api_key, default_model, models in definitions
    ]


def resolve_rule_model_config(
    *,
    provider: str | None = None,
    model: str | None = None,
    temperature: float = 0,
    top_p: float = 1,
    max_output_tokens: int | None = None,
    timeout_seconds: int | None = None,
    thinking: str = "disabled",
    reasoning_effort: str = "high",
    configured: Settings = settings,
) -> RuleModelRequestConfig:
    """Resolve a client choice through the server allowlist."""

    provider_name = (provider or configured.llm_provider).strip().casefold()
    options = {option["provider"]: option for option in provider_options(configured)}
    option = options.get(provider_name)
    if option is None:
        raise ValueError(f"LLM-провайдер {provider_name!r} не разрешён")
    selected_model = (model or option["default_model"]).strip()
    allowed_model = selected_model
    if provider_name == "yandexgpt" and selected_model.startswith("gpt://"):
        parts = selected_model.removeprefix("gpt://").split("/", 1)
        allowed_model = parts[1] if len(parts) == 2 else selected_model
    if allowed_model not in option["models"]:
        raise ValueError(
            f"Модель {selected_model!r} не разрешена для провайдера {provider_name}"
        )
    if thinking not in {"enabled", "disabled"}:
        raise ValueError("thinking должен быть enabled или disabled")
    if reasoning_effort not in {"high", "max"}:
        raise ValueError("reasoning_effort должен быть high или max")

    if provider_name == "yandexgpt":
        resolved_model = selected_model
        if not resolved_model.startswith("gpt://") and configured.yandexgpt_folder_id:
            resolved_model = (
                f"gpt://{configured.yandexgpt_folder_id}/{resolved_model.lstrip('/')}"
            )
        api_key = configured.yandexgpt_api_key
        base_url = configured.yandexgpt_api_base_url
    elif provider_name == "deepseek":
        resolved_model = selected_model
        api_key = configured.deepseek_api_key
        base_url = configured.deepseek_api_base_url
    else:
        resolved_model = selected_model
        api_key = configured.openai_api_key
        base_url = configured.openai_api_base_url

    return RuleModelRequestConfig(
        provider=provider_name,
        model=resolved_model,
        api_key=api_key,
        api_base_url=base_url,
        temperature=temperature,
        top_p=top_p,
        max_output_tokens=max_output_tokens or configured.openai_rule_max_output_tokens,
        timeout_seconds=timeout_seconds or configured.openai_rule_timeout_seconds,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
    )


def config_from_attempt(
    *, provider: str, model: str, request_settings: dict[str, Any], configured: Settings = settings
) -> RuleModelRequestConfig:
    """Rehydrate exactly the public settings persisted when a batch started."""

    selected_model = model
    if provider == "yandexgpt" and model.startswith("gpt://"):
        selected_model = model.rsplit("/", 2)[-2] + "/" + model.rsplit("/", 1)[-1]
    return resolve_rule_model_config(
        provider=provider,
        model=selected_model,
        temperature=float(request_settings.get("temperature", 0)),
        top_p=float(request_settings.get("top_p", 1)),
        max_output_tokens=int(request_settings.get("max_output_tokens", configured.openai_rule_max_output_tokens)),
        timeout_seconds=int(request_settings.get("timeout_seconds", configured.openai_rule_timeout_seconds)),
        thinking=str(request_settings.get("thinking", "disabled")),
        reasoning_effort=str(request_settings.get("reasoning_effort", "high")),
        configured=configured,
    )
