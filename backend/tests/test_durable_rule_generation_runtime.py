from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256
import json
import uuid

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import (
    Base,
    PriceFile,
    PriceVersion,
    RuleGenerationArtifact,
    RuleGenerationAttempt,
    RuleGenerationOperation,
    Supplier,
)
from app.parsing.grid import Cell, SheetGrid
from app.parsing.rule_proposal import context_hash
from app.parsing.rule_proposal_handlers import (
    TABULAR_PROPOSAL_CONTRACT,
    build_rule_section_contexts,
)
from app import rule_proposal_service as service


SECRET = "SUPER_SECRET_TEST_VALUE"


class SimulatedCrash(BaseException):
    pass


def _grid(*, item_name: str = "Клапан") -> SheetGrid:
    return SheetGrid(
        name="Прайс",
        rows=[
            [Cell("Наименование"), Cell("Артикул"), Cell("Цена")],
            [Cell(item_name), Cell("A-1"), Cell("1200")],
        ],
    )


def _proposal(context: dict, *, price_index: int = 2, rule_name: str = "Правило") -> dict:
    columns = [
        {
            "index": 0,
            "role": "name",
            "price_descriptor": None,
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        },
        {
            "index": 1,
            "role": "article",
            "price_descriptor": None,
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        },
        {
            "index": price_index,
            "role": "price",
            "price_descriptor": {"kind": "retail", "vat_mode": "unknown"},
            "attribute_code": None,
            "attribute_value_kind": None,
            "canonical_unit": None,
        },
    ]
    return {
        "schema_version": "price-rule-proposal-v1",
        "decision": "propose",
        "rule_name": rule_name,
        "loader": "xlsx",
        "source_section_id": context["section_id"],
        "mapping": {
            "sheet_name": context["sheet_name"],
            "header_row_index": context["header_row_index"],
            "columns": columns,
        },
        "discovered_attributes": [],
        "evidence": [
            {
                "row_index": context["header_row_index"],
                "column_index": index,
                "note": "synthetic header",
            }
            for index in (0, 1, price_index)
        ],
        "unresolved": [],
        "confidence": 0.95,
        "requires_manual_review": True,
    }


def _telemetry(call_no: int, usage: dict | None = None, *, error_type=None) -> dict:
    normalized_usage = usage or {
        "input_tokens": 20,
        "output_tokens": 10,
        "total_tokens": 30,
        "cached_input_tokens": 5,
        "billable_input_tokens": 15,
        "cache_hit_ratio": 0.25,
    }
    return {
        "schema_version": "llm_request_v1",
        "event_name": "llm.request.failed" if error_type else "llm.request.completed",
        "operation_id": f"logical-{call_no}",
        "provider": "openai",
        "model": "gpt-4o",
        "started_at": "2026-08-25T10:00:00+00:00",
        "finished_at": "2026-08-25T10:00:01+00:00",
        "http_attempts": 2,
        "http_status_code": None if error_type == "transport_error" else 200,
        "response_id": None if error_type else f"provider-{call_no}",
        "usage": normalized_usage,
        "raw_usage": {
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "total_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 5},
        },
        "request_settings": {"temperature": 0, "top_p": 1},
        "http_attempt_details": [],
        "error_type": error_type,
        "dimensions": {},
    }


class FakeProvider:
    def __init__(self, payloads: list[dict] | None = None, error: Exception | None = None):
        self.payloads = list(payloads or [])
        self.error = error
        self.calls = 0

    async def __call__(self, **_kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        payload = self.payloads[min(self.calls - 1, len(self.payloads) - 1)]
        usage = _telemetry(self.calls)["usage"]
        return service.RuleModelResult(
            payload=payload,
            usage=usage,
            telemetry=_telemetry(self.calls, usage),
        )


async def _database():
    engine = create_async_engine("sqlite+aiosqlite://")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, factory


async def _add_attempt(
    session,
    *,
    grid: SheetGrid,
    data: bytes,
    suffix: str,
    prompt_version: str | None = None,
    temperature: float = 0,
) -> tuple[RuleGenerationAttempt, dict]:
    context = build_rule_section_contexts([grid])[0]
    supplier = Supplier(code=f"supplier-{suffix}", name=f"Supplier {suffix}")
    session.add(supplier)
    await session.flush()
    price_file = PriceFile(
        supplier_id=supplier.id,
        original_filename=f"synthetic-{suffix}.xlsx",
        storage_key=f"synthetic/{suffix}",
        sha256=sha256(data).hexdigest(),
        size_bytes=len(data),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    session.add(price_file)
    await session.flush()
    version = PriceVersion(
        supplier_id=supplier.id,
        price_file_id=price_file.id,
        version_no=1,
    )
    session.add(version)
    await session.flush()
    attempt = RuleGenerationAttempt(
        price_version_id=version.id,
        batch_id=uuid.uuid4(),
        section_id=context["section_id"],
        status=service.ATTEMPT_QUEUED,
        provider="openai",
        model="gpt-4o",
        request_settings={
            "temperature": temperature,
            "top_p": 1,
            "max_output_tokens": 3500,
            "timeout_seconds": 75,
        },
        prompt_version=prompt_version
        or str(context.get("proposal_contract", TABULAR_PROPOSAL_CONTRACT)),
        prompt_id=f"prompt-{suffix}",
        source_file_sha256=sha256(data).hexdigest(),
        source_context_hash=context_hash(context),
        input_context=context,
        prompt="Classify this bounded synthetic section",
    )
    session.add(attempt)
    await session.commit()
    return attempt, context


def _install_runtime(monkeypatch, data_to_grid: dict[bytes, SheetGrid], provider) -> None:
    monkeypatch.setattr(service.settings, "durable_rule_generation_enabled", True)
    monkeypatch.setattr(service.settings, "openai_api_key", SECRET)
    monkeypatch.setattr(
        service.storage,
        "get_price_file",
        lambda storage_key: next(
            data for data in data_to_grid if storage_key.endswith(data.decode())
        ),
    )
    monkeypatch.setattr(service.loaders, "detect_loader", lambda *_args: "xlsx")
    monkeypatch.setattr(
        service.loaders,
        "load_grids_for",
        lambda _loader, data, _filename: [data_to_grid[data]],
    )
    monkeypatch.setattr(service, "_request_rule_proposal_result", provider)


def test_paid_response_is_committed_before_compiler_and_resumed(monkeypatch) -> None:
    async def scenario() -> None:
        engine, factory = await _database()
        data = b"crash"
        grid = _grid()
        async with factory() as session:
            attempt, context = await _add_attempt(
                session, grid=grid, data=data, suffix=data.decode()
            )
            provider = FakeProvider([_proposal(context)])
            _install_runtime(monkeypatch, {data: grid}, provider)

            original_dispatch = service.handler_for_contract
            original_handler = original_dispatch(
                context.get("proposal_contract", TABULAR_PROPOSAL_CONTRACT)
            )

            def crash_before_compiler(_payload):
                raise SimulatedCrash()

            monkeypatch.setattr(
                service,
                "handler_for_contract",
                lambda _contract: replace(original_handler, parse=crash_before_compiler),
            )
            with pytest.raises(SimulatedCrash):
                await service.run_rule_generation_attempt(session, attempt.id)

            operation = await session.scalar(select(RuleGenerationOperation))
            assert operation is not None
            assert operation.status == "completed"
            assert operation.response_payload["payload"]["decision"] == "propose"
            assert operation.usage["total_tokens"] == 30
            assert operation.http_attempts == 2
            assert (
                await session.scalar(select(func.count(RuleGenerationOperation.id)))
                == 1
            )
            assert provider.calls == 1

            monkeypatch.setattr(service, "handler_for_contract", original_dispatch)
            assert (
                await service.run_rule_generation_attempt(session, attempt.id)
                == service.ATTEMPT_PROPOSED
            )
            await session.refresh(attempt)
            assert provider.calls == 1
            assert attempt.artifact_id is not None
            assert await session.scalar(select(func.count(RuleGenerationArtifact.id))) == 1

        await engine.dispose()

    asyncio.run(scenario())


def test_exact_artifact_reuse_skips_provider_but_changed_key_does_not(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        engine, factory = await _database()
        data = b"reuse"
        grid = _grid()
        async with factory() as session:
            first, context = await _add_attempt(
                session, grid=grid, data=data, suffix=data.decode()
            )
            provider = FakeProvider([_proposal(context)])
            _install_runtime(monkeypatch, {data: grid}, provider)
            assert await service.run_rule_generation_attempt(session, first.id) == "proposed"
            first_artifact_id = first.artifact_id
            assert first_artifact_id is not None
            assert await service.run_rule_generation_attempt(session, first.id) == "proposed"
            assert provider.calls == 1
            assert await session.scalar(select(func.count(RuleGenerationArtifact.id))) == 1

            second = RuleGenerationAttempt(
                price_version_id=first.price_version_id,
                batch_id=uuid.uuid4(),
                section_id=first.section_id,
                status=service.ATTEMPT_QUEUED,
                provider=first.provider,
                model=first.model,
                request_settings=dict(first.request_settings),
                prompt_version=first.prompt_version,
                prompt_id="prompt-exact-reuse",
                source_file_sha256=first.source_file_sha256,
                source_context_hash=first.source_context_hash,
                input_context=dict(first.input_context),
                prompt=first.prompt,
            )
            session.add(second)
            await session.commit()
            assert await service.run_rule_generation_attempt(session, second.id) == "proposed"
            assert provider.calls == 1
            assert second.artifact_id == first_artifact_id

            changed = RuleGenerationAttempt(
                price_version_id=first.price_version_id,
                batch_id=uuid.uuid4(),
                section_id=first.section_id,
                status=service.ATTEMPT_QUEUED,
                provider=first.provider,
                model=first.model,
                request_settings={**first.request_settings, "temperature": 0.2},
                prompt_version=first.prompt_version,
                prompt_id="prompt-different-key",
                source_file_sha256=first.source_file_sha256,
                source_context_hash=first.source_context_hash,
                input_context=dict(first.input_context),
                prompt=first.prompt,
            )
            session.add(changed)
            await session.commit()
            assert await service.run_rule_generation_attempt(session, changed.id) == "proposed"
            assert provider.calls == 2
            assert changed.artifact_id != first_artifact_id

            incomplete = RuleGenerationAttempt(
                price_version_id=first.price_version_id,
                batch_id=uuid.uuid4(),
                section_id=first.section_id,
                status=service.ATTEMPT_QUEUED,
                provider=first.provider,
                model=first.model,
                request_settings=dict(first.request_settings),
                prompt_version="",
                prompt_id="prompt-incomplete-key",
                source_file_sha256=first.source_file_sha256,
                source_context_hash=first.source_context_hash,
                input_context=dict(first.input_context),
                prompt=first.prompt,
            )
            session.add(incomplete)
            await session.commit()
            assert await service.run_rule_generation_attempt(session, incomplete.id) == "proposed"
            assert provider.calls == 3
            assert incomplete.work_key is None
            assert incomplete.artifact_id is None

        await engine.dispose()

    asyncio.run(scenario())


def test_transport_failure_is_persisted_without_artifact(monkeypatch) -> None:
    async def scenario() -> None:
        engine, factory = await _database()
        data = b"timeout"
        grid = _grid()
        async with factory() as session:
            attempt, _context = await _add_attempt(
                session, grid=grid, data=data, suffix=data.decode()
            )
            error = httpx.ReadTimeout("synthetic provider timeout")
            provider = FakeProvider(error=error)
            _install_runtime(monkeypatch, {data: grid}, provider)
            assert await service.run_rule_generation_attempt(session, attempt.id) == "failed"

            operation = await session.scalar(select(RuleGenerationOperation))
            assert operation is not None
            assert operation.status == "failed"
            assert operation.error_type == "transport_error"
            assert operation.http_attempts == 1
            assert await session.scalar(select(func.count(RuleGenerationArtifact.id))) == 0

        await engine.dispose()

    asyncio.run(scenario())


def test_semantic_repair_response_is_resumed_without_second_paid_call(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        engine, factory = await _database()
        data = b"repair"
        grid = _grid()
        async with factory() as session:
            attempt, context = await _add_attempt(
                session, grid=grid, data=data, suffix=data.decode()
            )
            provider = FakeProvider(
                [_proposal(context, price_index=9), _proposal(context)]
            )
            _install_runtime(monkeypatch, {data: grid}, provider)

            original_dispatch = service.handler_for_contract
            original_handler = original_dispatch(
                context.get("proposal_contract", TABULAR_PROPOSAL_CONTRACT)
            )
            parse_calls = 0

            def crash_on_repair(payload):
                nonlocal parse_calls
                parse_calls += 1
                if parse_calls == 2:
                    raise SimulatedCrash()
                return original_handler.parse(payload)

            monkeypatch.setattr(
                service,
                "handler_for_contract",
                lambda _contract: replace(original_handler, parse=crash_on_repair),
            )
            with pytest.raises(SimulatedCrash):
                await service.run_rule_generation_attempt(session, attempt.id)
            operations = (
                (
                    await session.execute(
                        select(RuleGenerationOperation).order_by(
                            RuleGenerationOperation.operation_no
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [(row.operation_no, row.operation_kind, row.status) for row in operations] == [
                (1, "initial", "completed"),
                (2, "semantic_repair", "completed"),
            ]
            assert provider.calls == 2

            monkeypatch.setattr(service, "handler_for_contract", original_dispatch)
            assert await service.run_rule_generation_attempt(session, attempt.id) == "proposed"
            assert provider.calls == 2
            assert attempt.artifact_id is not None

        await engine.dispose()

    asyncio.run(scenario())


def test_semantic_rejection_keeps_operations_but_not_artifact(monkeypatch) -> None:
    async def scenario() -> None:
        engine, factory = await _database()
        data = b"reject"
        grid = _grid()
        async with factory() as session:
            attempt, context = await _add_attempt(
                session, grid=grid, data=data, suffix=data.decode()
            )
            provider = FakeProvider(
                [_proposal(context, price_index=9), _proposal(context, price_index=9)]
            )
            _install_runtime(monkeypatch, {data: grid}, provider)
            assert await service.run_rule_generation_attempt(session, attempt.id) == "rejected"
            assert await session.scalar(select(func.count(RuleGenerationOperation.id))) == 2
            assert await session.scalar(select(func.count(RuleGenerationArtifact.id))) == 0
            await session.refresh(attempt)
            assert attempt.validation["passed"] is False

        await engine.dispose()

    asyncio.run(scenario())


def test_secret_is_not_persisted_in_durable_rows(monkeypatch) -> None:
    async def scenario() -> None:
        engine, factory = await _database()
        data = b"secret"
        grid = _grid()
        async with factory() as session:
            attempt, context = await _add_attempt(
                session, grid=grid, data=data, suffix=data.decode()
            )
            provider = FakeProvider([_proposal(context, rule_name=SECRET)])
            _install_runtime(monkeypatch, {data: grid}, provider)
            assert await service.run_rule_generation_attempt(session, attempt.id) == "proposed"

            operation = await session.scalar(select(RuleGenerationOperation))
            artifact = await session.scalar(select(RuleGenerationArtifact))
            serialized = json.dumps(
                {
                    "operation": {
                        "response": operation.response_payload,
                        "usage": operation.usage,
                        "error": operation.error_type,
                        "provider_operation_id": operation.provider_operation_id,
                    },
                    "artifact": artifact.proposal,
                    "attempt": {
                        "proposal": attempt.proposal,
                        "validation": attempt.validation,
                        "error": attempt.error_message,
                    },
                },
                ensure_ascii=False,
                default=str,
            )
            assert SECRET not in serialized

        await engine.dispose()

    asyncio.run(scenario())


def test_existing_started_operation_blocks_a_concurrent_paid_call(monkeypatch) -> None:
    async def scenario() -> None:
        engine, factory = await _database()
        data = b"concurrent"
        grid = _grid()
        async with factory() as session:
            attempt, context = await _add_attempt(
                session, grid=grid, data=data, suffix=data.decode()
            )
            attempt.status = service.ATTEMPT_GENERATING
            session.add(
                RuleGenerationOperation(
                    attempt_id=attempt.id,
                    operation_no=1,
                    operation_kind="initial",
                    status="started",
                    provider="openai",
                    model="gpt-4o",
                )
            )
            await session.commit()
            provider = FakeProvider([_proposal(context)])
            _install_runtime(monkeypatch, {data: grid}, provider)

            assert (
                await service.run_rule_generation_attempt(session, attempt.id)
                == service.ATTEMPT_GENERATING
            )
            assert provider.calls == 0
            assert (
                await session.scalar(select(func.count(RuleGenerationOperation.id)))
                == 1
            )

        await engine.dispose()

    asyncio.run(scenario())


def test_feature_flag_false_preserves_legacy_path(monkeypatch) -> None:
    async def scenario() -> None:
        engine, factory = await _database()
        data = b"legacy"
        grid = _grid()
        async with factory() as session:
            attempt, context = await _add_attempt(
                session, grid=grid, data=data, suffix=data.decode()
            )
            provider = FakeProvider([_proposal(context)])
            _install_runtime(monkeypatch, {data: grid}, provider)
            monkeypatch.setattr(service.settings, "durable_rule_generation_enabled", False)
            assert await service.run_rule_generation_attempt(session, attempt.id) == "proposed"
            assert provider.calls == 1
            assert await session.scalar(select(func.count(RuleGenerationOperation.id))) == 0
            assert await session.scalar(select(func.count(RuleGenerationArtifact.id))) == 0

        await engine.dispose()

    asyncio.run(scenario())
