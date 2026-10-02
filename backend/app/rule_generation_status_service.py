"""Safe Stage-4 status and selective retry facade for durable rule sections."""
from __future__ import annotations

from datetime import datetime, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.import_service import transition
from app.models import (
    PriceImport,
    PriceImportStatus,
    RuleGenerationArtifact,
    RuleGenerationAttempt,
    RuleGenerationOperation,
)
from app.rule_generation_lifecycle import (
    ATTEMPT_LEASED,
    ATTEMPT_NEEDS_REVIEW,
    ATTEMPT_QUEUED,
    ATTEMPT_RETRYABLE_FAILED,
    request_manual_retry,
)
from app.schemas import (
    RuleGenerationSummaryOut,
    RuleSectionOut,
    RuleSectionRetryItemOut,
    RuleSectionRetryOut,
)


SUCCESS_STATUSES = {
    "succeeded", "proposed", "catalog_without_price", "skipped_non_price", "draft_created"
}
PERMANENT_FAILURE_STATUSES = {"failed", "rejected", "insufficient_evidence"}


class RetryScopeError(ValueError):
    pass


class RetryUnavailableError(RuntimeError):
    pass


class RetryImportStateError(RuntimeError):
    pass


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _latest(values: list[datetime | None]) -> datetime | None:
    present = [_aware(value) for value in values if value is not None]
    return max(present) if present else None


async def _attempts_for_scope(
    session: AsyncSession,
    *,
    price_import_id: uuid.UUID | None = None,
    price_version_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
) -> list[RuleGenerationAttempt]:
    stmt = select(RuleGenerationAttempt)
    if price_import_id is not None:
        stmt = stmt.where(RuleGenerationAttempt.price_import_id == price_import_id)
    if price_version_id is not None:
        stmt = stmt.where(RuleGenerationAttempt.price_version_id == price_version_id)
    if batch_id is not None:
        stmt = stmt.where(RuleGenerationAttempt.batch_id == batch_id)
    attempts = list((await session.execute(stmt.order_by(
        RuleGenerationAttempt.updated_at.desc(), RuleGenerationAttempt.created_at.desc()
    ))).scalars())
    if attempts and batch_id is None:
        current_batch = attempts[0].batch_id
        attempts = [attempt for attempt in attempts if attempt.batch_id == current_batch]
    return attempts


async def _activity_maps(session: AsyncSession, attempts: list[RuleGenerationAttempt]):
    attempt_ids = [attempt.id for attempt in attempts]
    artifact_ids = [attempt.artifact_id for attempt in attempts if attempt.artifact_id]
    operations = list((await session.execute(
        select(RuleGenerationOperation).where(RuleGenerationOperation.attempt_id.in_(attempt_ids))
    )).scalars()) if attempt_ids else []
    artifacts = list((await session.execute(
        select(RuleGenerationArtifact).where(RuleGenerationArtifact.id.in_(artifact_ids))
    )).scalars()) if artifact_ids else []
    by_attempt: dict[uuid.UUID, list[RuleGenerationOperation]] = {}
    for operation in operations:
        by_attempt.setdefault(operation.attempt_id, []).append(operation)
    by_artifact = {artifact.id: artifact for artifact in artifacts}
    return by_attempt, by_artifact


def _attempt_activity(attempt, operations, artifact) -> datetime | None:
    values = [attempt.created_at, attempt.updated_at, attempt.last_heartbeat_at]
    for operation in operations:
        values.extend([operation.created_at, operation.started_at, operation.finished_at])
    if artifact is not None:
        values.extend([artifact.created_at, artifact.updated_at])
    return _latest(values)


def _sheet_name(attempt: RuleGenerationAttempt) -> str | None:
    context = attempt.input_context if isinstance(attempt.input_context, dict) else {}
    direct = context.get("sheet_name")
    if isinstance(direct, str):
        return direct
    sheet = context.get("sheet")
    if isinstance(sheet, dict) and isinstance(sheet.get("name"), str):
        return sheet["name"]
    return None


async def get_rule_generation_summary(
    session: AsyncSession, *, price_import_id: uuid.UUID,
    batch_id: uuid.UUID | None = None, now: datetime | None = None
) -> RuleGenerationSummaryOut | None:
    current = now or datetime.now(timezone.utc)
    attempts = await _attempts_for_scope(
        session, price_import_id=price_import_id, batch_id=batch_id
    )
    if not attempts:
        return None
    operations, artifacts = await _activity_maps(session, attempts)
    counts = dict(queued=0, running=0, succeeded=0, retryable_failed=0, needs_review=0, permanent_failed=0)
    activities: list[datetime | None] = []
    for attempt in attempts:
        if attempt.status == ATTEMPT_LEASED:
            if attempt.lease_until is not None and _aware(attempt.lease_until) > current:
                counts["running"] += 1
            else:
                counts["queued"] += 1
        elif attempt.status == ATTEMPT_QUEUED:
            counts["queued"] += 1
        elif attempt.status in SUCCESS_STATUSES:
            counts["succeeded"] += 1
        elif attempt.status == ATTEMPT_RETRYABLE_FAILED:
            counts["retryable_failed"] += 1
        elif attempt.status == ATTEMPT_NEEDS_REVIEW:
            counts["needs_review"] += 1
        elif attempt.status in PERMANENT_FAILURE_STATUSES:
            counts["permanent_failed"] += 1
        else:
            counts["permanent_failed"] += 1
        activities.append(_attempt_activity(
            attempt, operations.get(attempt.id, []), artifacts.get(attempt.artifact_id)
        ))
    future_retries = [
        _aware(attempt.next_retry_at) for attempt in attempts
        if attempt.next_retry_at is not None and _aware(attempt.next_retry_at) > current
    ]
    return RuleGenerationSummaryOut(
        batch_id=attempts[0].batch_id,
        total=len(attempts),
        **counts,
        reused=sum(attempt.reused_from_attempt_id is not None for attempt in attempts),
        http_attempts=sum(int(operation.http_attempts or 0) for rows in operations.values() for operation in rows),
        semantic_retries=sum(int(attempt.semantic_retry_count or 0) for attempt in attempts),
        next_retry_at=min(future_retries) if future_retries else None,
        last_activity_at=_latest(activities),
    )


async def list_rule_sections(
    session: AsyncSession, *, price_import_id: uuid.UUID,
    batch_id: uuid.UUID | None = None, now: datetime | None = None
) -> list[RuleSectionOut]:
    attempts = await _attempts_for_scope(
        session, price_import_id=price_import_id, batch_id=batch_id
    )
    operations, artifacts = await _activity_maps(session, attempts)
    return [
        RuleSectionOut(
            section_id=attempt.section_id,
            sheet_name=_sheet_name(attempt),
            status=attempt.status,
            failure_class=attempt.failure_class,
            terminal_reason=attempt.terminal_reason,
            retry_count=attempt.retry_count,
            semantic_retry_count=attempt.semantic_retry_count,
            next_retry_at=attempt.next_retry_at,
            artifact_id=attempt.artifact_id,
            provenance=(
                artifacts[attempt.artifact_id].provenance
                if attempt.artifact_id in artifacts
                else None
            ),
            reused=attempt.reused_from_attempt_id is not None,
            last_activity_at=_attempt_activity(
                attempt, operations.get(attempt.id, []), artifacts.get(attempt.artifact_id)
            ),
        )
        for attempt in sorted(attempts, key=lambda item: item.section_id)
    ]


async def retry_rule_sections(
    session: AsyncSession,
    *,
    price_import_id: uuid.UUID | None = None,
    price_version_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    section_ids: list[str] | None,
    retryable_only: bool,
    actor: str,
    now: datetime | None = None,
) -> RuleSectionRetryOut:
    if not settings.durable_rule_generation_enabled:
        raise RetryUnavailableError("durable rule generation is disabled")
    current = now or datetime.now(timezone.utc)
    attempts = await _attempts_for_scope(
        session, price_import_id=price_import_id, price_version_id=price_version_id, batch_id=batch_id
    )
    if not attempts:
        raise RetryScopeError("rule generation batch not found")
    resolved_batch_id = attempts[0].batch_id
    explicit = section_ids is not None
    selected = list(dict.fromkeys(section_ids)) if explicit else []
    by_section = {attempt.section_id: attempt for attempt in attempts}
    if explicit:
        missing = [section_id for section_id in selected if section_id not in by_section]
        if missing:
            raise RetryScopeError(f"sections do not belong to scope: {', '.join(missing)}")
        targets = [by_section[section_id] for section_id in selected]
    else:
        targets = [attempt for attempt in attempts if attempt.status == ATTEMPT_RETRYABLE_FAILED]

    linked_import_ids = {attempt.price_import_id for attempt in attempts if attempt.price_import_id}
    if price_import_id is not None:
        linked_import_ids.add(price_import_id)
    linked_imports = [
        price_import for import_id in linked_import_ids
        if (price_import := await session.get(PriceImport, import_id)) is not None
    ]
    for price_import in linked_imports:
        if (
            price_import.status in PriceImportStatus.terminal_values()
            and price_import.status != PriceImportStatus.review_required.value
        ):
            raise RetryImportStateError(f"import status {price_import.status} does not allow retry")

    results: list[RuleSectionRetryItemOut] = []
    queued = 0
    for attempt in targets:
        previous = attempt.status
        outcome = await request_manual_retry(
            session,
            attempt_id=attempt.id,
            retryable_only=retryable_only,
            explicitly_selected=explicit,
            actor=actor,
            now=current,
        )
        queued += int(outcome.changed)
        results.append(RuleSectionRetryItemOut(
            section_id=attempt.section_id,
            previous_status=previous,
            status=outcome.status,
            result=outcome.reason,
        ))
    if queued:
        for price_import in linked_imports:
            if price_import.status == PriceImportStatus.review_required.value:
                session.add(transition(
                    price_import,
                    PriceImportStatus.generating_rule.value,
                    event_type="rule_sections_retry_requested",
                    payload={"batch_id": str(attempts[0].batch_id), "queued": queued, "actor": actor[:128]},
                ))
                price_import.finished_at = None
        await session.commit()
    return RuleSectionRetryOut(batch_id=resolved_batch_id, queued=queued, results=results)
