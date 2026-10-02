from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from hashlib import sha256
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import (
    Base,
    PriceFile,
    PriceImport,
    PriceImportStatus,
    PriceVersion,
    RuleGenerationArtifact,
    RuleGenerationAttempt,
    ServiceClient,
    Supplier,
    SupplierPriceItem,
    SupplierPriceValue,
    VersionStatus,
)
from app.parsing.grid import Cell, SheetGrid
from app.parsing.pipeline import ParseResult, ParsedItem, ParsedValue
from app.parsing.rule_proposal import context_hash
from app.parsing.rule_proposal_handlers import build_rule_section_contexts


async def _database():
    engine = create_async_engine("sqlite+aiosqlite://")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, factory


async def _version(session) -> tuple[PriceVersion, PriceFile]:
    supplier = Supplier(code="partial-materialization", name="Synthetic supplier")
    session.add(supplier)
    await session.flush()
    price_file = PriceFile(
        supplier_id=supplier.id,
        original_filename="synthetic.xlsx",
        storage_key="synthetic/partial",
        sha256=sha256(b"source").hexdigest(),
        size_bytes=len(b"source"),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    session.add(price_file)
    await session.flush()
    version = PriceVersion(
        supplier_id=supplier.id,
        price_file_id=price_file.id,
        version_no=1,
        status=VersionStatus.needs_mapping.value,
    )
    session.add(version)
    await session.flush()
    return version, price_file


def _attempt(version: PriceVersion, batch_id: uuid.UUID, index: int, status: str):
    section_id = f"sec_{index:02d}"
    return RuleGenerationAttempt(
        price_version_id=version.id,
        batch_id=batch_id,
        section_id=section_id,
        status=status,
        provider="mock",
        model="mock-model",
        request_settings={},
        prompt_version="synthetic-v1",
        prompt_id=f"prompt-{index}",
        source_file_sha256=sha256(b"source").hexdigest(),
        source_context_hash=f"{index:064x}",
        input_context={"section_id": section_id},
        prompt="synthetic",
        proposal={"decision": "propose"},
        validation={"passed": True},
    )


def _result(section_id: str, index: int) -> ParseResult:
    item = ParsedItem(
        source_name=f"Product {index}",
        normalized_name=f"product {index}",
        source_article=f"A-{index}",
        normalized_article=f"a-{index}",
        unit_code="шт",
        category_path="Synthetic category",
        dimensions=None,
        description=None,
        attribute_values=[],
        source_sheet="Synthetic sheet",
        source_row=index + 1,
        source_section_id=section_id,
        source_panel_index=0,
        source_column_start=0,
        source_column_end=2,
        source_header_cells=[[0, "Name"], [1, "Price"]],
        source_data_cells=[[0, f"Product {index}"], [1, str(100 + index)]],
        source_category_cells=[[[0, "Synthetic category"]]],
        values=[
            ParsedValue(
                kind="retail",
                amount=Decimal(100 + index),
                vat_mode="unknown",
                is_on_request=False,
                source_label="Price",
                currency="RUB",
                currency_source="document",
                raw_value=str(100 + index),
                source_column_index=1,
            )
        ],
    )
    return ParseResult(items=[item], rows_accepted=1, coordinate_coverage_verified=True)


async def _counts(session, version_id: uuid.UUID) -> tuple[int, int]:
    items = await session.scalar(
        select(func.count(SupplierPriceItem.id)).where(
            SupplierPriceItem.price_version_id == version_id
        )
    )
    prices = await session.scalar(
        select(func.count(SupplierPriceValue.id))
        .join(SupplierPriceItem)
        .where(SupplierPriceItem.price_version_id == version_id)
    )
    return int(items or 0), int(prices or 0)


def test_47_2_1_materializes_successes_then_only_newly_recovered_sections(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        from app import rule_proposal_service as service
        from app.rule_proposal_service import materialize_succeeded_sections

        engine, factory = await _database()
        try:
            async with factory() as session:
                version, price_file = await _version(session)
                batch_id = uuid.uuid4()
                attempts = [
                    _attempt(version, batch_id, index, "succeeded")
                    for index in range(47)
                ]
                attempts.extend(
                    _attempt(version, batch_id, index, "retryable_failed")
                    for index in range(47, 49)
                )
                attempts.append(_attempt(version, batch_id, 49, "needs_review"))
                session.add_all(attempts)
                await session.commit()

                compile_calls: list[str] = []

                def compile_result(*, attempt, **_kwargs):
                    compile_calls.append(attempt.section_id)
                    return _result(attempt.section_id, int(attempt.section_id[-2:]))

                async def forbidden_provider(**_kwargs):  # pragma: no cover
                    raise AssertionError("partial materialization must not call LLM")

                monkeypatch.setattr(
                    service, "_compile_succeeded_attempt_result", compile_result
                )
                monkeypatch.setattr(
                    service, "_request_rule_proposal_result", forbidden_provider
                )
                monkeypatch.setattr(service.loaders, "detect_loader", lambda *_: "xlsx")
                monkeypatch.setattr(
                    service.loaders,
                    "load_grids_for",
                    lambda *_: [SheetGrid(name="Synthetic sheet", rows=[[Cell("x")]])],
                )

                first = await materialize_succeeded_sections(
                    session,
                    version=version,
                    price_file=price_file,
                    data=b"source",
                    attempts=attempts,
                )
                assert first["sections_materialized"] == 47
                assert await _counts(session, version.id) == (47, 47)
                assert version.status == VersionStatus.needs_mapping.value

                duplicate = await materialize_succeeded_sections(
                    session,
                    version=version,
                    price_file=price_file,
                    data=b"source",
                    attempts=attempts,
                )
                assert duplicate["sections_materialized"] == 0
                assert len(compile_calls) == 47
                assert await _counts(session, version.id) == (47, 47)

                for attempt in attempts[47:49]:
                    attempt.status = "succeeded"
                await session.commit()
                retry = await materialize_succeeded_sections(
                    session,
                    version=version,
                    price_file=price_file,
                    data=b"source",
                    attempts=attempts,
                )
                assert retry["sections_materialized"] == 2
                assert compile_calls[-2:] == ["sec_47", "sec_48"]
                assert await _counts(session, version.id) == (49, 49)

                attempts[49].status = "succeeded"
                await session.commit()
                final = await materialize_succeeded_sections(
                    session,
                    version=version,
                    price_file=price_file,
                    data=b"source",
                    attempts=attempts,
                )
                assert final["sections_materialized"] == 1
                assert compile_calls[-1] == "sec_49"
                assert await _counts(session, version.id) == (50, 50)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_section_replace_is_idempotent_and_preserves_source_provenance() -> None:
    async def scenario() -> None:
        from app.parse_service import persist_partial_section_result

        engine, factory = await _database()
        try:
            async with factory() as session:
                version, _ = await _version(session)
                result = _result("sec_provenance", 7)

                first = await persist_partial_section_result(
                    session, version=version, section_id="sec_provenance", result=result
                )
                await session.commit()
                second = await persist_partial_section_result(
                    session, version=version, section_id="sec_provenance", result=result
                )
                await session.commit()

                assert first["items"] == second["items"] == 1
                assert await _counts(session, version.id) == (1, 1)
                item = await session.scalar(select(SupplierPriceItem))
                value = await session.scalar(select(SupplierPriceValue))
                assert item.source_section_id == "sec_provenance"
                assert item.source_sheet == "Synthetic sheet"
                assert item.source_row == 8
                assert item.source_column_start == 0
                assert item.source_column_end == 2
                assert item.source_header_cells == [[0, "Name"], [1, "Price"]]
                assert item.source_data_cells == [[0, "Product 7"], [1, "107"]]
                assert value.source_column_index == 1
                assert value.raw_value == "107"
                assert version.status == VersionStatus.needs_mapping.value
                assert version.quality_score is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failed_status",
    [
        "retryable_failed",
        "needs_review",
        "rejected",
        "permanent_failed",
        "leased",
    ],
)
def test_failed_section_never_replaces_successful_section_data(
    monkeypatch, failed_status: str
) -> None:
    async def scenario() -> None:
        from app import rule_proposal_service as service
        from app.rule_proposal_service import materialize_succeeded_sections

        engine, factory = await _database()
        try:
            async with factory() as session:
                version, price_file = await _version(session)
                batch_id = uuid.uuid4()
                success = _attempt(version, batch_id, 1, "succeeded")
                rejected = _attempt(version, batch_id, 2, failed_status)
                session.add_all([success, rejected])
                await session.commit()
                seen: list[str] = []

                def compile_result(*, attempt, **_kwargs):
                    seen.append(attempt.section_id)
                    return _result(attempt.section_id, int(attempt.section_id[-2:]))

                monkeypatch.setattr(
                    service, "_compile_succeeded_attempt_result", compile_result
                )
                monkeypatch.setattr(service.loaders, "detect_loader", lambda *_: "xlsx")
                monkeypatch.setattr(
                    service.loaders,
                    "load_grids_for",
                    lambda *_: [SheetGrid(name="Synthetic sheet", rows=[[Cell("x")]])],
                )

                await materialize_succeeded_sections(
                    session,
                    version=version,
                    price_file=price_file,
                    data=b"source",
                    attempts=[success, rejected],
                )
                assert seen == ["sec_01"]
                assert await _counts(session, version.id) == (1, 1)
                assert await session.scalar(
                    select(func.count(SupplierPriceItem.id)).where(
                        SupplierPriceItem.source_section_id == "sec_02"
                    )
                ) == 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_partial_materialization_rejects_cross_section_result() -> None:
    async def scenario() -> None:
        from app.parse_service import persist_partial_section_result

        engine, factory = await _database()
        try:
            async with factory() as session:
                version, _ = await _version(session)
                with pytest.raises(ValueError, match="section"):
                    await persist_partial_section_result(
                        session,
                        version=version,
                        section_id="sec_expected",
                        result=_result("sec_other", 1),
                    )
                assert await _counts(session, version.id) == (0, 0)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_saved_artifact_is_recompiled_without_provider_and_keeps_excel_provenance(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        from app import rule_proposal_service as service
        from app.rule_proposal_service import materialize_succeeded_sections

        engine, factory = await _database()
        try:
            async with factory() as session:
                version, price_file = await _version(session)
                grid = SheetGrid(
                    name="Verified sheet",
                    rows=[
                        [Cell("Наименование"), Cell("Розничная цена")],
                        [Cell("Клапан синтетический"), Cell("1250")],
                    ],
                )
                context = build_rule_section_contexts([grid])[0]
                proposal = {
                    "schema_version": "price-rule-proposal-v1",
                    "decision": "propose",
                    "rule_name": "Synthetic verified section",
                    "loader": "xlsx",
                    "source_section_id": context["section_id"],
                    "mapping": {
                        "sheet_name": grid.name,
                        "header_row_index": context["header_row_index"],
                        "columns": [
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
                                "role": "price",
                                "price_descriptor": {
                                    "kind": "retail",
                                    "vat_mode": "unknown",
                                },
                                "attribute_code": None,
                                "attribute_value_kind": None,
                                "canonical_unit": None,
                            },
                        ],
                    },
                    "discovered_attributes": [],
                    "evidence": [
                        {"row_index": 0, "column_index": 0, "note": "name header"},
                        {"row_index": 0, "column_index": 1, "note": "price header"},
                    ],
                    "unresolved": [],
                    "confidence": 0.99,
                    "requires_manual_review": True,
                }
                artifact = RuleGenerationArtifact(
                    work_key="w" * 64,
                    provider="mock",
                    model="mock-model",
                    request_settings_hash="r" * 64,
                    source_file_sha256=price_file.sha256,
                    source_context_hash=context_hash(context),
                    prompt_version="price-rule-proposal-v1",
                    proposal=proposal,
                    validation={"passed": True},
                    compiler_dry_run_summary={"passed": True},
                    provenance="model_verified",
                )
                session.add(artifact)
                await session.flush()
                attempt = RuleGenerationAttempt(
                    price_version_id=version.id,
                    batch_id=uuid.uuid4(),
                    section_id=context["section_id"],
                    status="succeeded",
                    provider="mock",
                    model="mock-model",
                    request_settings={},
                    prompt_version="price-rule-proposal-v1",
                    prompt_id="artifact-recovery",
                    source_file_sha256=price_file.sha256,
                    source_context_hash=context_hash(context),
                    input_context=context,
                    prompt="synthetic",
                    proposal=None,
                    validation={"passed": True},
                    artifact_id=artifact.id,
                )
                session.add(attempt)
                await session.commit()

                async def forbidden_provider(**_kwargs):  # pragma: no cover
                    raise AssertionError("stored artifact recovery called provider")

                monkeypatch.setattr(
                    service, "_request_rule_proposal_result", forbidden_provider
                )
                monkeypatch.setattr(service.loaders, "detect_loader", lambda *_: "xlsx")
                monkeypatch.setattr(
                    service.loaders, "load_grids_for", lambda *_: [grid]
                )

                result = await materialize_succeeded_sections(
                    session,
                    version=version,
                    price_file=price_file,
                    data=b"source",
                    attempts=[attempt],
                )

                assert result["sections_materialized"] == 1
                assert await _counts(session, version.id) == (1, 1)
                item = await session.scalar(select(SupplierPriceItem))
                value = await session.scalar(select(SupplierPriceValue))
                assert item.source_section_id == context["section_id"]
                assert item.source_sheet == "Verified sheet"
                assert item.source_row == 2
                assert item.source_data_cells == [
                    [0, "Клапан синтетический"],
                    [1, "1250"],
                ]
                assert value.source_column_index == 1
                assert value.amount == Decimal("1250")
                assert attempt.validation["partial_materialization"]["completed"] is True
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_import_remains_review_required_with_successful_items_visible_in_db(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        from app import import_service, parse_service, rule_proposal_service

        engine, factory = await _database()
        try:
            async with factory() as session:
                version, price_file = await _version(session)
                client = ServiceClient(
                    name="Partial import client",
                    key_prefix="partial-import",
                    key_digest="d" * 64,
                )
                session.add(client)
                await session.flush()
                price_import = PriceImport(
                    service_client_id=client.id,
                    supplier_id=version.supplier_id,
                    price_file_id=price_file.id,
                    price_version_id=version.id,
                    idempotency_key="partial-import",
                    request_fingerprint="f" * 64,
                    status=PriceImportStatus.generating_rule.value,
                    stage=PriceImportStatus.generating_rule.value,
                    rule_batch_id=uuid.uuid4(),
                )
                session.add(price_import)
                await session.flush()
                success = _attempt(version, price_import.rule_batch_id, 1, "succeeded")
                success.price_import_id = price_import.id
                rejected = _attempt(version, price_import.rule_batch_id, 2, "needs_review")
                rejected.price_import_id = price_import.id
                session.add_all([success, rejected])
                await session.commit()

                compile_calls: list[str] = []

                def compile_result(*, attempt, **_kwargs):
                    compile_calls.append(attempt.section_id)
                    return _result(attempt.section_id, 1)

                monkeypatch.setattr(
                    rule_proposal_service,
                    "_compile_succeeded_attempt_result",
                    compile_result,
                )
                monkeypatch.setattr(
                    rule_proposal_service.loaders,
                    "detect_loader",
                    lambda *_: "xlsx",
                )
                monkeypatch.setattr(
                    rule_proposal_service.loaders,
                    "load_grids_for",
                    lambda *_: [SheetGrid(name="Synthetic sheet", rows=[[Cell("x")]])],
                )
                monkeypatch.setattr(
                    import_service.storage, "get_price_file", lambda _key: b"source"
                )

                status = await import_service.advance_price_import(
                    session, price_import.id
                )

                await session.refresh(price_import)
                await session.refresh(version)
                assert status == PriceImportStatus.review_required.value
                assert price_import.status == PriceImportStatus.review_required.value
                assert version.status == VersionStatus.needs_mapping.value
                assert version.item_count == 1
                assert await _counts(session, version.id) == (1, 1)
                partial = price_import.completeness_summary[
                    "partial_materialization"
                ]
                assert partial["sections_materialized"] == 1
                assert price_import.completeness_summary[
                    "final_validation_complete"
                ] is False

                final_steps: list[str] = []

                async def create_final_rule(*_args, **_kwargs):
                    final_steps.append("create_rule")
                    return SimpleNamespace(
                        id=uuid.uuid4(),
                        project_id=price_file.project_id,
                        supplier_id=version.supplier_id,
                        loader="xlsx",
                        validation={
                            "passed": True,
                            "item_count": 2,
                            "metrics": {
                                "rows_accepted": 2,
                                "items_with_price": 2,
                                "errors_count": 0,
                                "warnings_count": 0,
                                "quality_score": 1.0,
                            },
                        },
                    )

                async def forbid_global_reparse(*_args, **_kwargs):
                    raise AssertionError(
                        "final validation must not recreate previous section rows"
                    )

                async def final_quality(session, *, price_import, version, **_kwargs):
                    final_steps.append("quality_gate")
                    session.add(
                        import_service.transition(
                            price_import, PriceImportStatus.completed.value
                        )
                    )
                    await session.commit()
                    return PriceImportStatus.completed.value

                monkeypatch.setattr(
                    import_service,
                    "create_model_draft_from_batch",
                    create_final_rule,
                )
                monkeypatch.setattr(
                    parse_service, "apply_parse_rule_to_version", forbid_global_reparse
                )
                monkeypatch.setattr(import_service, "_quality_gate", final_quality)
                rejected.status = "succeeded"
                rejected.proposal = {"decision": "propose"}
                price_import.status = PriceImportStatus.generating_rule.value
                price_import.stage = PriceImportStatus.generating_rule.value
                price_import.finished_at = None
                await session.commit()

                final_status = await import_service.advance_price_import(
                    session, price_import.id
                )

                assert final_status == PriceImportStatus.completed.value
                assert compile_calls == ["sec_01", "sec_02"]
                assert final_steps == ["create_rule", "quality_gate"]
                assert await _counts(session, version.id) == (2, 2)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_celery_finalizer_runs_partial_coordinator_while_neighbours_are_pending(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        from app import import_service, tasks

        import_id = uuid.uuid4()
        price_import = SimpleNamespace(
            id=import_id,
            status=PriceImportStatus.generating_rule.value,
        )

        class Session:
            async def scalar(self, _stmt):
                return price_import

        class Engine:
            async def dispose(self):
                return None

        @asynccontextmanager
        async def session_context():
            yield Session()

        class Factory:
            def __call__(self):
                return session_context()

        async def batch_state(*_args, **_kwargs):
            return SimpleNamespace(total_count=50, pending_count=3)

        coordinator_calls: list[uuid.UUID] = []

        async def coordinator(_session, selected_import_id):
            coordinator_calls.append(selected_import_id)
            return PriceImportStatus.generating_rule.value

        monkeypatch.setattr(tasks, "make_task_engine", lambda: (Engine(), Factory()))
        monkeypatch.setattr(tasks, "inspect_batch", batch_state)
        monkeypatch.setattr(import_service, "advance_price_import", coordinator)

        status = await tasks._finalize_rule_batch(str(uuid.uuid4()))

        assert status == PriceImportStatus.generating_rule.value
        assert coordinator_calls == [import_id]

    asyncio.run(scenario())
