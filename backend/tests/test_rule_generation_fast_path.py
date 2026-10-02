from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import uuid

import pytest
from sqlalchemy import func, select

from app import rule_proposal_service as service
from app.models import (
    PriceFile,
    PriceImport,
    PriceImportStatus,
    PriceVersion,
    RuleGenerationArtifact,
    RuleGenerationAttempt,
    RuleGenerationOperation,
    ServiceClient,
    SupplierPriceItem,
    SupplierPriceValue,
)
from app.parsing import rule_generation_fast_path as fast_path
from app.parsing.rule_proposal import ProposalError, context_hash
from app.parsing.rule_proposal_handlers import TABULAR_PROPOSAL_CONTRACT
from app.parsing.section_diagnostics import evaluate_section_llm_need
from app.rule_generation_status_service import list_rule_sections
from tests.test_durable_rule_generation_runtime import (
    FakeProvider,
    _add_attempt,
    _database,
    _grid,
    _install_runtime,
    _proposal,
    _telemetry,
)


def _enable(monkeypatch, *, enabled: bool) -> None:
    monkeypatch.setattr(service.settings, "rule_generation_fast_path_enabled", enabled)


def test_a_flag_off_preserves_provider_workflow(monkeypatch) -> None:
    async def scenario() -> None:
        engine, factory = await _database()
        data = b"fast-flag-off"
        grid = _grid()
        async with factory() as session:
            attempt, context = await _add_attempt(
                session, grid=grid, data=data, suffix=data.decode()
            )
            provider = FakeProvider([_proposal(context)])
            _install_runtime(monkeypatch, {data: grid}, provider)
            _enable(monkeypatch, enabled=False)

            assert await service.run_rule_generation_attempt(session, attempt.id) == "proposed"
            assert provider.calls == 1
            assert await session.scalar(select(func.count(RuleGenerationOperation.id))) == 1
            artifact = await session.get(RuleGenerationArtifact, attempt.artifact_id)
            assert artifact is not None
            assert artifact.provenance == "model_verified"
        await engine.dispose()

    asyncio.run(scenario())


def test_b_flag_on_compiles_dry_runs_materializes_and_never_calls_provider(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        engine, factory = await _database()
        data = b"fast-complete"
        grid = _grid()
        async with factory() as session:
            attempt, _ = await _add_attempt(
                session, grid=grid, data=data, suffix=data.decode()
            )
            provider = FakeProvider(error=AssertionError("provider must not be called"))
            _install_runtime(monkeypatch, {data: grid}, provider)
            _enable(monkeypatch, enabled=True)

            assert await service.run_rule_generation_attempt(session, attempt.id) == "proposed"
            await session.refresh(attempt)
            artifact = await session.get(RuleGenerationArtifact, attempt.artifact_id)
            assert provider.calls == 0
            assert artifact is not None
            assert artifact.provenance == "deterministic_fast_path"
            assert attempt.validation["execution_path"] == "deterministic_fast_path"
            assert attempt.validation["model_usage"]["input_tokens"] == 0
            assert await session.scalar(select(func.count(RuleGenerationOperation.id))) == 0

            client = ServiceClient(
                name="Fast path status client",
                key_prefix="fast-path",
                key_digest="f" * 64,
            )
            session.add(client)
            await session.flush()
            version = await session.get(PriceVersion, attempt.price_version_id)
            assert version is not None
            price_import = PriceImport(
                service_client_id=client.id,
                supplier_id=version.supplier_id,
                price_file_id=version.price_file_id,
                price_version_id=version.id,
                idempotency_key="fast-path-status",
                request_fingerprint="s" * 64,
                status=PriceImportStatus.generating_rule.value,
                stage=PriceImportStatus.generating_rule.value,
                rule_batch_id=attempt.batch_id,
            )
            session.add(price_import)
            await session.flush()
            attempt.price_import_id = price_import.id
            await session.commit()
            sections = await list_rule_sections(
                session, price_import_id=price_import.id
            )
            assert sections[0].provenance == "deterministic_fast_path"

            price_file = await session.get(PriceFile, version.price_file_id)
            first = await service.materialize_succeeded_sections(
                session,
                version=version,
                price_file=price_file,
                data=data,
                attempts=[attempt],
            )
            duplicate = await service.materialize_succeeded_sections(
                session,
                version=version,
                price_file=price_file,
                data=data,
                attempts=[attempt],
            )
            assert first["sections_materialized"] == 1
            assert duplicate["sections_materialized"] == 0
            assert await session.scalar(select(func.count(SupplierPriceItem.id))) == 1
            assert await session.scalar(select(func.count(SupplierPriceValue.id))) == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_known_verified_output_mismatch_reuses_artifact_fail_closed(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        engine, factory = await _database()
        data = b"fast-known-artifact-mismatch"
        grid = _grid()
        async with factory() as session:
            attempt, context = await _add_attempt(
                session, grid=grid, data=data, suffix=data.decode()
            )
            provider = FakeProvider([_proposal(context)])
            _install_runtime(monkeypatch, {data: grid}, provider)

            _enable(monkeypatch, enabled=False)
            assert await service.run_rule_generation_attempt(session, attempt.id) == "proposed"
            model_artifact_id = attempt.artifact_id
            assert provider.calls == 1

            attempt.status = service.ATTEMPT_QUEUED
            attempt.proposal = None
            attempt.validation = None
            attempt.artifact_id = None
            attempt.work_key = None
            await session.commit()

            _enable(monkeypatch, enabled=True)
            assert await service.run_rule_generation_attempt(session, attempt.id) == "proposed"
            await session.refresh(attempt)
            artifact = await session.get(RuleGenerationArtifact, attempt.artifact_id)
            assert provider.calls == 1
            assert attempt.artifact_id == model_artifact_id
            assert artifact is not None
            assert artifact.provenance == "model_verified"
            assert (
                attempt.validation["fast_path"]["reason"]
                == "verified_artifact_output_mismatch"
            )
            assert await session.scalar(select(func.count(RuleGenerationOperation.id))) == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_h_i_48_fast_path_two_llm_and_one_rejected_materialize_49(
    monkeypatch,
) -> None:
    class ContextProvider:
        def __init__(self, contexts: dict[str, dict]):
            self.contexts = contexts
            self.calls = 0

        async def __call__(self, *, prompt: str, **_kwargs):
            self.calls += 1
            payload = _proposal(self.contexts[prompt])
            usage = _telemetry(self.calls)["usage"]
            return service.RuleModelResult(
                payload=payload,
                usage=usage,
                telemetry=_telemetry(self.calls, usage),
            )

    async def scenario() -> None:
        engine, factory = await _database()
        data = b"fast-48-plus-2"
        grid = _grid()
        async with factory() as session:
            first, base_context = await _add_attempt(
                session, grid=grid, data=data, suffix=data.decode()
            )
            attempts: list[RuleGenerationAttempt] = []
            contexts: dict[str, dict] = {}
            for index in range(50):
                section_id = f"sec_fast_{index:02d}"
                context = deepcopy(base_context)
                context["section_id"] = section_id
                if index >= 48:
                    context["deterministic_observations"] = [
                        row
                        for row in context["deterministic_observations"]
                        if row.get("suggested_role") != "price"
                    ]
                contexts[section_id] = context
                attempt = first if index == 0 else RuleGenerationAttempt(
                    price_version_id=first.price_version_id,
                    batch_id=first.batch_id,
                    section_id=section_id,
                    status=service.ATTEMPT_QUEUED,
                    provider=first.provider,
                    model=first.model,
                    request_settings=dict(first.request_settings),
                    prompt_version=TABULAR_PROPOSAL_CONTRACT,
                    prompt_id=f"prompt-fast-{index:02d}",
                    source_file_sha256=sha256(data).hexdigest(),
                    source_context_hash=context_hash(context),
                    input_context=context,
                    prompt=section_id,
                )
                if index == 0:
                    attempt.section_id = section_id
                    attempt.source_context_hash = context_hash(context)
                    attempt.input_context = context
                    attempt.prompt = section_id
                attempts.append(attempt)
            session.add_all(attempts[1:])
            await session.commit()

            provider = ContextProvider(contexts)
            _install_runtime(monkeypatch, {data: grid}, provider)
            _enable(monkeypatch, enabled=True)
            monkeypatch.setattr(
                service,
                "verified_current_context",
                lambda *, attempt, **_kwargs: attempt.input_context,
            )

            for attempt in attempts:
                assert await service.run_rule_generation_attempt(session, attempt.id) == "proposed"
            assert provider.calls == 2
            assert await session.scalar(select(func.count(RuleGenerationOperation.id))) == 2
            assert await session.scalar(
                select(func.count(RuleGenerationArtifact.id)).where(
                    RuleGenerationArtifact.provenance == "deterministic_fast_path"
                )
            ) == 48

            # One of the two LLM sections is rejected before coordinator delivery.
            attempts[-1].status = service.ATTEMPT_REJECTED
            await session.commit()
            version = await session.get(PriceVersion, first.price_version_id)
            price_file = await session.get(PriceFile, version.price_file_id)
            materialized = await service.materialize_succeeded_sections(
                session,
                version=version,
                price_file=price_file,
                data=data,
                attempts=attempts,
            )
            assert materialized["sections_materialized"] == 49
            assert await session.scalar(select(func.count(SupplierPriceItem.id))) == 49
            assert await session.scalar(select(func.count(SupplierPriceValue.id))) == 49
            assert await session.scalar(
                select(func.count(SupplierPriceItem.id)).where(
                    SupplierPriceItem.source_section_id == attempts[-1].section_id
                )
            ) == 0
        await engine.dispose()

    asyncio.run(scenario())


def test_c_missing_price_falls_back_to_llm() -> None:
    context = {
        "sheet_shape": {"columns": 2},
        "section_kind": "price_rows",
        "price_presence": {
            "all_source_columns_inspected": True,
            "inspected_column_indexes": [0, 1],
            "price_column_indexes": [],
        },
        "deterministic_observations": [
            {"index": 0, "suggested_role": "name", "confidence": 0.99}
        ],
    }
    result = evaluate_section_llm_need(context, loader="xlsx")
    assert result["decision"] == "llm_required"
    assert "missing_price_role" in result["reasons"]


def test_d_unresolved_money_column_falls_back_to_llm() -> None:
    context = {
        "sheet_shape": {"columns": 4},
        "section_kind": "price_rows",
        "price_presence": {
            "all_source_columns_inspected": True,
            "inspected_column_indexes": [0, 1, 2, 3],
            "price_column_indexes": [2, 3],
        },
        "structural_candidates": {"candidate_numeric_column_indexes": [2, 3]},
        "deterministic_observations": [
            {"index": 0, "suggested_role": "name", "confidence": 0.99},
            {
                "index": 3,
                "suggested_role": "price",
                "confidence": 0.99,
                "price_descriptor": {"kind": "retail", "vat_mode": "unknown"},
            },
        ],
    }
    result = evaluate_section_llm_need(context, loader="xlsx")
    assert result["decision"] == "llm_required"
    assert result["unresolved_material_columns"] == [2]
    assert "unresolved_material_column" in result["reasons"]


def test_e_matrix_ambiguity_falls_back_to_llm() -> None:
    result = evaluate_section_llm_need(
        {
            "proposal_contract": "matrix-unfold-v1",
            "loader_scope": "matrix",
            "section_kind": "matrix",
            "sheet_shape": {"columns": 2},
            "price_presence": {
                "all_source_columns_inspected": True,
                "inspected_column_indexes": [0, 1],
            },
        },
        loader="xlsx",
    )
    assert result["decision"] == "llm_required"
    assert "ambiguous_matrix_axis" in result["reasons"]


@pytest.mark.parametrize("failure", ["compiler", "dry_run"])
def test_f_g_local_verifier_failure_falls_back_to_llm(monkeypatch, failure: str) -> None:
    async def scenario() -> None:
        engine, factory = await _database()
        data = f"fast-{failure}".encode()
        grid = _grid()
        async with factory() as session:
            attempt, context = await _add_attempt(
                session, grid=grid, data=data, suffix=data.decode()
            )
            provider = FakeProvider([_proposal(context)])
            _install_runtime(monkeypatch, {data: grid}, provider)
            _enable(monkeypatch, enabled=True)

            if failure == "compiler":
                original = fast_path.handler_for_contract
                handler = original(context.get("proposal_contract"))

                def reject_compiler(*_args, **_kwargs):
                    raise ProposalError("synthetic compiler rejection")

                monkeypatch.setattr(
                    fast_path,
                    "handler_for_contract",
                    lambda _contract: replace(handler, compile=reject_compiler),
                )
            else:
                monkeypatch.setattr(
                    fast_path,
                    "dry_run_decision",
                    lambda *_args, **_kwargs: (
                        type("Result", (), {"items": []})(),
                        {"passed": False},
                    ),
                )

            assert await service.run_rule_generation_attempt(session, attempt.id) == "proposed"
            await session.refresh(attempt)
            assert provider.calls == 1
            assert attempt.validation["fast_path"]["outcome"] == "fallback_to_llm"
            assert await session.scalar(select(func.count(RuleGenerationOperation.id))) == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_j_k_duplicate_delivery_and_saved_artifact_recovery_are_provider_free(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        engine, factory = await _database()
        data = b"fast-recovery"
        grid = _grid()
        async with factory() as session:
            attempt, _ = await _add_attempt(
                session, grid=grid, data=data, suffix=data.decode()
            )
            provider = FakeProvider(error=AssertionError("provider must not be called"))
            _install_runtime(monkeypatch, {data: grid}, provider)
            _enable(monkeypatch, enabled=True)

            assert await service.run_rule_generation_attempt(session, attempt.id) == "proposed"
            artifact_id = attempt.artifact_id
            # Duplicate task delivery returns the terminal local result.
            assert await service.run_rule_generation_attempt(session, attempt.id) == "proposed"
            assert provider.calls == 0
            assert await session.scalar(select(func.count(RuleGenerationArtifact.id))) == 1

            # Simulate crash after artifact commit and before materialization.
            attempt.status = service.ATTEMPT_QUEUED
            await session.commit()
            assert await service.run_rule_generation_attempt(session, attempt.id) == "proposed"
            await session.refresh(attempt)
            assert attempt.artifact_id == artifact_id
            assert provider.calls == 0

            version = await session.get(PriceVersion, attempt.price_version_id)
            price_file = await session.get(PriceFile, version.price_file_id)
            await service.materialize_succeeded_sections(
                session,
                version=version,
                price_file=price_file,
                data=data,
                attempts=[attempt],
            )
            await service.materialize_succeeded_sections(
                session,
                version=version,
                price_file=price_file,
                data=data,
                attempts=[attempt],
            )
            assert await session.scalar(select(func.count(SupplierPriceItem.id))) == 1
            assert await session.scalar(select(func.count(SupplierPriceValue.id))) == 1
        await engine.dispose()

    asyncio.run(scenario())
