from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import rule_generation_artifacts as artifact_service
from app.config import Settings
from app.models import (
    Base,
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


def _work_key_inputs() -> dict:
    return {
        "source_file_sha256": "a" * 64,
        "source_context_hash": "b" * 64,
        "prompt_version": "tabular-section-mapping-v7",
        "provider": "yandexgpt",
        "model": "gpt://folder/model/latest",
        "request_settings": {
            "temperature": 0,
            "top_p": 1,
            "max_output_tokens": 3500,
            "thinking": {"enabled": False},
        },
        "contract_version": "price-rule-proposal-v1",
    }


def test_durable_rule_generation_flag_defaults_to_false(monkeypatch) -> None:
    monkeypatch.delenv("DURABLE_RULE_GENERATION_ENABLED", raising=False)

    assert Settings().durable_rule_generation_enabled is False

    monkeypatch.setenv("DURABLE_RULE_GENERATION_ENABLED", "true")
    assert Settings().durable_rule_generation_enabled is True


def test_work_key_is_stable_for_equivalent_mapping_order() -> None:
    inputs = _work_key_inputs()
    reversed_settings = dict(reversed(list(inputs["request_settings"].items())))

    first = compute_rule_generation_work_key(**inputs)
    second = compute_rule_generation_work_key(
        **{**inputs, "request_settings": reversed_settings}
    )

    assert first == second
    assert len(first) == 64
    assert compute_request_settings_hash(
        inputs["request_settings"], contract_version=inputs["contract_version"]
    ) == compute_request_settings_hash(
        reversed_settings, contract_version=inputs["contract_version"]
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("source_file_sha256", "c" * 64),
        ("source_context_hash", "d" * 64),
        ("prompt_version", "tabular-section-mapping-v8"),
        ("provider", "openai"),
        ("model", "gpt-4o"),
        ("contract_version", "price-rule-proposal-v2"),
    ],
)
def test_work_key_changes_with_each_material_component(
    field: str, replacement: str
) -> None:
    inputs = _work_key_inputs()
    baseline = compute_rule_generation_work_key(**inputs)

    assert compute_rule_generation_work_key(
        **{**inputs, field: replacement}
    ) != baseline


def test_work_key_changes_with_material_inference_setting() -> None:
    inputs = _work_key_inputs()
    baseline = compute_rule_generation_work_key(**inputs)
    changed_settings = {**inputs["request_settings"], "temperature": 0.1}

    assert compute_rule_generation_work_key(
        **{**inputs, "request_settings": changed_settings}
    ) != baseline


def test_legacy_attempt_contract_uses_additive_defaults() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_factory() as session:
            attempt = RuleGenerationAttempt(
                price_version_id=uuid.uuid4(),
                batch_id=uuid.uuid4(),
                section_id="section-1",
                status="queued",
                provider="mock",
                model="mock-model",
                request_settings={},
                prompt_version="test-v1",
                prompt_id="p" * 64,
                source_file_sha256="a" * 64,
                source_context_hash="b" * 64,
                input_context={},
                prompt="test",
            )
            session.add(attempt)
            await session.commit()

            assert attempt.work_key is None
            assert attempt.artifact_id is None
            assert attempt.failure_class is None
            assert attempt.retry_count == 0
            assert attempt.semantic_retry_count == 0
            assert attempt.next_retry_at is None
            assert attempt.lease_until is None
            assert attempt.last_heartbeat_at is None
            assert attempt.reused_from_attempt_id is None
            assert attempt.terminal_reason is None

        await engine.dispose()

    asyncio.run(scenario())


def test_verified_artifact_creation_is_idempotent_and_lookup_is_exact() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        inputs = _work_key_inputs()
        work_key = compute_rule_generation_work_key(**inputs)
        kwargs = {
            "work_key": work_key,
            "provider": inputs["provider"],
            "model": inputs["model"],
            "request_settings_hash": compute_request_settings_hash(
                inputs["request_settings"],
                contract_version=inputs["contract_version"],
            ),
            "source_file_sha256": inputs["source_file_sha256"],
            "source_context_hash": inputs["source_context_hash"],
            "prompt_version": inputs["prompt_version"],
            "proposal": {"schema_version": inputs["contract_version"]},
            "validation": {"passed": True},
            "compiler_dry_run_summary": {"passed": True, "items": 3},
            "provenance": "model_verified",
        }

        async with session_factory() as session:
            first = await create_verified_artifact(session, **kwargs)
            await session.commit()
            second = await create_verified_artifact(session, **kwargs)
            await session.commit()

            assert second.id == first.id
            assert await find_verified_artifact(session, work_key) is second
            assert await find_verified_artifact(session, "f" * 64) is None
            assert (
                await session.scalar(select(func.count(RuleGenerationArtifact.id)))
                == 1
            )

        await engine.dispose()

    asyncio.run(scenario())


def test_verified_artifact_creation_converges_after_unique_race(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        inputs = _work_key_inputs()
        work_key = compute_rule_generation_work_key(**inputs)
        kwargs = {
            "work_key": work_key,
            "provider": inputs["provider"],
            "model": inputs["model"],
            "request_settings_hash": compute_request_settings_hash(
                inputs["request_settings"],
                contract_version=inputs["contract_version"],
            ),
            "source_file_sha256": inputs["source_file_sha256"],
            "source_context_hash": inputs["source_context_hash"],
            "prompt_version": inputs["prompt_version"],
            "proposal": {"schema_version": inputs["contract_version"]},
            "validation": {"passed": True},
            "compiler_dry_run_summary": {"passed": True},
            "provenance": "model_verified",
        }

        async with session_factory() as session:
            winner = await create_verified_artifact(session, **kwargs)
            await session.commit()

            original_find = artifact_service.find_verified_artifact
            lookup_count = 0

            async def race_aware_find(current_session, current_work_key):
                nonlocal lookup_count
                lookup_count += 1
                if lookup_count == 1:
                    return None
                return await original_find(current_session, current_work_key)

            monkeypatch.setattr(
                artifact_service,
                "find_verified_artifact",
                race_aware_find,
            )
            converged = await create_verified_artifact(session, **kwargs)
            await session.commit()

            assert converged.id == winner.id
            assert lookup_count == 2
            assert (
                await session.scalar(select(func.count(RuleGenerationArtifact.id)))
                == 1
            )

        await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("validation", "compiler_summary"),
    [
        ({"passed": False}, {"passed": True}),
        ({"passed": True}, {"passed": False}),
    ],
)
def test_unverified_output_cannot_be_persisted_as_artifact(
    validation: dict, compiler_summary: dict
) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        inputs = _work_key_inputs()
        async with session_factory() as session:
            with pytest.raises(ValueError):
                await create_verified_artifact(
                    session,
                    work_key=compute_rule_generation_work_key(**inputs),
                    provider=inputs["provider"],
                    model=inputs["model"],
                    request_settings_hash=compute_request_settings_hash(
                        inputs["request_settings"],
                        contract_version=inputs["contract_version"],
                    ),
                    source_file_sha256=inputs["source_file_sha256"],
                    source_context_hash=inputs["source_context_hash"],
                    prompt_version=inputs["prompt_version"],
                    proposal={},
                    validation=validation,
                    compiler_dry_run_summary=compiler_summary,
                    provenance="model_verified",
                )
            assert (
                await session.scalar(select(func.count(RuleGenerationArtifact.id)))
                == 0
            )

        await engine.dispose()

    asyncio.run(scenario())


def test_operation_number_is_unique_per_attempt() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        attempt_id = uuid.uuid4()
        async with session_factory() as session:
            session.add_all(
                [
                    RuleGenerationOperation(
                        attempt_id=attempt_id,
                        operation_no=1,
                        operation_kind="initial",
                        status="completed",
                        provider="mock",
                        model="mock-model",
                    ),
                    RuleGenerationOperation(
                        attempt_id=attempt_id,
                        operation_no=2,
                        operation_kind="semantic_repair",
                        status="completed",
                        provider="mock",
                        model="mock-model",
                    ),
                ]
            )
            await session.commit()

            session.add(
                RuleGenerationOperation(
                    attempt_id=attempt_id,
                    operation_no=1,
                    operation_kind="initial",
                    status="completed",
                    provider="mock",
                    model="mock-model",
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

            assert (
                await session.scalar(select(func.count(RuleGenerationOperation.id)))
                == 2
            )

        await engine.dispose()

    asyncio.run(scenario())
