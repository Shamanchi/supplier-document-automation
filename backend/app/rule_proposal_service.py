"""Application service for model section proposals and draft-rule creation.

Network access lives here, not in the parser.  The parser remains completely
deterministic: a model can only propose a mapping; local source validation and
a full dry-run decide whether a human may create a draft rule.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import rules_service, storage
from app.config import settings
from app.durable_rule_generation import (
    DurableOperationFailed,
    DurableOperationInProgress,
    OPERATION_INITIAL,
    OPERATION_SEMANTIC_REPAIR,
    WorkIdentity,
    attach_exact_artifact,
    build_work_identity,
    complete_operation,
    create_and_attach_verified_artifact,
    fail_operation,
    redact_sensitive_text,
    reserve_operation,
)
from app.llm_telemetry import (
    aggregate_model_usage,
    build_llm_request_event,
    emit_llm_request_event,
    normalise_model_usage,
)
from app.llm_providers import (
    RuleModelRequestConfig,
    config_from_attempt,
    resolve_rule_model_config,
    rule_generation_stub_enabled,
)
from app.models import (
    ParseRule,
    PriceFile,
    PriceVersion,
    RuleGenerationArtifact,
    RuleGenerationAttempt,
)
from app.parse_service import persist_partial_section_result
from app.parsing.mapping import MappingDecision
from app.parsing import loaders
from app.parsing.rule_proposal import (
    RULE_PROPOSAL_PROMPT_VERSION,
    RULE_PROPOSAL_SYSTEM_PROMPT,
    ProposalError,
    build_tabular_rule_generation_prompt,
    build_tabular_rule_repair_prompt,
    build_tabular_section_contexts,
    compile_batch_proposals,
    compile_section_proposal,
    context_hash,
    dry_run_decision,
    parse_proposal,
    prompt_hash,
    rule_proposal_json_schema,
)
from app.parsing.rule_proposal_handlers import (
    TABULAR_PROPOSAL_CONTRACT,
    build_rule_section_specs,
    build_rule_section_contexts,
    handler_for_contract,
)
from app.parsing.rule_generation_fast_path import prepare_deterministic_fast_path
from app.parsing.rule_generation_fast_path import verified_proposal_matches_fast_path
from app.parsing.rules import compute_fingerprint, compute_header_hash, recipe_from_decision
from app.parsing.matrix_unfold import MATRIX_PROPOSAL_CONTRACT, parse_matrix_regions
from app.parsing.validation_preview import build_validation_samples
from app.parsing.rules import validation_metrics
from app.parsing.workbook_layout import parse_workbook_layout
from app.rule_generation_artifacts import find_verified_artifact


TABULAR_PROPOSAL_LOADERS = {"xlsx", "xls", "docx", "doc", "txt"}
MIXED_PROPOSAL_PROMPT_VERSION = "mixed-section-proposals-v1"
ATTEMPT_QUEUED = "queued"
ATTEMPT_GENERATING = "generating"
ATTEMPT_PROPOSED = "proposed"
ATTEMPT_CATALOG_WITHOUT_PRICE = "catalog_without_price"
ATTEMPT_INSUFFICIENT = "insufficient_evidence"
ATTEMPT_SKIPPED_NON_PRICE = "skipped_non_price"
ATTEMPT_REJECTED = "rejected"
ATTEMPT_FAILED = "failed"
ATTEMPT_DRAFT_CREATED = "draft_created"
DETERMINISTIC_FAST_PATH_PROVIDER = "deterministic"
DETERMINISTIC_FAST_PATH_MODEL = "stage6b-v1"


def _zero_model_usage() -> dict[str, int | float]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "billable_input_tokens": 0,
        "cache_hit_ratio": 0.0,
    }


def _stub_price_descriptor_usable(descriptor: object) -> bool:
    return (
        isinstance(descriptor, dict)
        and isinstance(descriptor.get("kind"), str)
        and bool(descriptor["kind"].strip())
        and isinstance(descriptor.get("vat_mode"), str)
        and bool(descriptor["vat_mode"].strip())
    )


def _stub_context_identity(context: dict[str, Any]) -> tuple[str, str, int] | None:
    sheet_name = context.get("sheet_name")
    section_id = context.get("section_id")
    header_row_index = context.get("header_row_index")
    if (
        isinstance(sheet_name, str)
        and isinstance(section_id, str)
        and isinstance(header_row_index, int)
        and header_row_index >= 0
    ):
        return sheet_name, section_id, header_row_index
    return None


def build_stub_rule_proposal_payload(
    *,
    source_context: dict[str, Any] | None = None,
    loader: str = "xlsx",
    proposal_contract: str = TABULAR_PROPOSAL_CONTRACT,
) -> dict[str, Any]:
    """Build a deterministic, schema-conformant proposal for stub mode.

    The stub replaces the model answer without any external API call.  It is a
    pure function of the bounded section context: the same context always
    yields the same payload.  Source coordinates are taken only from locally
    verified deterministic evidence; anything ambiguous degrades to
    ``insufficient_evidence`` so the existing verifier keeps the decision.
    """

    context = source_context if isinstance(source_context, dict) else {}
    identity = _stub_context_identity(context)

    if proposal_contract == MATRIX_PROPOSAL_CONTRACT:
        return {
            "schema_version": "matrix-unfold-proposal-v2",
            "decision": "insufficient_evidence",
            "rule_name": "Stub matrix proposal",
            "source_section_id": identity[1] if identity is not None else "stub-matrix-section",
            "blocks": [],
            "unresolved": [
                "rule_generation_stub_mode: матричные оси требуют семантического разбора модели"
            ],
            "confidence": 0.0,
            "requires_manual_review": True,
        }

    if identity is None:
        return {
            "schema_version": "price-rule-proposal-v1",
            "decision": "insufficient_evidence",
            "rule_name": "Stub rule proposal",
            "loader": loader,
            "source_section_id": "stub-section",
            "mapping": None,
            "discovered_attributes": [],
            "evidence": [],
            "unresolved": [
                "rule_generation_stub_mode: координаты секции недоступны для стаба"
            ],
            "confidence": 0.0,
            "requires_manual_review": True,
        }

    sheet_name, section_id, header_row_index = identity
    observations = context.get("deterministic_observations")
    observations = observations if isinstance(observations, list) else []
    headerless_hint = context.get("headerless_mapping_hint")
    headerless_hint = headerless_hint if isinstance(headerless_hint, dict) else None

    resolved: dict[int, dict[str, Any]] = {}
    for observation in observations:
        if not isinstance(observation, dict):
            continue
        index = observation.get("index")
        role = observation.get("suggested_role")
        confidence = observation.get("confidence")
        if (
            not isinstance(index, int)
            or index < 0
            or not isinstance(role, str)
            or role == "ignore"
            or not isinstance(confidence, (int, float))
            or float(confidence) < 0.7
        ):
            continue
        resolved[index] = {
            "index": index,
            "role": role,
            "price_descriptor": (
                observation.get("price_descriptor") if role == "price" else None
            ),
            "attribute_code": observation.get("attribute_code"),
            "attribute_value_kind": observation.get("attribute_value_kind"),
            "canonical_unit": observation.get("canonical_unit"),
            "quantity_threshold_column_index": observation.get(
                "quantity_threshold_column_index"
            ),
        }
    if headerless_hint is not None:
        name_index = headerless_hint.get("name_column_index")
        if isinstance(name_index, int) and name_index not in resolved:
            resolved[name_index] = {
                "index": name_index,
                "role": "name",
                "price_descriptor": None,
                "attribute_code": None,
                "attribute_value_kind": None,
                "canonical_unit": None,
                "quantity_threshold_column_index": None,
            }
        unit_index = headerless_hint.get("unit_column_index")
        if isinstance(unit_index, int) and unit_index not in resolved:
            resolved[unit_index] = {
                "index": unit_index,
                "role": "unit",
                "price_descriptor": None,
                "attribute_code": None,
                "attribute_value_kind": None,
                "canonical_unit": None,
                "quantity_threshold_column_index": None,
            }
        for price_index in sorted(
            index
            for index in (headerless_hint.get("price_column_indexes") or [])
            if isinstance(index, int)
        ):
            if price_index not in resolved:
                resolved[price_index] = {
                    "index": price_index,
                    "role": "price",
                    "price_descriptor": {"kind": "other", "vat_mode": "unknown"},
                    "attribute_code": None,
                    "attribute_value_kind": None,
                    "canonical_unit": None,
                    "quantity_threshold_column_index": None,
                }

    roles = [resolved[index] for index in sorted(resolved)]
    name_roles = [role for role in roles if role["role"] == "name"]
    price_roles = [
        role for role in roles if role["role"] == "price"
    ]
    usable_price_roles = [
        role for role in price_roles if _stub_price_descriptor_usable(role.get("price_descriptor"))
    ]
    confidences = [
        float(role.get("confidence"))
        for role in roles
        if isinstance(role.get("confidence"), (int, float))
    ]
    confidence = round(min(confidences), 4) if confidences else 0.0

    if name_roles and usable_price_roles:
        columns: list[dict[str, Any]] = []
        for role in roles:
            is_price = role["role"] == "price"
            columns.append(
                {
                    "index": role["index"],
                    "role": role["role"],
                    "price_descriptor": role.get("price_descriptor") if is_price else None,
                    "attribute_code": role.get("attribute_code"),
                    "attribute_value_kind": role.get("attribute_value_kind"),
                    "canonical_unit": role.get("canonical_unit"),
                    "quantity_threshold_column_index": role.get(
                        "quantity_threshold_column_index"
                    ),
                }
            )
        return {
            "schema_version": "price-rule-proposal-v1",
            "decision": "propose",
            "rule_name": f"Stub: {sheet_name}"[:512],
            "loader": loader,
            "source_section_id": section_id,
            "mapping": {
                "sheet_name": sheet_name,
                "header_row_index": header_row_index,
                "name_fallback_column_indexes": [],
                "columns": columns,
            },
            "discovered_attributes": [],
            "evidence": [
                {
                    "row_index": header_row_index,
                    "column_index": role["index"],
                    "note": "stub deterministic mapping of a source column",
                }
                for role in roles
            ],
            "unresolved": [],
            "confidence": min(confidence, 0.69),
            "requires_manual_review": True,
        }

    return {
        "schema_version": "price-rule-proposal-v1",
        "decision": "insufficient_evidence",
        "rule_name": f"Stub: {sheet_name}"[:512],
        "loader": loader,
        "source_section_id": section_id,
        "mapping": None,
        "discovered_attributes": [],
        "evidence": [],
        "unresolved": [
            "rule_generation_stub_mode: нет детерминированной пары имя/цена "
            "или нераспознан price_descriptor"
        ],
        "confidence": 0.0,
        "requires_manual_review": True,
    }


class RuleModelError(RuntimeError):
    """An API/configuration failure safe to expose as an attempt diagnostic."""

    def __init__(
        self,
        message: str,
        *,
        telemetry: dict[str, Any] | None = None,
        response_payload: object = None,
    ):
        super().__init__(message)
        self.telemetry = telemetry
        self.response_payload = response_payload


@dataclass(frozen=True)
class RuleModelResult:
    payload: dict[str, Any]
    usage: dict[str, int | float]
    telemetry: dict[str, Any]


def serialise_attribute_catalog(definitions: list[object]) -> list[dict[str, Any]]:
    """Create a stable, sorted cache prefix from reviewed canonical meanings."""

    rows: list[dict[str, Any]] = []
    for definition in definitions:
        status = getattr(definition, "status", None)
        if status is not None and status != "active":
            continue
        rows.append(
            {
                "code": str(getattr(definition, "code")),
                "display_name": str(getattr(definition, "display_name")),
                "value_kind": str(getattr(definition, "value_kind")),
                "canonical_unit": getattr(definition, "canonical_unit", None),
            }
        )
    return sorted(rows, key=lambda row: row["code"])


def is_retryable_model_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


def is_retryable_model_exception(exc: Exception) -> bool:
    return isinstance(exc, httpx.TransportError)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _short_error(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return text[:800] if text else type(exc).__name__


def build_attempt_specs(
    *, grids, loader: str, model: str, attribute_catalog: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Return immutable per-section attempt inputs; does not contact a model."""

    if loader not in TABULAR_PROPOSAL_LOADERS:
        return []
    return build_rule_section_specs(
        grids=grids,
        loader=loader,
        model=model,
        attribute_catalog=attribute_catalog,
    )


def batch_prompt_version(specs: list[dict[str, Any]]) -> str:
    """Describe a batch without labelling mixed contracts as tabular."""

    versions = {str(spec["prompt_version"]) for spec in specs}
    if len(versions) == 1:
        return next(iter(versions))
    return MIXED_PROPOSAL_PROMPT_VERSION


def verified_current_context(
    *,
    attempt: RuleGenerationAttempt,
    grids,
    current_contexts: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the current section only when it still equals the audited input.

    ``storage_key`` normally points to immutable content, but that convention
    must not be the only safety boundary.  Rebuilding the bounded section
    context after loading the original prevents a queued task (or a later
    draft action) from applying a model answer to a replaced file or a
    tampered attempt row.
    """

    saved_context = attempt.input_context
    if (
        not isinstance(saved_context, dict)
        or context_hash(saved_context) != attempt.source_context_hash
    ):
        raise ProposalError("Сохранённый контекст попытки повреждён")
    current_contexts = current_contexts or {
        str(context["section_id"]): context for context in build_rule_section_contexts(grids)
    }
    current_context = current_contexts.get(attempt.section_id)
    if current_context is None:
        raise ProposalError("Секция источника больше не найдена")
    if context_hash(current_context) != attempt.source_context_hash:
        raise ProposalError("Источник изменился после подготовки GPT-запроса")
    return current_context


def draft_rule_name(filename: str) -> str:
    """Use the selected source file as the stable human-facing draft name."""

    name = Path(filename).stem.strip()
    return name or filename.strip() or "Новое правило разбора"


def verified_source_bytes(
    *, attempt: RuleGenerationAttempt, price_file: PriceFile, data: bytes
) -> None:
    """Bind a proposal attempt to the exact immutable source bytes."""

    digest = sha256(data).hexdigest()
    if digest != price_file.sha256 or digest != attempt.source_file_sha256:
        raise ProposalError("Оригинал прайса изменился после создания GPT-batch")


def build_chat_completions_request(
    *,
    prompt: str,
    model: str,
    provider: str,
    max_output_tokens: int,
    temperature: float = 0,
    top_p: float = 1,
    thinking: str = "disabled",
    reasoning_effort: str = "high",
    proposal_contract: str = TABULAR_PROPOSAL_CONTRACT,
) -> dict[str, Any]:
    """Build the provider-specific OpenAI-compatible request body."""

    handler = handler_for_contract(proposal_contract)
    system_prompt = handler.system_prompt
    output_schema = handler.json_schema()
    if provider.casefold() == "deepseek":
        # DeepSeek's stable Chat Completions endpoint supports JSON mode, but
        # not OpenAI's response_format=json_schema.  Put the same strict
        # contract into the fixed system prefix so the provider can cache it
        # across section requests and cannot mistake the input context schema
        # for the required output schema.
        output_schema_text = json.dumps(
            output_schema,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        system_prompt += (
            "\n\nDeepSeek JSON mode: обязательный контракт ответа приведён ниже. "
            f"Версия контракта входа: {handler.prompt_version}. "
            "Верни объект строго по этой JSON Schema и не копируй входной контекст. "
            "Все required-поля и null-поля должны присутствовать. JSON Schema:\n"
            + output_schema_text
        )
    request_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "top_p": top_p,
    }
    if provider.casefold() == "deepseek":
        request_body["response_format"] = {"type": "json_object"}
        request_body["thinking"] = {"type": thinking}
        request_body["reasoning_effort"] = reasoning_effort
        if thinking == "enabled":
            # DeepSeek documents these sampling parameters as ineffective in
            # thinking mode, so omit them from the wire request and audit the
            # chosen mode instead of implying that they took effect.
            request_body.pop("temperature")
            request_body.pop("top_p")
    else:
        request_body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": handler.schema_name,
                "strict": True,
                "schema": output_schema,
            },
        }
    token_parameter = "max_tokens" if provider.casefold() in {"yandexgpt", "deepseek"} else "max_completion_tokens"
    request_body[token_parameter] = max_output_tokens
    return request_body


async def _request_rule_proposal_result(
    *,
    prompt: str,
    model: str,
    request_config: RuleModelRequestConfig | None = None,
    telemetry_dimensions: dict[str, str] | None = None,
    proposal_contract: str = TABULAR_PROPOSAL_CONTRACT,
    source_context: dict[str, Any] | None = None,
    loader: str | None = None,
) -> RuleModelResult:
    """Call an OpenAI-compatible Chat Completions API with JSON Schema.

    GPT-4o supports Structured Outputs.  We use a JSON Schema response format
    rather than JSON mode, then still validate against the original grid
    locally because a schema cannot prove source coordinates or parser quality.
    """

    request_config = request_config or resolve_rule_model_config(model=model)
    operation_id = str(uuid.uuid4())
    started_at = _now()
    http_attempts = 0
    http_status_code: int | None = None
    response_id: str | None = None
    finish_reason: str | None = None
    usage: dict[str, int | float] = normalise_model_usage(None)
    raw_usage: dict[str, Any] = {}
    http_attempt_details: list[dict[str, Any]] = []
    request_body: dict[str, Any] | None = None
    response_body: object = None
    retry_after: str | None = None

    def event(*, error_type: str | None = None) -> dict[str, Any]:
        result = build_llm_request_event(
            operation_id=operation_id,
            provider=request_config.provider,
            model=model,
            started_at=started_at,
            finished_at=_now(),
            http_attempts=http_attempts,
            http_status_code=http_status_code,
            finish_reason=finish_reason,
            response_id=response_id,
            usage=usage,
            dimensions=telemetry_dimensions,
            request_settings=request_config.persisted_settings(),
            raw_usage=raw_usage,
            http_attempt_details=http_attempt_details,
            error_type=error_type,
        )
        if retry_after:
            result["retry_after"] = retry_after
        return result

    def failure(message: str, error_type: str) -> RuleModelError:
        telemetry = event(error_type=error_type)
        emit_llm_request_event(
            telemetry,
            request_body=request_body,
            response_body=response_body,
        )
        return RuleModelError(
            message,
            telemetry=telemetry,
            response_payload=response_body,
        )

    if rule_generation_stub_enabled():
        # Dev stub: deterministic schema-conformant answer, zero provider calls.
        # The flag is a pure dev escape hatch; with a real key and the flag off
        # the unchanged provider workflow below is used.
        payload = build_stub_rule_proposal_payload(
            source_context=source_context,
            loader=loader or "xlsx",
            proposal_contract=proposal_contract,
        )
        operation_id = "stub-" + sha256(
            "\0".join(
                (prompt, str(proposal_contract), request_config.provider, model)
            ).encode("utf-8")
        ).hexdigest()[:20]
        response_id = "stub-" + sha256(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()[:20]
        telemetry = event()
        emit_llm_request_event(
            telemetry,
            request_body=None,
            response_body=payload,
        )
        return RuleModelResult(payload=payload, usage=usage, telemetry=telemetry)

    api_key = request_config.api_key
    if not api_key:
        raise failure(
            "API-ключ LLM-провайдера не задан: генерация правил отключена",
            "configuration_error",
        )
    endpoint = request_config.api_base_url.rstrip("/") + "/chat/completions"
    request_body = build_chat_completions_request(
        prompt=prompt,
        model=model,
        provider=request_config.provider,
        max_output_tokens=request_config.max_output_tokens,
        temperature=request_config.temperature,
        top_p=request_config.top_p,
        thinking=request_config.thinking,
        reasoning_effort=request_config.reasoning_effort,
        proposal_contract=proposal_contract,
    )
    try:
        async with httpx.AsyncClient(timeout=request_config.timeout_seconds) as client:
            for attempt_index in range(3):
                http_attempts += 1
                attempt_started_at = _now()
                try:
                    response = await client.post(
                        endpoint,
                        headers={"Authorization": f"Bearer {api_key}"},
                        json=request_body,
                    )
                except httpx.HTTPError as exc:
                    http_attempt_details.append(
                        {
                            "attempt": http_attempts,
                            "duration_ms": max(
                                0,
                                round(
                                    (_now() - attempt_started_at).total_seconds() * 1000
                                ),
                            ),
                            "error_type": type(exc).__name__,
                            "error": _short_error(exc),
                        }
                    )
                    if not is_retryable_model_exception(exc) or attempt_index == 2:
                        raise
                    await asyncio.sleep(2**attempt_index)
                    continue
                http_status_code = response.status_code
                retry_after = response.headers.get("Retry-After")
                http_attempt_details.append(
                    {
                        "attempt": http_attempts,
                        "duration_ms": max(
                            0,
                            round((_now() - attempt_started_at).total_seconds() * 1000),
                        ),
                        "http_status_code": response.status_code,
                    }
                )
                if not is_retryable_model_status(response.status_code) or attempt_index == 2:
                    break
                await asyncio.sleep(2**attempt_index)
        if response.is_error:
            try:
                response_body = response.json()
            except ValueError:
                response_body = {"raw_text": response.text}
            raise failure(
                f"LLM-провайдер вернул HTTP {response.status_code}",
                "provider_http_error",
            )
        try:
            body = response.json()
        except ValueError as exc:
            response_body = {"raw_text": response.text}
            raise failure(
                "LLM-провайдер вернул не-JSON HTTP-ответ", "invalid_http_json"
            ) from exc
        response_body = body
    except httpx.HTTPError as exc:
        raise failure(
            f"Ошибка вызова LLM-провайдера: {_short_error(exc)}", "transport_error"
        ) from exc

    if not isinstance(body, dict):
        raise failure("JSON-ответ LLM-провайдера должен быть объектом", "invalid_http_shape")
    response_id = str(body.get("id")) if body.get("id") is not None else None
    raw_usage_value = body.get("usage")
    raw_usage = raw_usage_value if isinstance(raw_usage_value, dict) else {}
    usage = normalise_model_usage(raw_usage)

    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise failure("LLM-провайдер не вернул choice с JSON-предложением", "missing_choice")
    finish_reason = (
        str(choices[0].get("finish_reason"))
        if choices[0].get("finish_reason") is not None
        else None
    )
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise failure("LLM-провайдер не вернул message с JSON-предложением", "missing_message")
    if message.get("refusal"):
        raise failure("Модель отказалась сформировать предложение правила", "model_refusal")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise failure("LLM-провайдер не вернул текст JSON-предложением", "empty_content")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise failure("Ответ модели не является корректным JSON", "invalid_content_json") from exc
    if not isinstance(parsed, dict):
        raise failure("JSON-ответ модели должен быть объектом", "invalid_content_shape")
    telemetry = event()
    emit_llm_request_event(
        telemetry,
        request_body=request_body,
        response_body=response_body,
    )
    return RuleModelResult(payload=parsed, usage=usage, telemetry=telemetry)


async def request_rule_proposal(*, prompt: str, model: str) -> dict[str, Any]:
    """Compatibility wrapper used by benchmark scripts."""

    return (await _request_rule_proposal_result(prompt=prompt, model=model)).payload


async def _durable_model_result(
    session: AsyncSession,
    *,
    attempt: RuleGenerationAttempt,
    operation_no: int,
    operation_kind: str,
    prompt: str,
    request_config: RuleModelRequestConfig,
    proposal_contract: str,
    telemetry_dimensions: dict[str, str],
    allow_operation_recovery: bool = False,
    source_context: dict[str, Any] | None = None,
    loader: str | None = None,
) -> RuleModelResult:
    """Resume or execute one paid operation with a commit before validation."""

    reservation = await reserve_operation(
        session,
        attempt_id=attempt.id,
        operation_no=operation_no,
        operation_kind=operation_kind,
        provider=request_config.provider,
        model=request_config.model,
        allow_recovery=allow_operation_recovery,
    )
    if reservation.stored_result is not None:
        return RuleModelResult(
            payload=reservation.stored_result.payload,
            usage=reservation.stored_result.usage,
            telemetry=reservation.stored_result.telemetry,
        )
    try:
        result = await _request_rule_proposal_result(
            prompt=prompt,
            model=attempt.model,
            request_config=request_config,
            proposal_contract=proposal_contract,
            telemetry_dimensions=telemetry_dimensions,
            source_context=source_context,
            loader=loader,
        )
    except RuleModelError as exc:
        await fail_operation(
            session,
            operation_id=reservation.operation_id,
            telemetry=exc.telemetry,
            response_payload=exc.response_payload,
            secrets=(request_config.api_key or "",),
        )
        raise
    except Exception as exc:
        telemetry = {
            "provider": request_config.provider,
            "model": request_config.model,
            "finished_at": _now().isoformat(),
            "http_attempts": 1,
            "http_status_code": None,
            "error_type": (
                "transport_error"
                if isinstance(exc, httpx.TransportError)
                else type(exc).__name__
            ),
            "usage": {},
        }
        await fail_operation(
            session,
            operation_id=reservation.operation_id,
            telemetry=telemetry,
            secrets=(request_config.api_key or "",),
        )
        raise RuleModelError(str(exc), telemetry=telemetry) from exc
    stored = await complete_operation(
        session,
        operation_id=reservation.operation_id,
        payload=result.payload,
        usage=result.usage,
        telemetry=result.telemetry,
        secrets=(request_config.api_key or "",),
    )
    # Continue from the committed, redacted representation.  The compiler
    # never consumes a response that exists only in process memory.
    return RuleModelResult(
        payload=stored.payload,
        usage=stored.usage,
        telemetry=stored.telemetry,
    )


async def run_rule_generation_attempt(
    session: AsyncSession,
    attempt_id: uuid.UUID,
    *,
    lease_claimed: bool = False,
    recover_started_operation: bool = False,
) -> str:
    """Run one queued section call and persist only its reviewed artifact."""

    attempt = await session.get(RuleGenerationAttempt, attempt_id)
    if attempt is None:
        return "not_found"
    durable_enabled = settings.durable_rule_generation_enabled
    if lease_claimed and durable_enabled:
        if attempt.status != "leased":
            return attempt.status
    elif attempt.status != ATTEMPT_QUEUED and not (
        durable_enabled and attempt.status == ATTEMPT_GENERATING
    ):
        return attempt.status

    if lease_claimed and durable_enabled:
        await session.commit()
    elif attempt.status == ATTEMPT_QUEUED:
        # Celery provides at-least-once delivery. Claim the row atomically before
        # the paid request so duplicate delivery cannot result in two model calls.
        claimed = await session.execute(
            update(RuleGenerationAttempt)
            .where(
                RuleGenerationAttempt.id == attempt_id,
                RuleGenerationAttempt.status == ATTEMPT_QUEUED,
            )
            .values(status=ATTEMPT_GENERATING, error_message=None, updated_at=_now())
        )
        if claimed.rowcount != 1:
            await session.refresh(attempt)
            return attempt.status
        await session.commit()
    else:
        # A durable retry may resume a paid response left after process death.
        # Operation reservation below prevents a concurrent second provider call.
        await session.commit()

    # Refresh the identity-map object after the claim/commit so all later
    # writes operate on the authoritative attempt state.
    attempt = await session.get(RuleGenerationAttempt, attempt_id)
    if attempt is None:  # pragma: no cover - cascade deletion race
        return "not_found"
    version = await session.get(PriceVersion, attempt.price_version_id)
    if version is None:
        attempt.status = ATTEMPT_FAILED
        attempt.error_message = "Связанная версия прайса не найдена"
        attempt.updated_at = _now()
        await session.commit()
        return attempt.status
    price_file = await session.get(PriceFile, version.price_file_id)
    if price_file is None:
        attempt.status = ATTEMPT_FAILED
        attempt.error_message = "Оригинал прайса не найден"
        attempt.updated_at = _now()
        await session.commit()
        return attempt.status

    proposal = None
    request_config: RuleModelRequestConfig | None = None
    work_identity: WorkIdentity | None = None
    try:
        data = storage.get_price_file(price_file.storage_key)
        verified_source_bytes(attempt=attempt, price_file=price_file, data=data)
        loader = loaders.detect_loader(data, price_file.original_filename)
        if loader not in TABULAR_PROPOSAL_LOADERS:
            raise RuleModelError(f"Загрузчик {loader} не поддерживает section mapping")
        grids = loaders.load_grids_for(loader, data, price_file.original_filename)
        current_context = verified_current_context(attempt=attempt, grids=grids)
        proposal_contract = current_context.get(
            "proposal_contract", TABULAR_PROPOSAL_CONTRACT
        )
        handler = handler_for_contract(proposal_contract)
        is_matrix = proposal_contract == MATRIX_PROPOSAL_CONTRACT
        request_config = config_from_attempt(
            provider=attempt.provider,
            model=attempt.model,
            request_settings=attempt.request_settings,
        )
        model_identity = (
            build_work_identity(
                attempt,
                provider=request_config.provider,
                model=request_config.model,
                request_settings=request_config.persisted_settings(),
                contract_version=str(proposal_contract),
            )
            if durable_enabled
            else None
        )
        known_artifact = (
            await find_verified_artifact(session, model_identity.work_key)
            if model_identity is not None
            else None
        )
        fast_path_fallback: dict[str, Any] | None = None
        if settings.rule_generation_fast_path_enabled:
            try:
                deterministic = prepare_deterministic_fast_path(
                    current_context, loader=loader, grids=grids
                )
            except Exception as exc:  # fail closed into the established LLM flow
                fast_path_fallback = {
                    "outcome": "fallback_to_llm",
                    "reason": type(exc).__name__,
                    "detail": _short_error(exc),
                }
            else:
                if deterministic is not None:
                    if known_artifact is not None:
                        try:
                            artifact_matches = verified_proposal_matches_fast_path(
                                deterministic,
                                known_artifact.proposal,
                                context=current_context,
                                loader=loader,
                                grids=grids,
                            )
                        except Exception as exc:
                            artifact_matches = False
                            artifact_guard_detail = _short_error(exc)
                        else:
                            artifact_guard_detail = None
                        if not artifact_matches:
                            fast_path_fallback = {
                                "outcome": "fallback_to_llm",
                                "reason": "verified_artifact_output_mismatch",
                                **(
                                    {"detail": artifact_guard_detail}
                                    if artifact_guard_detail is not None
                                    else {}
                                ),
                            }
                            deterministic = None
                if deterministic is not None:
                    deterministic_identity = build_work_identity(
                        attempt,
                        provider=DETERMINISTIC_FAST_PATH_PROVIDER,
                        model=DETERMINISTIC_FAST_PATH_MODEL,
                        request_settings={"mode": "deterministic_fast_path"},
                        contract_version=str(proposal_contract),
                    )
                    if deterministic_identity is None:
                        fast_path_fallback = {
                            "outcome": "fallback_to_llm",
                            "reason": "incomplete_work_identity",
                        }
                    else:
                        attempt.proposal = deterministic.proposal
                        attempt.validation = {
                            **deterministic.validation,
                            "model_usage": _zero_model_usage(),
                            "semantic_retries": 0,
                            "execution_path": "deterministic_fast_path",
                            "fast_path": {
                                "outcome": "passed",
                                "provider_calls": 0,
                                "evaluator": deterministic.evaluator,
                            },
                        }
                        attempt.status = "leased" if lease_claimed else ATTEMPT_PROPOSED
                        try:
                            await create_and_attach_verified_artifact(
                                session,
                                attempt=attempt,
                                identity=deterministic_identity,
                                proposal=attempt.proposal,
                                validation=attempt.validation,
                                compiler_dry_run_summary={
                                    "passed": True,
                                    "attempt_status": ATTEMPT_PROPOSED,
                                    "local_validation": deterministic.validation,
                                    "execution_path": "deterministic_fast_path",
                                },
                                provenance="deterministic_fast_path",
                            )
                        except Exception as exc:  # persistence must also fail closed
                            await session.rollback()
                            attempt = await session.get(RuleGenerationAttempt, attempt_id)
                            if attempt is None:
                                return "not_found"
                            fast_path_fallback = {
                                "outcome": "fallback_to_llm",
                                "reason": "artifact_persistence_failed",
                                "detail": _short_error(exc),
                            }
                        else:
                            attempt.updated_at = _now()
                            await session.commit()
                            return ATTEMPT_PROPOSED
        if durable_enabled:
            work_identity = model_identity
            if work_identity is not None:
                reused = await attach_exact_artifact(
                    session,
                    attempt=attempt,
                    identity=work_identity,
                    preserve_attempt_status=lease_claimed,
                )
                if reused is not None:
                    if fast_path_fallback is not None:
                        attempt.validation = {
                            **dict(attempt.validation or {}),
                            "fast_path": fast_path_fallback,
                        }
                        await session.commit()
                    reused_status = reused.compiler_dry_run_summary.get(
                        "attempt_status", ATTEMPT_PROPOSED
                    )
                    return str(reused_status)
        model_results: list[RuleModelResult] = []
        current_prompt = attempt.prompt
        compiled = None
        validation = None
        for correction_index in range(2):
            telemetry_dimensions = {
                **(
                    {"price_import_id": str(attempt.price_import_id)}
                    if attempt.price_import_id is not None
                    else {}
                ),
                "price_version_id": str(attempt.price_version_id),
                "batch_id": str(attempt.batch_id),
                "attempt_id": str(attempt.id),
                "section_id": attempt.section_id,
                "prompt_id": attempt.prompt_id,
                "semantic_attempt": str(correction_index + 1),
            }
            if durable_enabled:
                model_result = await _durable_model_result(
                    session,
                    attempt=attempt,
                    operation_no=correction_index + 1,
                    operation_kind=(
                        OPERATION_INITIAL
                        if correction_index == 0
                        else OPERATION_SEMANTIC_REPAIR
                    ),
                    prompt=current_prompt,
                    request_config=request_config,
                    proposal_contract=proposal_contract,
                    telemetry_dimensions=telemetry_dimensions,
                    allow_operation_recovery=recover_started_operation,
                    source_context=current_context,
                    loader=loader,
                )
            else:
                model_result = await _request_rule_proposal_result(
                    prompt=current_prompt,
                    model=attempt.model,
                    request_config=request_config,
                    proposal_contract=proposal_contract,
                    telemetry_dimensions=telemetry_dimensions,
                    source_context=current_context,
                    loader=loader,
                )
            model_results.append(model_result)
            aggregate_usage = aggregate_model_usage(
                [result.usage for result in model_results]
            )
            telemetry_events = [result.telemetry for result in model_results]
            attempt.validation = {
                **(
                    {"fast_path": fast_path_fallback}
                    if fast_path_fallback is not None
                    else {}
                ),
                "model_usage": aggregate_usage,
                "llm_telemetry": telemetry_events[-1],
                "llm_telemetry_events": telemetry_events,
                "semantic_retries": correction_index,
            }
            raw_proposal = model_result.payload
            try:
                proposal = handler.parse(raw_proposal)
                # Retain a schema-valid answer even when the source compiler
                # rejects it. The operator sees both proposal and verifier.
                attempt.proposal = proposal.model_dump(mode="json")
                if is_matrix:
                    if proposal.decision != "propose":
                        compiled = {"decision": proposal.decision}
                        validation = {"passed": True, "decision": proposal.decision}
                    else:
                        compiled = handler.compile(
                            proposal, source_context=current_context, grids=grids
                        )
                        matrix_result = parse_matrix_regions(grids, [compiled])
                        validation = validation_metrics(matrix_result)
                    accepted = bool(validation["passed"])
                    feedback = validation
                else:
                    compiled = handler.compile(
                        proposal,
                        source_context=current_context,
                        loader=loader,
                        grids=grids,
                    )
                    validation = None
                    if compiled.table is not None:
                        decision = MappingDecision(tables=[compiled.table], auto_ok=True)
                        _, validation = dry_run_decision(grids, decision)
                    accepted = (
                        validation["passed"]
                        if validation is not None
                        else bool(compiled.validation.get("passed"))
                    )
                    feedback = validation if validation is not None else compiled.validation
            except (ProposalError, ValueError) as exc:
                accepted = False
                feedback = {"source_compiler_error": _short_error(exc)}
                if correction_index == 1:
                    raise
            if accepted or correction_index == 1:
                break
            current_prompt = handler.build_repair_prompt(
                base_prompt=attempt.prompt,
                previous_payload=raw_proposal,
                verifier_feedback=feedback,
            )

        if compiled is None:  # defensive: loop either compiles or raises
            raise ProposalError("Модель не сформировала компилируемое предложение")
        aggregate_usage = aggregate_model_usage([result.usage for result in model_results])
        telemetry_events = [result.telemetry for result in model_results]
        common_validation = {
            **(
                {"fast_path": fast_path_fallback}
                if fast_path_fallback is not None
                else {}
            ),
            "model_usage": aggregate_usage,
            "llm_telemetry": telemetry_events[-1],
            "llm_telemetry_events": telemetry_events,
            "semantic_retries": len(model_results) - 1,
        }
        if is_matrix and proposal.decision != "propose":
            attempt.validation = {**validation, **common_validation}
            attempt.status = ATTEMPT_INSUFFICIENT
            attempt.error_message = None
        elif not is_matrix and compiled.table is None:
            attempt.validation = {
                **compiled.validation,
                **common_validation,
            }
            attempt.status = (
                ATTEMPT_SKIPPED_NON_PRICE
                if proposal.decision == "not_price_section"
                else (
                    ATTEMPT_CATALOG_WITHOUT_PRICE
                    if proposal.decision == "catalog_without_price"
                    else ATTEMPT_INSUFFICIENT
                )
            )
            attempt.error_message = None
        else:
            if validation is None:
                raise ProposalError("Для предложения отсутствует dry-run")
            attempt.validation = {
                **({} if is_matrix else compiled.validation),
                **validation,
                **common_validation,
            }
            attempt.status = ATTEMPT_PROPOSED if validation["passed"] else ATTEMPT_REJECTED
            attempt.error_message = (
                None
                if validation["passed"]
                else "Черновик не прошёл локальный dry-run / quality gate"
            )
        verified_statuses = {
            ATTEMPT_PROPOSED,
            ATTEMPT_INSUFFICIENT,
            ATTEMPT_CATALOG_WITHOUT_PRICE,
            ATTEMPT_SKIPPED_NON_PRICE,
        }
        verified_outcome = attempt.status
        if lease_claimed:
            attempt.status = "leased"
        if (
            durable_enabled
            and work_identity is not None
            and verified_outcome in verified_statuses
            and isinstance(attempt.proposal, dict)
            and isinstance(attempt.validation, dict)
        ):
            await create_and_attach_verified_artifact(
                session,
                attempt=attempt,
                identity=work_identity,
                proposal=attempt.proposal,
                validation=attempt.validation,
                compiler_dry_run_summary={
                    "passed": True,
                    "attempt_status": verified_outcome,
                    "local_validation": (
                        validation
                        if isinstance(validation, dict)
                        else getattr(compiled, "validation", {})
                    ),
                },
            )
        if lease_claimed:
            attempt.status = verified_outcome
    except (ProposalError, ValueError) as exc:
        attempt.status = ATTEMPT_REJECTED
        attempt.error_message = _short_error(exc)
        attempt.validation = {
            **(attempt.validation or {}),
            "schema_passed": proposal is not None,
            "source_passed": False,
            "passed": False,
        }
    except DurableOperationInProgress:
        await session.rollback()
        return ATTEMPT_GENERATING
    except DurableOperationFailed as exc:
        attempt.status = ATTEMPT_FAILED
        attempt.error_message = str(exc)[:800]
    except RuleModelError as exc:
        attempt.status = ATTEMPT_FAILED
        attempt.error_message = (
            redact_sensitive_text(
                _short_error(exc),
                secrets=((request_config.api_key or "") if request_config else "",),
            )
            if durable_enabled
            else _short_error(exc)
        )
        if exc.telemetry:
            previous = attempt.validation if isinstance(attempt.validation, dict) else {}
            events = previous.get("llm_telemetry_events")
            if not isinstance(events, list):
                prior_event = previous.get("llm_telemetry")
                events = [prior_event] if isinstance(prior_event, dict) else []
            if not any(
                event.get("operation_id") == exc.telemetry.get("operation_id")
                for event in events
                if isinstance(event, dict)
            ):
                events.append(exc.telemetry)
            previous_usage = previous.get("model_usage")
            usages = [previous_usage] if isinstance(previous_usage, dict) else []
            event_usage = exc.telemetry.get("usage")
            if isinstance(event_usage, dict):
                usages.append(event_usage)
            attempt.validation = {
                **previous,
                "llm_telemetry": exc.telemetry,
                "llm_telemetry_events": events,
                "model_usage": aggregate_model_usage(usages),
            }
    except Exception as exc:  # noqa: BLE001 - task must leave a visible status
        attempt.status = ATTEMPT_FAILED
        attempt.error_message = f"{type(exc).__name__}: {_short_error(exc)}"
    outcome_status = attempt.status
    if lease_claimed:
        attempt.status = "leased"
    attempt.updated_at = _now()
    await session.commit()
    return outcome_status


DraftProgressCallback = Callable[[str, int, int, int, str], Awaitable[None]]


def _compile_succeeded_attempt_result(
    *,
    attempt: RuleGenerationAttempt,
    raw_proposal: dict[str, Any],
    loader: str,
    grids,
    current_contexts: dict[str, dict[str, Any]],
):
    """Recompile one stored response through the existing source verifier."""

    current_context = verified_current_context(
        attempt=attempt,
        grids=grids,
        current_contexts=current_contexts,
    )
    proposal_contract = current_context.get(
        "proposal_contract", TABULAR_PROPOSAL_CONTRACT
    )
    handler = handler_for_contract(proposal_contract)
    proposal = handler.parse(raw_proposal)
    if proposal.decision != "propose":
        raise ProposalError("Successful section не содержит товарного предложения")
    if proposal_contract == MATRIX_PROPOSAL_CONTRACT:
        region = handler.compile(
            proposal, source_context=current_context, grids=grids
        )
        result = parse_matrix_regions(grids, [region])
        passed = bool(validation_metrics(result)["passed"])
    else:
        compiled = handler.compile(
            proposal,
            source_context=current_context,
            loader=loader,
            grids=grids,
        )
        if compiled.table is None:
            raise ProposalError("Successful section не содержит executable table")
        result, validation = dry_run_decision(
            grids, MappingDecision(tables=[compiled.table], auto_ok=True)
        )
        passed = bool(validation["passed"])
    if not passed:
        raise ProposalError("Section не прошла повторный compiler dry-run")
    if not result.items:
        raise ProposalError("Successful section не создала ни одной позиции")
    if any(item.source_section_id != attempt.section_id for item in result.items):
        raise ProposalError("Compiler вернул данные другой section")
    return result


async def materialize_succeeded_sections(
    session: AsyncSession,
    *,
    version: PriceVersion,
    price_file: PriceFile,
    data: bytes,
    attempts: list[RuleGenerationAttempt],
) -> dict[str, int]:
    """Persist newly successful sections without changing final version status.

    Each section commit contains both its item replacement and a marker in the
    attempt validation JSON. A crash therefore leaves either both or neither,
    and repeated coordinator deliveries skip already materialized sections.
    """

    successful = [
        attempt
        for attempt in attempts
        if attempt.status in {ATTEMPT_PROPOSED, "succeeded"}
    ]
    pending: list[RuleGenerationAttempt] = []
    for attempt in successful:
        validation = attempt.validation if isinstance(attempt.validation, dict) else {}
        marker = validation.get("partial_materialization")
        if (
            isinstance(marker, dict)
            and marker.get("completed") is True
            and marker.get("source_context_hash") == attempt.source_context_hash
        ):
            continue
        pending.append(attempt)
    if not pending:
        return {
            "successful_sections": len(successful),
            "sections_materialized": 0,
            "sections_already_materialized": len(successful),
            "items_materialized": 0,
            "prices_materialized": 0,
        }

    verified_source_bytes(attempt=pending[0], price_file=price_file, data=data)
    loader = loaders.detect_loader(data, price_file.original_filename)
    if loader not in TABULAR_PROPOSAL_LOADERS:
        raise ProposalError(f"Загрузчик {loader} не поддерживает section mapping")
    grids = loaders.load_grids_for(loader, data, price_file.original_filename)
    current_contexts = {
        str(context["section_id"]): context
        for context in build_rule_section_contexts(grids)
    }
    items_materialized = 0
    prices_materialized = 0
    for attempt in pending:
        verified_source_bytes(attempt=attempt, price_file=price_file, data=data)
        raw_proposal = attempt.proposal
        if not isinstance(raw_proposal, dict) and attempt.artifact_id is not None:
            artifact = await session.get(RuleGenerationArtifact, attempt.artifact_id)
            raw_proposal = artifact.proposal if artifact is not None else None
        if not isinstance(raw_proposal, dict):
            raise ProposalError(
                f"Для successful section {attempt.section_id} отсутствует сохранённый response"
            )
        result = _compile_succeeded_attempt_result(
            attempt=attempt,
            raw_proposal=raw_proposal,
            loader=loader,
            grids=grids,
            current_contexts=current_contexts,
        )
        persisted = await persist_partial_section_result(
            session,
            version=version,
            section_id=attempt.section_id,
            result=result,
        )
        validation = dict(attempt.validation or {})
        validation["partial_materialization"] = {
            "completed": True,
            "completed_at": _now().isoformat(),
            "source_context_hash": attempt.source_context_hash,
            "items": persisted["items"],
            "prices": persisted["prices"],
            "attribute_warnings": persisted["attribute_warnings"],
        }
        attempt.validation = validation
        attempt.updated_at = _now()
        items_materialized += persisted["items"]
        prices_materialized += persisted["prices"]
        await session.commit()
    return {
        "successful_sections": len(successful),
        "sections_materialized": len(pending),
        "sections_already_materialized": len(successful) - len(pending),
        "items_materialized": items_materialized,
        "prices_materialized": prices_materialized,
    }


async def create_model_draft_from_batch(
    session: AsyncSession,
    *,
    version_id: uuid.UUID,
    batch_id: uuid.UUID,
    progress: DraftProgressCallback | None = None,
):
    """Compile all successful section calls into one unactivated parse-rule draft."""

    # Serialise explicit draft creation for one version. Without this lock two
    # double-clicks could both see all attempts as proposed and create
    # competing rules before either transaction commits.
    version = (
        await session.execute(
            select(PriceVersion)
            .where(PriceVersion.id == version_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if version is None:
        raise ProposalError("Версия прайса не найдена")
    attempts = (
        (
            await session.execute(
                select(RuleGenerationAttempt)
                .where(
                    RuleGenerationAttempt.price_version_id == version_id,
                    RuleGenerationAttempt.batch_id == batch_id,
                )
                .order_by(RuleGenerationAttempt.created_at, RuleGenerationAttempt.id)
            )
        )
        .scalars()
        .all()
    )
    if not attempts:
        raise ProposalError("Попытки для указанного batch не найдены")
    if all(attempt.status == ATTEMPT_DRAFT_CREATED and attempt.parse_rule_id for attempt in attempts):
        rule = await session.get(ParseRule, attempts[0].parse_rule_id)
        if rule is not None:
            return rule
    ready_statuses = {
        ATTEMPT_PROPOSED,
        "succeeded",
        ATTEMPT_CATALOG_WITHOUT_PRICE,
        ATTEMPT_SKIPPED_NON_PRICE,
    }
    not_ready = [attempt for attempt in attempts if attempt.status not in ready_statuses]
    if not_ready:
        states = ", ".join(f"{attempt.section_id}: {attempt.status}" for attempt in not_ready)
        raise ProposalError(f"Не все секции batch прошли проверку: {states}")
    proposed_attempts = [
        attempt
        for attempt in attempts
        if attempt.status in {ATTEMPT_PROPOSED, "succeeded"}
        and isinstance(attempt.proposal, dict)
        and attempt.proposal.get("decision") == "propose"
    ]
    if not proposed_attempts:
        raise ProposalError("Batch не содержит ни одной подтверждённой товарной таблицы")

    price_file = await session.get(PriceFile, version.price_file_id)
    if price_file is None:
        raise ProposalError("Оригинал прайса не найден")
    if progress:
        await progress(
            "loading_source", 0, len(attempts), 5,
            "Загружаем исходный файл и проверяем его целостность",
        )
    data = storage.get_price_file(price_file.storage_key)
    for attempt in attempts:
        verified_source_bytes(attempt=attempt, price_file=price_file, data=data)
    loader = loaders.detect_loader(data, price_file.original_filename)
    if loader not in TABULAR_PROPOSAL_LOADERS:
        raise ProposalError(f"Загрузчик {loader} не поддерживает section mapping")
    grids = loaders.load_grids_for(loader, data, price_file.original_filename)
    if progress:
        await progress(
            "finding_sections", 0, len(attempts), 12,
            "Находим в файле все секции, подтверждённые DeepSeek",
        )
    source_contexts = {
        str(context["section_id"]): context
        for context in build_rule_section_contexts(grids)
    }
    current_contexts: dict[uuid.UUID, dict[str, Any]] = {}
    for index, attempt in enumerate(attempts, start=1):
        current_contexts[attempt.id] = verified_current_context(
            attempt=attempt,
            grids=grids,
            current_contexts=source_contexts,
        )
        if progress and (index == len(attempts) or index % 8 == 0):
            await progress(
                "verifying_sections",
                index,
                len(attempts),
                12 + round(28 * index / len(attempts)),
                "Проверяем привязку секций к исходному файлу",
            )
    tabular_attempts = [
        attempt
        for attempt in attempts
        if current_contexts[attempt.id].get("proposal_contract")
        != MATRIX_PROPOSAL_CONTRACT
    ]
    matrix_attempts = [
        attempt
        for attempt in attempts
        if current_contexts[attempt.id].get("proposal_contract")
        == MATRIX_PROPOSAL_CONTRACT
    ]
    if progress:
        await progress(
            "combining_rule", len(attempts), len(attempts), 52,
            "Объединяем обычные таблицы и специальные форматы в одно правило",
        )
    decision = (
        compile_batch_proposals(
            (
                (attempt.proposal, current_contexts[attempt.id])
                for attempt in tabular_attempts
            ),
            loader=loader,
            grids=grids,
        )
        if tabular_attempts
        else MappingDecision(auto_ok=True)
    )
    if matrix_attempts:
        tabular_recipe = recipe_from_decision(decision, grids)
        matrix_regions = [
            handler_for_contract(MATRIX_PROPOSAL_CONTRACT).compile(
                handler_for_contract(MATRIX_PROPOSAL_CONTRACT).parse(
                    attempt.proposal
                ),
                source_context=current_contexts[attempt.id],
                grids=grids,
            )
            for attempt in matrix_attempts
        ]
        recipe = {
            "format": "workbook_layout_v1",
            "profile": "generated_matrix_v1",
            "sheets": tabular_recipe.get("sheets", []),
            "options": tabular_recipe.get("options", {}),
            "matrix_regions": matrix_regions,
        }
        if progress:
            await progress(
                "dry_run", len(attempts), len(attempts), 72,
                "Проверяем готовое правило на всём исходном прайсе",
            )
        parsed = parse_workbook_layout(grids, recipe)
        quality = validation_metrics(parsed)
        validation = {
            "compiler_passed": True,
            "dry_run": quality,
            "item_count": len(parsed.items),
            "table_count": len(parsed.tables) + len(matrix_regions),
            "passed": bool(quality["passed"]),
        }
    else:
        if progress:
            await progress(
                "dry_run", len(attempts), len(attempts), 72,
                "Проверяем готовое правило на всём исходном прайсе",
            )
        recipe = recipe_from_decision(decision, grids)
        parsed, validation = dry_run_decision(grids, decision)
    if not validation["passed"]:
        raise ProposalError("Объединённое правило не прошло dry-run / quality gate")
    samples = build_validation_samples(parsed)
    fingerprint = compute_fingerprint(loader, grids)
    header_hash = compute_header_hash(fingerprint)
    if progress:
        await progress(
            "saving_draft", len(attempts), len(attempts), 92,
            "Сохраняем черновик и результаты проверки",
        )
    name = draft_rule_name(price_file.original_filename)
    rule = await rules_service.create_model_draft_rule(
        session,
        project_id=price_file.project_id,
        supplier_id=version.supplier_id,
        name=name[:512],
        loader=loader,
        header_hash=header_hash,
        fingerprint=fingerprint,
        recipe=recipe,
        created_from_version_id=version.id,
        validation={
            "at": _now().isoformat(),
            "price_version_id": str(version.id),
            "model_batch_id": str(batch_id),
            "metrics": validation["dry_run"]["metrics"],
            "samples": samples,
            **validation,
        },
    )
    for attempt in attempts:
        attempt.status = ATTEMPT_DRAFT_CREATED
        attempt.parse_rule_id = rule.id
        attempt.validation = {**(attempt.validation or {}), "batch_dry_run": validation}
        attempt.updated_at = _now()
    await session.flush()
    return rule
    build_tabular_rule_repair_prompt,
