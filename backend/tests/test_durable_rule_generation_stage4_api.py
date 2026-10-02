from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from functools import wraps
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, PriceImport, RuleGenerationArtifact, RuleGenerationAttempt
from app.rule_generation_status_service import (
    RetryScopeError,
    RetryImportStateError,
    RetryUnavailableError,
    get_rule_generation_summary,
    list_rule_sections,
    retry_rule_sections,
)


@pytest.fixture(autouse=True)
def _durable_enabled(monkeypatch):
    from app.rule_generation_status_service import settings

    monkeypatch.setattr(settings, "durable_rule_generation_enabled", True)


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


async def _database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stage4.sqlite'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, factory


def _attempt(import_id, version_id, batch_id, section_id, status):
    return RuleGenerationAttempt(
        price_import_id=import_id,
        price_version_id=version_id,
        batch_id=batch_id,
        section_id=section_id,
        status=status,
        provider="mock",
        model="fake",
        request_settings={},
        prompt_version="synthetic-v1",
        prompt_id=f"prompt-{section_id}",
        source_file_sha256="a" * 64,
        source_context_hash="b" * 64,
        input_context={"sheet_name": "Sheet 1"},
        prompt="never expose this prompt",
    )


@async_test
async def test_status_47_2_1_and_safe_section_details(tmp_path):
    engine, factory = await _database(tmp_path)
    import_id, version_id, batch_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    try:
        async with factory() as session:
            attempts = [
                _attempt(import_id, version_id, batch_id, f"ok-{index}", "succeeded")
                for index in range(47)
            ]
            attempts += [
                _attempt(import_id, version_id, batch_id, "failed-1", "retryable_failed"),
                _attempt(import_id, version_id, batch_id, "failed-2", "retryable_failed"),
                _attempt(import_id, version_id, batch_id, "review-1", "needs_review"),
            ]
            attempts[-1].failure_class = "semantic"
            attempts[-1].terminal_reason = "invalid_mapping"
            attempts[-1].semantic_retry_count = 1
            attempts[-2].next_retry_at = now + timedelta(minutes=5)
            session.add_all(attempts)
            await session.commit()

            summary = await get_rule_generation_summary(session, price_import_id=import_id, now=now)
            assert summary is not None
            assert (summary.total, summary.succeeded, summary.retryable_failed, summary.needs_review) == (50, 47, 2, 1)
            assert summary.next_retry_at == attempts[-2].next_retry_at
            details = await list_rule_sections(session, price_import_id=import_id, now=now)
            review = next(item for item in details if item.section_id == "review-1")
            assert review.failure_class == "semantic"
            assert review.semantic_retry_count == 1
            assert not hasattr(review, "prompt")
            assert not hasattr(review, "input_context")
    finally:
        await engine.dispose()


@async_test
async def test_retry_selected_all_review_success_lease_and_idempotency(tmp_path):
    engine, factory = await _database(tmp_path)
    import_id, version_id, batch_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    try:
        async with factory() as session:
            selected = _attempt(import_id, version_id, batch_id, "failed", "retryable_failed")
            other = _attempt(import_id, version_id, batch_id, "other", "retryable_failed")
            review = _attempt(import_id, version_id, batch_id, "review", "needs_review")
            succeeded = _attempt(import_id, version_id, batch_id, "done", "succeeded")
            active = _attempt(import_id, version_id, batch_id, "running", "leased")
            active.lease_owner = "worker"
            active.lease_until = now + timedelta(minutes=5)
            session.add_all([selected, other, review, succeeded, active])
            await session.commit()

            result = await retry_rule_sections(
                session, price_import_id=import_id, section_ids=["failed", "done", "running"],
                retryable_only=True, actor="admin", now=now,
            )
            assert {item.section_id: item.result for item in result.results} == {
                "failed": "queued", "done": "skipped_already_succeeded", "running": "already_running"
            }
            assert selected.status == "queued"
            assert other.status == "retryable_failed"
            duplicate = await retry_rule_sections(
                session, price_import_id=import_id, section_ids=["failed"],
                retryable_only=True, actor="admin", now=now,
            )
            assert duplicate.results[0].result == "already_queued"

            all_retryable = await retry_rule_sections(
                session, price_import_id=import_id, section_ids=None,
                retryable_only=True, actor="admin", now=now,
            )
            assert [item.section_id for item in all_retryable.results] == ["other"]
            explicit_review = await retry_rule_sections(
                session, price_import_id=import_id, section_ids=["review"],
                retryable_only=False, actor="admin", now=now,
            )
            assert explicit_review.results[0].result == "queued"
    finally:
        await engine.dispose()


@async_test
async def test_wrong_import_scope_is_atomic(tmp_path):
    engine, factory = await _database(tmp_path)
    import_id, other_import_id = uuid.uuid4(), uuid.uuid4()
    attempt = _attempt(other_import_id, uuid.uuid4(), uuid.uuid4(), "foreign", "retryable_failed")
    try:
        async with factory() as session:
            session.add(attempt)
            await session.commit()
            try:
                await retry_rule_sections(
                    session, price_import_id=import_id, section_ids=["foreign"],
                    retryable_only=True, actor="admin",
                )
            except RetryScopeError:
                pass
            else:
                raise AssertionError("foreign section must be rejected")
            await session.refresh(attempt)
            assert attempt.status == "retryable_failed"
    finally:
        await engine.dispose()


@async_test
async def test_reused_and_running_are_real_lifecycle_signals(tmp_path):
    engine, factory = await _database(tmp_path)
    import_id, version_id, batch_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    try:
        async with factory() as session:
            donor = _attempt(import_id, version_id, batch_id, "donor", "succeeded")
            donor.id = uuid.uuid4()
            reused = _attempt(import_id, version_id, batch_id, "reused", "succeeded")
            reused.reused_from_attempt_id = donor.id
            active = _attempt(import_id, version_id, batch_id, "active", "leased")
            active.lease_until = now + timedelta(minutes=1)
            stale = _attempt(import_id, version_id, batch_id, "stale", "leased")
            stale.lease_until = now - timedelta(minutes=1)
            session.add_all([donor, reused, active, stale])
            await session.commit()
            summary = await get_rule_generation_summary(session, price_import_id=import_id, now=now)
            assert summary.reused == 1
            assert summary.running == 1
            assert summary.queued == 1
    finally:
        await engine.dispose()


@async_test
async def test_stale_lease_uses_recovery_transition_and_flag_false_is_safe(tmp_path, monkeypatch):
    from app.rule_generation_status_service import settings

    engine, factory = await _database(tmp_path)
    import_id, version_id, batch_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    try:
        async with factory() as session:
            stale = _attempt(import_id, version_id, batch_id, "stale", "leased")
            stale.lease_owner = "lost-worker"
            stale.lease_until = now - timedelta(seconds=1)
            session.add(stale)
            await session.commit()
            result = await retry_rule_sections(
                session, price_import_id=import_id, section_ids=["stale"],
                retryable_only=True, actor="admin", now=now,
            )
            assert result.results[0].result == "queued"
            assert stale.lease_owner is None and stale.lease_until is None
            assert stale.validation["manual_retry_history"][-1]["from_status"] == "leased"

            stale.status = "retryable_failed"
            await session.commit()
            monkeypatch.setattr(settings, "durable_rule_generation_enabled", False)
            try:
                await retry_rule_sections(
                    session, price_import_id=import_id, section_ids=["stale"],
                    retryable_only=True, actor="admin", now=now,
                )
            except RetryUnavailableError:
                pass
            else:
                raise AssertionError("disabled durable endpoint must not change state")
            assert stale.status == "retryable_failed"
    finally:
        await engine.dispose()


def test_retry_routes_use_existing_admin_boundaries() -> None:
    from app.auth import require_admin
    from app.api import price_imports, rule_proposals
    from app.import_auth import get_import_admin

    routes = {
        route.path: route
        for route in [*price_imports.router.routes, *rule_proposals.router.routes]
        if hasattr(route, "dependant")
    }
    import_calls = {
        dependency.call
        for dependency in routes["/price-imports/{import_id}/rule-sections/retry"].dependant.dependencies
    }
    version_calls = {
        dependency.call
        for dependency in routes[
            "/price-versions/{version_id}/rule-proposals/{batch_id}/retry"
        ].dependant.dependencies
    }
    assert get_import_admin in import_calls
    assert require_admin in version_calls


@async_test
async def test_full_resume_keeps_47_successes_and_queues_only_three_problems(tmp_path):
    from app.rule_generation_lifecycle import inspect_batch

    engine, factory = await _database(tmp_path)
    import_id, version_id, batch_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        async with factory() as session:
            successes = [
                _attempt(import_id, version_id, batch_id, f"ok-{index}", "succeeded")
                for index in range(47)
            ]
            problems = [
                _attempt(import_id, version_id, batch_id, "transport-1", "retryable_failed"),
                _attempt(import_id, version_id, batch_id, "transport-2", "retryable_failed"),
                _attempt(import_id, version_id, batch_id, "semantic-1", "needs_review"),
            ]
            session.add_all(successes + problems)
            await session.commit()
            result = await retry_rule_sections(
                session,
                price_import_id=import_id,
                section_ids=[attempt.section_id for attempt in problems],
                retryable_only=False,
                actor="admin",
            )
            assert result.queued == 3
            assert all(attempt.status == "succeeded" for attempt in successes)
            assert all(attempt.status == "queued" for attempt in problems)
            for attempt in problems:
                attempt.status = "succeeded"
            await session.commit()
            state = await inspect_batch(session, batch_id=batch_id)
            assert state.ready is True
            assert state.succeeded_count == 50
    finally:
        await engine.dispose()


@async_test
async def test_review_import_resumes_but_other_terminal_import_is_blocked(tmp_path):
    engine, factory = await _database(tmp_path)
    now = datetime.now(timezone.utc)
    try:
        async with factory() as session:
            def make_import(status):
                return PriceImport(
                    id=uuid.uuid4(),
                    service_client_id=uuid.uuid4(), supplier_id=uuid.uuid4(),
                    price_file_id=uuid.uuid4(), price_version_id=uuid.uuid4(),
                    idempotency_key=str(uuid.uuid4()), request_fingerprint="c" * 64,
                    status=status, stage=status, progress_percent=100,
                )

            review_import = make_import("review_required")
            completed_import = make_import("completed")
            review_attempt = _attempt(
                review_import.id, review_import.price_version_id, uuid.uuid4(), "review", "needs_review"
            )
            blocked_attempt = _attempt(
                completed_import.id, completed_import.price_version_id, uuid.uuid4(), "blocked", "retryable_failed"
            )
            session.add_all([review_import, completed_import, review_attempt, blocked_attempt])
            await session.commit()

            result = await retry_rule_sections(
                session, price_import_id=review_import.id, section_ids=["review"],
                retryable_only=False, actor="admin", now=now,
            )
            assert result.queued == 1
            assert review_import.status == "generating_rule"
            assert review_import.finished_at is None

            try:
                await retry_rule_sections(
                    session, price_import_id=completed_import.id, section_ids=["blocked"],
                    retryable_only=True, actor="admin", now=now,
                )
            except RetryImportStateError:
                pass
            else:
                raise AssertionError("completed import must not resume")
            assert blocked_attempt.status == "retryable_failed"
    finally:
        await engine.dispose()
