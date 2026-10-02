"""PostgreSQL-backed lifecycle primitives for independent rule sections.

Celery is delivery only.  Every eligibility, ownership, retry and completion
decision is derived from ``rule_generation_attempts``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable
import uuid

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RuleGenerationAttempt


ATTEMPT_QUEUED = "queued"
ATTEMPT_LEASED = "leased"
ATTEMPT_SUCCEEDED = "succeeded"
ATTEMPT_RETRYABLE_FAILED = "retryable_failed"
ATTEMPT_NEEDS_REVIEW = "needs_review"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ClaimResult:
    claimed: bool
    status: str
    reason: str


@dataclass(frozen=True)
class BatchState:
    ready: bool
    pending_count: int
    review_count: int
    succeeded_count: int
    total_count: int


@dataclass(frozen=True)
class ManualRetryResult:
    changed: bool
    status: str
    reason: str


def _eligible_for_claim(now: datetime):
    return or_(
        RuleGenerationAttempt.status == ATTEMPT_QUEUED,
        and_(
            RuleGenerationAttempt.status == ATTEMPT_RETRYABLE_FAILED,
            RuleGenerationAttempt.next_retry_at.is_not(None),
            RuleGenerationAttempt.next_retry_at <= now,
        ),
        and_(
            RuleGenerationAttempt.status == ATTEMPT_LEASED,
            RuleGenerationAttempt.lease_until.is_not(None),
            RuleGenerationAttempt.lease_until < now,
        ),
    )


def _stale_recovery_values(now: datetime) -> dict:
    return {
        "status": ATTEMPT_QUEUED,
        "lease_owner": None,
        "lease_until": None,
        "updated_at": now,
    }


async def claim_attempt(
    session: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    lease_owner: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> ClaimResult:
    """Atomically acquire one due section; only one delivery can win."""

    current = now or utcnow()
    result = await session.execute(
        update(RuleGenerationAttempt)
        .where(RuleGenerationAttempt.id == attempt_id, _eligible_for_claim(current))
        .values(
            status=ATTEMPT_LEASED,
            lease_owner=lease_owner[:64],
            lease_until=current + timedelta(seconds=max(1, lease_seconds)),
            last_heartbeat_at=current,
            next_retry_at=None,
            error_message=None,
            updated_at=current,
        )
    )
    if result.rowcount == 1:
        await session.commit()
        return ClaimResult(True, ATTEMPT_LEASED, "claimed")
    await session.rollback()
    attempt = await session.get(RuleGenerationAttempt, attempt_id)
    if attempt is None:
        return ClaimResult(False, "not_found", "not_found")
    active = (
        attempt.status == ATTEMPT_LEASED
        and attempt.lease_until is not None
        and _aware(attempt.lease_until) >= current
    )
    return ClaimResult(False, attempt.status, "active_lease" if active else "not_eligible")


async def heartbeat_attempt(
    session: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    lease_owner: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> bool:
    current = now or utcnow()
    result = await session.execute(
        update(RuleGenerationAttempt)
        .where(
            RuleGenerationAttempt.id == attempt_id,
            RuleGenerationAttempt.status == ATTEMPT_LEASED,
            RuleGenerationAttempt.lease_owner == lease_owner,
        )
        .values(
            last_heartbeat_at=current,
            lease_until=current + timedelta(seconds=max(1, lease_seconds)),
            updated_at=current,
        )
    )
    await session.commit()
    return result.rowcount == 1


async def recover_stale_attempts(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 100
) -> list[uuid.UUID]:
    current = now or utcnow()
    stmt = (
        select(RuleGenerationAttempt.id)
        .where(
            RuleGenerationAttempt.status == ATTEMPT_LEASED,
            RuleGenerationAttempt.lease_until.is_not(None),
            RuleGenerationAttempt.lease_until < current,
        )
        .order_by(RuleGenerationAttempt.lease_until, RuleGenerationAttempt.id)
        .limit(max(1, limit))
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    ids = list((await session.execute(stmt)).scalars())
    if ids:
        await session.execute(
            update(RuleGenerationAttempt)
            .where(
                RuleGenerationAttempt.id.in_(ids),
                RuleGenerationAttempt.status == ATTEMPT_LEASED,
                RuleGenerationAttempt.lease_until < current,
            )
            .values(**_stale_recovery_values(current))
        )
    await session.commit()
    return ids


async def request_manual_retry(
    session: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    retryable_only: bool,
    explicitly_selected: bool,
    actor: str,
    now: datetime | None = None,
) -> ManualRetryResult:
    """Atomically make one eligible section dispatchable without doing work.

    Failure metadata and append-only operations are deliberately preserved.
    The compact validation entry records the administrative transition without
    introducing a second audit table or changing retry counter semantics.
    """

    current = now or utcnow()
    stmt = select(RuleGenerationAttempt).where(RuleGenerationAttempt.id == attempt_id)
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update()
    attempt = (await session.execute(stmt)).scalar_one_or_none()
    if attempt is None:
        return ManualRetryResult(False, "not_found", "not_found")
    previous_status = attempt.status
    if attempt.artifact_id is not None or attempt.status in {
        ATTEMPT_SUCCEEDED,
        "proposed",
        "catalog_without_price",
        "skipped_non_price",
        "draft_created",
    }:
        return ManualRetryResult(False, attempt.status, "skipped_already_succeeded")
    if attempt.status == ATTEMPT_LEASED:
        active = attempt.lease_until is not None and _aware(attempt.lease_until) >= current
        if active:
            return ManualRetryResult(False, attempt.status, "already_running")
        # Same durable recovery transition as recover_stale_attempts, scoped to
        # the explicitly selected row and protected by the row lock.
        for field, value in _stale_recovery_values(current).items():
            setattr(attempt, field, value)
    elif attempt.status == ATTEMPT_QUEUED:
        return ManualRetryResult(False, attempt.status, "already_queued")
    elif attempt.status == ATTEMPT_RETRYABLE_FAILED:
        pass
    elif (
        attempt.status == ATTEMPT_NEEDS_REVIEW
        and explicitly_selected
        and not retryable_only
    ):
        pass
    else:
        return ManualRetryResult(False, attempt.status, "skipped_not_retryable")

    validation = dict(attempt.validation or {})
    history = list(validation.get("manual_retry_history") or [])[-19:]
    history.append(
        {
            "requested_at": current.isoformat(),
            "requested_by": actor[:128],
            "from_status": previous_status,
            "failure_class": attempt.failure_class,
            "terminal_reason": attempt.terminal_reason,
        }
    )
    validation["manual_retry_history"] = history
    attempt.validation = validation
    attempt.status = ATTEMPT_QUEUED
    attempt.next_retry_at = None
    attempt.lease_owner = None
    attempt.lease_until = None
    attempt.updated_at = current
    await session.flush()
    return ManualRetryResult(True, ATTEMPT_QUEUED, "queued")


async def reserve_dispatchable_attempts(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 100,
    publication_lease_seconds: int = 60,
) -> list[uuid.UUID]:
    """Reserve DB work for publication without doing any section work."""

    current = now or utcnow()
    due = or_(
        RuleGenerationAttempt.status == ATTEMPT_QUEUED,
        and_(
            RuleGenerationAttempt.status == ATTEMPT_RETRYABLE_FAILED,
            RuleGenerationAttempt.next_retry_at.is_not(None),
            RuleGenerationAttempt.next_retry_at <= current,
        ),
    )
    stmt = (
        select(RuleGenerationAttempt.id)
        .where(
            due,
            or_(
                RuleGenerationAttempt.lease_until.is_(None),
                RuleGenerationAttempt.lease_until < current,
            ),
        )
        .order_by(RuleGenerationAttempt.created_at, RuleGenerationAttempt.id)
        .limit(max(1, limit))
    )
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    ids = list((await session.execute(stmt)).scalars())
    if ids:
        await session.execute(
            update(RuleGenerationAttempt)
            .where(RuleGenerationAttempt.id.in_(ids), due)
            .values(
                lease_until=current + timedelta(seconds=max(1, publication_lease_seconds)),
                updated_at=current,
            )
        )
    await session.commit()
    return ids


async def mark_section_succeeded(
    session: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    lease_owner: str,
    status: str = ATTEMPT_SUCCEEDED,
    now: datetime | None = None,
) -> bool:
    current = now or utcnow()
    result = await session.execute(
        update(RuleGenerationAttempt)
        .where(
            RuleGenerationAttempt.id == attempt_id,
            RuleGenerationAttempt.status.in_(
                [
                    ATTEMPT_LEASED,
                    "proposed",
                    "catalog_without_price",
                    "skipped_non_price",
                ]
            ),
            RuleGenerationAttempt.lease_owner == lease_owner,
        )
        .values(
            status=status,
            lease_owner=None,
            lease_until=None,
            next_retry_at=None,
            failure_class=None,
            terminal_reason=None,
            error_message=None,
            updated_at=current,
        )
    )
    await session.commit()
    return result.rowcount == 1


async def record_section_failure(
    session: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    lease_owner: str,
    failure_class: str,
    terminal_reason: str,
    retry_delays: Iterable[int],
    now: datetime | None = None,
) -> str:
    current = now or utcnow()
    attempt = await session.get(RuleGenerationAttempt, attempt_id)
    if (
        attempt is None
        or attempt.status
        not in {
            ATTEMPT_LEASED,
            "failed",
            "rejected",
            "insufficient_evidence",
        }
        or attempt.lease_owner != lease_owner
    ):
        return attempt.status if attempt is not None else "not_found"
    delays = tuple(max(1, int(value)) for value in retry_delays)
    if failure_class == "transport" and attempt.retry_count < len(delays):
        delay = delays[attempt.retry_count]
        attempt.retry_count += 1
        attempt.status = ATTEMPT_RETRYABLE_FAILED
        attempt.next_retry_at = current + timedelta(seconds=delay)
    else:
        attempt.status = ATTEMPT_NEEDS_REVIEW
        attempt.next_retry_at = None
    attempt.failure_class = failure_class[:32]
    attempt.terminal_reason = terminal_reason[:64]
    attempt.lease_owner = None
    attempt.lease_until = None
    attempt.updated_at = current
    await session.commit()
    return attempt.status


async def inspect_batch(session: AsyncSession, *, batch_id: uuid.UUID) -> BatchState:
    attempts = list(
        (
            await session.execute(
                select(RuleGenerationAttempt).where(
                    RuleGenerationAttempt.batch_id == batch_id
                )
            )
        ).scalars()
    )
    pending = sum(
        attempt.status
        in {ATTEMPT_QUEUED, ATTEMPT_LEASED, ATTEMPT_RETRYABLE_FAILED}
        for attempt in attempts
    )
    review = sum(attempt.status == ATTEMPT_NEEDS_REVIEW for attempt in attempts)
    succeeded = sum(
        attempt.status
        in {
            ATTEMPT_SUCCEEDED,
            "catalog_without_price",
            "skipped_non_price",
            "draft_created",
        }
        for attempt in attempts
    )
    return BatchState(
        ready=bool(attempts) and pending == 0 and review == 0 and succeeded == len(attempts),
        pending_count=pending,
        review_count=review,
        succeeded_count=succeeded,
        total_count=len(attempts),
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
