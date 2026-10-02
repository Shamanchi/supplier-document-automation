"""Transactional persistence for paid rule-model operations.

This module deliberately does not know how to compile a proposal.  It only
reserves one logical model operation, durably records its safe response, and
attaches an already locally verified artifact by an exact work key.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Mapping
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    RuleGenerationArtifact,
    RuleGenerationAttempt,
    RuleGenerationOperation,
)
from app.rule_generation_artifacts import (
    compute_request_settings_hash,
    compute_rule_generation_work_key,
    create_verified_artifact,
    find_verified_artifact,
)


OPERATION_STARTED = "started"
OPERATION_COMPLETED = "completed"
OPERATION_FAILED = "failed"
OPERATION_INITIAL = "initial"
OPERATION_SEMANTIC_REPAIR = "semantic_repair"
_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "connection_string",
    "database_url",
    "headers",
    "iam_token",
    "password",
    "secret",
    "access_token",
}


class DurableOperationInProgress(RuntimeError):
    """Another worker has reserved the same logical paid operation."""


class DurableOperationFailed(RuntimeError):
    """The logical operation is already terminal without a usable response."""


@dataclass(frozen=True)
class WorkIdentity:
    work_key: str
    request_settings_hash: str
    request_settings: dict[str, Any]
    contract_version: str
    provider: str
    model: str


@dataclass(frozen=True)
class StoredModelResult:
    payload: dict[str, Any]
    usage: dict[str, int | float]
    telemetry: dict[str, Any]


@dataclass(frozen=True)
class OperationReservation:
    operation_id: uuid.UUID
    should_call_provider: bool
    stored_result: StoredModelResult | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _safe_json(value: Any, *, secrets: tuple[str, ...] = ()) -> Any:
    """Return a JSON-safe copy with credentials and known secrets redacted."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name.casefold() in _SENSITIVE_KEYS:
                result[name] = "[REDACTED]"
            else:
                result[name] = _safe_json(item, secrets=secrets)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_json(item, secrets=secrets) for item in value]
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[REDACTED]")
        return result
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _bounded_safe_json(value: Any, *, secrets: tuple[str, ...] = ()) -> Any:
    safe = _safe_json(value, secrets=secrets)
    encoded = json.dumps(safe, ensure_ascii=False, default=str)
    if len(encoded) <= 64_000:
        return safe
    return {"truncated": True, "preview": encoded[:63_000]}


def _parse_retry_after(value: object) -> datetime | None:
    parsed = _parse_datetime(value)
    if parsed is not None:
        return parsed
    if isinstance(value, str) and value.strip().isdigit():
        return _now() + timedelta(seconds=int(value.strip()))
    return None


def redact_sensitive_text(value: object, *, secrets: tuple[str, ...] = ()) -> str:
    return str(_safe_json(str(value), secrets=secrets))


def build_work_identity(
    attempt: RuleGenerationAttempt,
    *,
    provider: str,
    model: str,
    request_settings: Mapping[str, Any],
    contract_version: str,
) -> WorkIdentity | None:
    """Build an exact identity only when every required component is present."""

    required = (
        attempt.source_file_sha256,
        attempt.source_context_hash,
        attempt.prompt_version,
        provider,
        model,
        contract_version,
    )
    if any(not isinstance(value, str) or not value.strip() for value in required):
        return None
    if not isinstance(request_settings, Mapping):
        return None
    canonical_settings = dict(request_settings)
    return WorkIdentity(
        work_key=compute_rule_generation_work_key(
            source_file_sha256=attempt.source_file_sha256,
            source_context_hash=attempt.source_context_hash,
            prompt_version=attempt.prompt_version,
            provider=provider,
            model=model,
            request_settings=canonical_settings,
            contract_version=contract_version,
        ),
        request_settings_hash=compute_request_settings_hash(
            canonical_settings,
            contract_version=contract_version,
        ),
        request_settings=canonical_settings,
        contract_version=contract_version,
        provider=provider,
        model=model,
    )


def _stored_result(operation: RuleGenerationOperation) -> StoredModelResult | None:
    response = operation.response_payload
    if operation.status != OPERATION_COMPLETED or not isinstance(response, dict):
        return None
    payload = response.get("payload")
    if not isinstance(payload, dict):
        return None
    telemetry = response.get("telemetry")
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    usage = operation.usage if isinstance(operation.usage, dict) else {}
    return StoredModelResult(
        payload=dict(payload),
        usage=dict(usage),
        telemetry=dict(telemetry),
    )


async def _operation_for_number(
    session: AsyncSession, *, attempt_id: uuid.UUID, operation_no: int
) -> RuleGenerationOperation | None:
    return await session.scalar(
        select(RuleGenerationOperation).where(
            RuleGenerationOperation.attempt_id == attempt_id,
            RuleGenerationOperation.operation_no == operation_no,
        )
    )


async def reserve_operation(
    session: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    operation_no: int,
    operation_kind: str,
    provider: str,
    model: str,
    allow_recovery: bool = False,
) -> OperationReservation:
    """Reserve one deterministic operation number and commit before HTTP."""

    if operation_no < 1:
        raise ValueError("operation_no must be positive")
    same_kind = list(
        (
            await session.execute(
                select(RuleGenerationOperation)
                .where(
                    RuleGenerationOperation.attempt_id == attempt_id,
                    RuleGenerationOperation.operation_kind == operation_kind,
                )
                .order_by(RuleGenerationOperation.operation_no.desc())
            )
        ).scalars()
    )
    for candidate in same_kind:
        result = _stored_result(candidate)
        if result is not None:
            await session.commit()
            return OperationReservation(candidate.id, False, result)
    existing = same_kind[0] if same_kind else await _operation_for_number(
        session, attempt_id=attempt_id, operation_no=operation_no
    )
    if existing is not None:
        if existing.status == OPERATION_STARTED:
            if allow_recovery:
                existing.started_at = _now()
                existing.finished_at = None
                existing.error_type = None
                existing.http_status = None
                existing.retry_after = None
                existing.response_payload = None
                await session.commit()
                return OperationReservation(existing.id, True)
            await session.commit()
            raise DurableOperationInProgress(
                f"operation {operation_no} is already in progress"
            )
        if not allow_recovery:
            await session.commit()
            raise DurableOperationFailed(
                f"operation {operation_no} is already {existing.status}"
            )
        maximum = await session.scalar(
            select(RuleGenerationOperation.operation_no)
            .where(RuleGenerationOperation.attempt_id == attempt_id)
            .order_by(RuleGenerationOperation.operation_no.desc())
            .limit(1)
        )
        operation_no = int(maximum or 0) + 1

    operation = RuleGenerationOperation(
        attempt_id=attempt_id,
        operation_no=operation_no,
        operation_kind=operation_kind,
        status=OPERATION_STARTED,
        provider=provider,
        model=model,
        started_at=_now(),
    )
    try:
        async with session.begin_nested():
            session.add(operation)
            await session.flush()
    except IntegrityError:
        winner = await _operation_for_number(
            session, attempt_id=attempt_id, operation_no=operation_no
        )
        await session.commit()
        if winner is None:  # pragma: no cover - preserve the original DB error
            raise
        result = _stored_result(winner)
        if result is not None:
            return OperationReservation(winner.id, False, result)
        if winner.status == OPERATION_STARTED:
            raise DurableOperationInProgress(
                f"operation {operation_no} is already in progress"
            )
        raise DurableOperationFailed(
            f"operation {operation_no} is already {winner.status}"
        )
    await session.commit()
    return OperationReservation(operation.id, True)


async def complete_operation(
    session: AsyncSession,
    *,
    operation_id: uuid.UUID,
    payload: dict[str, Any],
    usage: Mapping[str, int | float],
    telemetry: Mapping[str, Any],
    secrets: tuple[str, ...] = (),
) -> StoredModelResult:
    """Persist a paid response and commit before any local verification."""

    operation = await session.get(RuleGenerationOperation, operation_id)
    if operation is None:
        raise LookupError("reserved operation was not found")
    safe_payload = _bounded_safe_json(payload, secrets=secrets)
    safe_usage = _safe_json(dict(usage), secrets=secrets)
    safe_telemetry = _safe_json(dict(telemetry), secrets=secrets)
    operation.status = OPERATION_COMPLETED
    operation.provider_operation_id = (
        str(safe_telemetry["response_id"])[:255]
        if safe_telemetry.get("response_id") is not None
        else None
    )
    operation.finished_at = _parse_datetime(safe_telemetry.get("finished_at")) or _now()
    operation.http_attempts = int(safe_telemetry.get("http_attempts") or 0)
    operation.usage = safe_usage
    operation.error_type = None
    status = safe_telemetry.get("http_status_code")
    operation.http_status = int(status) if isinstance(status, int) else None
    operation.retry_after = _parse_retry_after(safe_telemetry.get("retry_after"))
    operation.response_payload = {
        "payload": safe_payload,
        "telemetry": safe_telemetry,
    }
    await session.commit()
    return StoredModelResult(
        payload=dict(safe_payload),
        usage=dict(safe_usage),
        telemetry=dict(safe_telemetry),
    )


async def fail_operation(
    session: AsyncSession,
    *,
    operation_id: uuid.UUID,
    telemetry: Mapping[str, Any] | None,
    response_payload: object = None,
    secrets: tuple[str, ...] = (),
) -> None:
    """Persist a terminal provider failure without introducing retry policy."""

    operation = await session.get(RuleGenerationOperation, operation_id)
    if operation is None:
        raise LookupError("reserved operation was not found")
    safe = _safe_json(dict(telemetry or {}), secrets=secrets)
    operation.status = OPERATION_FAILED
    operation.finished_at = _parse_datetime(safe.get("finished_at")) or _now()
    operation.http_attempts = int(safe.get("http_attempts") or 0)
    operation.error_type = str(safe.get("error_type") or "provider_error")[:64]
    status = safe.get("http_status_code")
    operation.http_status = int(status) if isinstance(status, int) else None
    operation.retry_after = _parse_retry_after(safe.get("retry_after"))
    response_id = safe.get("response_id")
    operation.provider_operation_id = str(response_id)[:255] if response_id else None
    usage = safe.get("usage")
    operation.usage = dict(usage) if isinstance(usage, dict) else None
    safe_response = _bounded_safe_json(response_payload, secrets=secrets)
    operation.response_payload = (
        {"telemetry": safe, "provider_response": safe_response}
        if safe or response_payload is not None
        else None
    )
    await session.commit()


async def attach_exact_artifact(
    session: AsyncSession,
    *,
    attempt: RuleGenerationAttempt,
    identity: WorkIdentity,
    preserve_attempt_status: bool = False,
) -> RuleGenerationArtifact | None:
    """Attach and rehydrate only one exact verified artifact."""

    artifact = await find_verified_artifact(session, identity.work_key)
    if artifact is None:
        attempt.work_key = identity.work_key
        await session.commit()
        return None
    status = artifact.compiler_dry_run_summary.get("attempt_status")
    if not isinstance(status, str) or not status:
        # Stage-1 artifacts predate runtime attachment and therefore have no
        # attempt status.  They are nevertheless locally verified; the only
        # successful priced-section status in the legacy flow is ``proposed``.
        status = "proposed"
    attempt.work_key = identity.work_key
    attempt.artifact_id = artifact.id
    attempt.proposal = dict(artifact.proposal)
    attempt.validation = dict(artifact.validation)
    if not preserve_attempt_status:
        attempt.status = status
    attempt.error_message = None
    attempt.updated_at = _now()
    await session.commit()
    return artifact


async def create_and_attach_verified_artifact(
    session: AsyncSession,
    *,
    attempt: RuleGenerationAttempt,
    identity: WorkIdentity,
    proposal: dict[str, Any],
    validation: dict[str, Any],
    compiler_dry_run_summary: dict[str, Any],
    provenance: str = "model_verified",
) -> RuleGenerationArtifact:
    """Create one immutable verified artifact and attach it to the attempt."""

    artifact = await create_verified_artifact(
        session,
        work_key=identity.work_key,
        provider=identity.provider,
        model=identity.model,
        request_settings_hash=identity.request_settings_hash,
        source_file_sha256=attempt.source_file_sha256,
        source_context_hash=attempt.source_context_hash,
        prompt_version=attempt.prompt_version,
        proposal=proposal,
        validation=validation,
        compiler_dry_run_summary=compiler_dry_run_summary,
        provenance=provenance,
    )
    attempt.work_key = identity.work_key
    attempt.artifact_id = artifact.id
    await session.commit()
    return artifact
