from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from functools import wraps
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import (
    Base,
    ParseRule,
    PriceImport,
    PriceImportStatus,
    RuleGenerationArtifact,
    RuleGenerationAttempt,
    RuleGenerationOperation,
)


def async_test(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapper


async def _database(tmp_path):
    path = tmp_path / "stage3.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, factory


def _attempt(*, status: str = "queued", batch_id: uuid.UUID | None = None):
    return RuleGenerationAttempt(
        price_version_id=uuid.uuid4(),
        batch_id=batch_id or uuid.uuid4(),
        section_id=f"section-{uuid.uuid4()}",
        status=status,
        provider="openai",
        model="gpt-4o",
        request_settings={"timeout_seconds": 75},
        prompt_version="synthetic-v1",
        prompt_id=f"prompt-{uuid.uuid4()}",
        source_file_sha256="a" * 64,
        source_context_hash="b" * 64,
        input_context={"proposal_contract": "price-rule-proposal-v1"},
        prompt="synthetic prompt",
    )


@async_test
async def test_atomic_claim_duplicate_delivery_and_owned_heartbeat(tmp_path):
    from app.rule_generation_lifecycle import claim_attempt, heartbeat_attempt

    engine, factory = await _database(tmp_path)
    now = datetime.now(timezone.utc)
    try:
        async with factory() as session:
            attempt = _attempt()
            session.add(attempt)
            await session.commit()
            attempt_id = attempt.id

        async with factory() as first:
            claim = await claim_attempt(
                first, attempt_id=attempt_id, lease_owner="worker-a",
                lease_seconds=900, now=now,
            )
            assert claim.claimed is True
            assert claim.status == "leased"

        async with factory() as duplicate:
            claim = await claim_attempt(
                duplicate, attempt_id=attempt_id, lease_owner="worker-b",
                lease_seconds=900, now=now + timedelta(seconds=1),
            )
            assert claim.claimed is False
            assert claim.reason == "active_lease"
            assert await heartbeat_attempt(
                duplicate, attempt_id=attempt_id, lease_owner="worker-b",
                lease_seconds=900, now=now + timedelta(seconds=2),
            ) is False

        async with factory() as owner:
            assert await heartbeat_attempt(
                owner, attempt_id=attempt_id, lease_owner="worker-a",
                lease_seconds=900, now=now + timedelta(seconds=2),
            ) is True
    finally:
        await engine.dispose()


@async_test
async def test_stale_lease_recovery_does_not_touch_active_lease(tmp_path):
    from app.rule_generation_lifecycle import recover_stale_attempts

    engine, factory = await _database(tmp_path)
    now = datetime.now(timezone.utc)
    try:
        async with factory() as session:
            expired = _attempt(status="leased")
            expired.lease_owner = "dead-worker"
            expired.lease_until = now - timedelta(seconds=1)
            active = _attempt(status="leased")
            active.lease_owner = "live-worker"
            active.lease_until = now + timedelta(minutes=10)
            session.add_all([expired, active])
            await session.commit()
            expired_id, active_id = expired.id, active.id

        async with factory() as session:
            recovered = await recover_stale_attempts(session, now=now, limit=100)
            assert recovered == [expired_id]
            assert (await session.get(RuleGenerationAttempt, expired_id)).status == "queued"
            assert (await session.get(RuleGenerationAttempt, active_id)).status == "leased"
    finally:
        await engine.dispose()


@async_test
async def test_dispatcher_selects_only_queued_and_due_transport_retry(tmp_path):
    from app.rule_generation_lifecycle import reserve_dispatchable_attempts

    engine, factory = await _database(tmp_path)
    now = datetime.now(timezone.utc)
    batch_id = uuid.uuid4()
    try:
        async with factory() as session:
            queued = _attempt(status="queued", batch_id=batch_id)
            due = _attempt(status="retryable_failed", batch_id=batch_id)
            due.next_retry_at = now - timedelta(seconds=1)
            future = _attempt(status="retryable_failed", batch_id=batch_id)
            future.next_retry_at = now + timedelta(minutes=5)
            succeeded = _attempt(status="succeeded", batch_id=batch_id)
            review = _attempt(status="needs_review", batch_id=batch_id)
            session.add_all([queued, due, future, succeeded, review])
            await session.commit()
            expected = {queued.id, due.id}

        async with factory() as session:
            selected = await reserve_dispatchable_attempts(
                session, now=now, limit=100, publication_lease_seconds=60,
            )
            assert set(selected) == expected
            assert all(isinstance(value, uuid.UUID) for value in selected)
            assert await reserve_dispatchable_attempts(
                session, now=now, limit=100, publication_lease_seconds=60,
            ) == []
            republished = await reserve_dispatchable_attempts(
                session,
                now=now + timedelta(seconds=61),
                limit=100,
                publication_lease_seconds=60,
            )
            assert set(republished) == expected
    finally:
        await engine.dispose()


@async_test
async def test_transport_retry_is_bounded_and_semantic_failure_is_terminal(tmp_path):
    from app.rule_generation_lifecycle import record_section_failure

    engine, factory = await _database(tmp_path)
    now = datetime.now(timezone.utc)
    try:
        async with factory() as session:
            transport = _attempt(status="leased")
            transport.lease_owner = "worker"
            semantic = _attempt(status="leased")
            semantic.lease_owner = "worker"
            session.add_all([transport, semantic])
            await session.commit()

            status = await record_section_failure(
                session, attempt_id=transport.id, lease_owner="worker",
                failure_class="transport", terminal_reason="ReadTimeout",
                retry_delays=(60, 300, 900), now=now,
            )
            assert status == "retryable_failed"
            await session.refresh(transport)
            assert transport.retry_count == 1
            next_retry_at = transport.next_retry_at
            if next_retry_at.tzinfo is None:  # SQLite test dialect
                next_retry_at = next_retry_at.replace(tzinfo=timezone.utc)
            assert next_retry_at == now + timedelta(seconds=60)

            status = await record_section_failure(
                session, attempt_id=semantic.id, lease_owner="worker",
                failure_class="semantic", terminal_reason="source_compiler_reject",
                retry_delays=(60, 300, 900), now=now,
            )
            assert status == "needs_review"
            await session.refresh(semantic)
            assert semantic.next_retry_at is None

            transport.status = "leased"
            transport.lease_owner = "worker"
            transport.retry_count = 3
            await session.commit()
            status = await record_section_failure(
                session, attempt_id=transport.id, lease_owner="worker",
                failure_class="transport", terminal_reason="HTTP_503",
                retry_delays=(60, 300, 900), now=now,
            )
            assert status == "needs_review"
    finally:
        await engine.dispose()


@async_test
async def test_batch_47_2_1_preserves_successful_artifacts_and_is_not_ready(tmp_path):
    from app.rule_generation_lifecycle import inspect_batch

    engine, factory = await _database(tmp_path)
    batch_id = uuid.uuid4()
    try:
        async with factory() as session:
            successful = []
            for index in range(47):
                artifact = RuleGenerationArtifact(
                    work_key=f"{index:064x}", provider="openai", model="gpt-4o",
                    request_settings_hash="c" * 64, source_file_sha256="a" * 64,
                    source_context_hash=f"{index + 100:064x}", prompt_version="v1",
                    proposal={"decision": "propose"}, validation={"passed": True},
                    compiler_dry_run_summary={"passed": True}, provenance="model_verified",
                )
                session.add(artifact)
                await session.flush()
                attempt = _attempt(status="succeeded", batch_id=batch_id)
                attempt.artifact_id = artifact.id
                successful.append(attempt)
            retrying = [_attempt(status="retryable_failed", batch_id=batch_id) for _ in range(2)]
            review = _attempt(status="needs_review", batch_id=batch_id)
            session.add_all([*successful, *retrying, review])
            await session.commit()

            state = await inspect_batch(session, batch_id=batch_id)
            assert state.ready is False
            assert state.pending_count == 2
            assert state.review_count == 1
            assert await session.scalar(select(func.count(RuleGenerationArtifact.id))) == 47
            assert await session.scalar(select(func.count(ParseRule.id))) == 0
    finally:
        await engine.dispose()


def test_stage3_celery_entrypoints_and_configuration_exist():
    from app import tasks
    from app.config import settings

    assert callable(tasks.generate_rule_proposal_section)
    assert callable(tasks.dispatch_queued_rule_attempts)
    assert callable(tasks.recover_stale_rule_attempts)
    assert callable(tasks.finalize_rule_batch)
    assert settings.rule_generation_lease_seconds >= 300
    assert tuple(settings.rule_generation_retry_delays_seconds) == (60, 300, 900)


@async_test
async def test_started_operation_can_be_recovered_only_after_section_reclaim(tmp_path):
    from app.durable_rule_generation import reserve_operation

    engine, factory = await _database(tmp_path)
    try:
        async with factory() as session:
            attempt = _attempt(status="leased")
            attempt.lease_owner = "replacement"
            session.add(attempt)
            await session.flush()
            operation = RuleGenerationOperation(
                attempt_id=attempt.id,
                operation_no=1,
                operation_kind="initial",
                status="started",
                provider="openai",
                model="gpt-4o",
                started_at=datetime.now(timezone.utc) - timedelta(minutes=20),
            )
            session.add(operation)
            await session.commit()

            recovered = await reserve_operation(
                session,
                attempt_id=attempt.id,
                operation_no=1,
                operation_kind="initial",
                provider="openai",
                model="gpt-4o",
                allow_recovery=True,
            )
            assert recovered.should_call_provider is True
            assert recovered.operation_id == operation.id

            operation = await session.get(RuleGenerationOperation, operation.id)
            operation.status = "failed"
            await session.commit()
            retried = await reserve_operation(
                session,
                attempt_id=attempt.id,
                operation_no=1,
                operation_kind="initial",
                provider="openai",
                model="gpt-4o",
                allow_recovery=True,
            )
            assert retried.should_call_provider is True
            assert retried.operation_id != operation.id
            assert await session.scalar(select(func.count(RuleGenerationOperation.id))) == 2
    finally:
        await engine.dispose()


@async_test
async def test_coordinator_is_idempotent_for_terminal_import(tmp_path, monkeypatch):
    from app import import_service, tasks

    engine, factory = await _database(tmp_path)
    batch_id = uuid.uuid4()
    try:
        async with factory() as session:
            price_import = PriceImport(
                service_client_id=uuid.uuid4(),
                supplier_id=uuid.uuid4(),
                price_file_id=uuid.uuid4(),
                price_version_id=uuid.uuid4(),
                idempotency_key="stage3-coordinator",
                request_fingerprint="f" * 64,
                status=PriceImportStatus.generating_rule.value,
                stage=PriceImportStatus.generating_rule.value,
                rule_batch_id=batch_id,
            )
            attempt = _attempt(status="succeeded", batch_id=batch_id)
            attempt.price_import_id = price_import.id
            session.add_all([price_import, attempt])
            await session.commit()

        calls = 0
        coordinator_lock = asyncio.Lock()

        async def fake_advance(session, import_id):
            nonlocal calls
            # Production uses the existing PostgreSQL advisory lock in
            # advance_price_import. This lock models that critical section for
            # the SQLite test dialect.
            async with coordinator_lock:
                price_import = await session.get(PriceImport, import_id)
                await session.refresh(price_import)
                if price_import.status in PriceImportStatus.terminal_values():
                    return price_import.status
                calls += 1
                await asyncio.sleep(0.01)
                price_import.status = PriceImportStatus.completed.value
                price_import.stage = PriceImportStatus.completed.value
                await session.commit()
                return price_import.status

        monkeypatch.setattr(tasks, "make_task_engine", lambda: (engine, factory))
        monkeypatch.setattr(import_service, "advance_price_import", fake_advance)
        results = await asyncio.gather(
            tasks._finalize_rule_batch(str(batch_id)),
            tasks._finalize_rule_batch(str(batch_id)),
        )
        assert results == ["completed", "completed"]
        assert calls == 1
    finally:
        await engine.dispose()


def test_feature_flag_false_keeps_legacy_batch_entrypoint(monkeypatch):
    from app import import_service, tasks

    monkeypatch.setattr(import_service.settings, "durable_rule_generation_enabled", False)
    assert import_service.rule_generation_task_name() == "app.tasks.generate_rule_proposal_batch"
    monkeypatch.setattr(tasks.settings, "durable_rule_generation_enabled", False)
    assert tasks.dispatch_queued_rule_attempts() == {"published": 0, "finalizers": 0}
    assert tasks.recover_stale_rule_attempts() == {"recovered": 0}


def test_feature_flag_true_selects_section_workflow(monkeypatch):
    from app import import_service

    monkeypatch.setattr(import_service.settings, "durable_rule_generation_enabled", True)
    assert import_service.rule_generation_task_name() == "app.tasks.generate_rule_proposal_section"
