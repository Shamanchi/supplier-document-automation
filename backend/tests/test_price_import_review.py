"""Contracts for the import-scoped review workflow."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import AuthUser
from app.main import app
from app.models import (
    Base,
    PriceFile,
    PriceImport,
    PriceImportReview,
    PriceImportReviewEvent,
    PriceImportReviewSection,
    PriceImportReviewStatus,
    PriceVersion,
    RuleGenerationAttempt,
    ServiceClient,
    Supplier,
)
from app.parsing.grid import Cell, SheetGrid
from app.review_service import request_apply
from app.review_validation import (
    dry_run_summary,
    parse_review_draft,
    review_state_hash,
    validate_non_price_decisions,
)
from app.schemas import ReviewCurrencyUpdateIn, ReviewDecisionIn


def _grid() -> SheetGrid:
    return SheetGrid(
        name="Прайс",
        rows=[
            [Cell(value="Наименование"), Cell(value="Цена, KZT")],
            [Cell(value="Товар A"), Cell(value="130-300")],
            [Cell(value="Товар B"), Cell(value="50 тенге за 1 км")],
        ],
    )


def test_review_requests_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ReviewCurrencyUpdateIn.model_validate(
            {"revision": 1, "currency": "KZT", "unsafe": True}
        )


def _invoice_grid() -> SheetGrid:
    """Счёт с шапкой-реквизитами: ИНН и р/с парсятся как числа."""
    return SheetGrid(
        name="Счёт",
        rows=[
            [Cell(value="ООО «Таурус»"), Cell(value="ИНН"), Cell(value="043601607")],
            [Cell(value="р/с"), Cell(value=""), Cell(value="40702810954400017252")],
            [Cell(value="Наименование"), Cell(value="Кол-во"), Cell(value="Цена")],
            [Cell(value="Гидроклапан МКПВ-20"), Cell(value="1"), Cell(value="5786,00")],
        ],
    )


def test_review_state_hash_binds_every_editable_dimension() -> None:
    baseline = review_state_hash(mapping={"tables": []}, recipe=None, decisions=[], currency=None)
    assert baseline != review_state_hash(
        mapping={"tables": []}, recipe=None, decisions=[], currency="KZT"
    )
    assert baseline != review_state_hash(
        mapping={"tables": []},
        recipe=None,
        decisions=[{"sheet": "A", "row_start": 1, "row_end": 1}],
        currency=None,
    )


def test_non_price_decision_cannot_hide_qualified_or_ranged_price() -> None:
    with pytest.raises(Exception) as exc_info:
        validate_non_price_decisions(
            grids=[_grid()],
            decisions=[
                {
                    "sheet": "Прайс",
                    "row_start": 2,
                    "row_end": 3,
                    "panel_index": None,
                    "decision": "catalog_without_price",
                    "reason_code": "supplier_left_price_empty",
                    "comment": "Проверено",
                }
            ],
        )
    assert "UNSAFE_NON_PRICE_DECISION" in str(exc_info.value)


def test_requisites_rows_are_dismissable_without_override() -> None:
    """ИНН и р/с в счёте — не ценовое доказательство (дефект 17.09.2026)."""
    decisions = [
        {
            "sheet": "Счёт",
            "row_start": 1,
            "row_end": 2,
            "panel_index": None,
            "decision": "not_price_section",
            "reason_code": "supplier_card",
            "comment": "Шапка счёта: реквизиты поставщика",
        }
    ]
    validate_non_price_decisions(grids=[_invoice_grid()], decisions=decisions)


def test_requisites_dismissal_does_not_hide_a_real_price() -> None:
    with pytest.raises(HTTPException) as exc_info:
        validate_non_price_decisions(
            grids=[_invoice_grid()],
            decisions=[
                {
                    "sheet": "Счёт",
                    "row_start": 1,
                    "row_end": 4,
                    "panel_index": None,
                    "decision": "not_price_section",
                    "reason_code": "supplier_card",
                    "comment": "Скрыть весь блок вместе с позицией",
                }
            ],
        )
    assert "UNSAFE_NON_PRICE_DECISION" in str(exc_info.value.detail)


def _residual_requisites_grid() -> SheetGrid:
    """Шапка счёта, которую правдоподобие не отсекает: ИНН без ведущего нуля."""
    return SheetGrid(
        name="Счёт",
        rows=[
            [Cell(value="ООО «Таурус»"), Cell(value="ИНН"), Cell(value="7707083893")],
            [Cell(value="Наименование"), Cell(value="Кол-во"), Cell(value="Цена")],
            [Cell(value="Гидроклапан МКПВ-20"), Cell(value="1"), Cell(value="5786,00")],
        ],
    )


def test_override_guard_requires_a_concrete_reason() -> None:
    """«Проверено оператором» — не причина обходить защиту от скрытия цены."""
    with pytest.raises(ValidationError):
        ReviewDecisionIn.model_validate(
            {
                "sheet": "Счёт",
                "row_start": 1,
                "row_end": 1,
                "panel_index": None,
                "decision": "not_price_section",
                "reason_code": "operator_review",
                "comment": "Шапка счёта",
                "override_guard": True,
            }
        )


def test_residual_requisites_block_without_override() -> None:
    """Остаточный случай (10 разрядов без ведущего нуля) — ровно для override."""
    with pytest.raises(HTTPException) as exc_info:
        validate_non_price_decisions(
            grids=[_residual_requisites_grid()],
            decisions=[
                {
                    "sheet": "Счёт",
                    "row_start": 1,
                    "row_end": 1,
                    "panel_index": None,
                    "decision": "not_price_section",
                    "reason_code": "supplier_card",
                    "comment": "Шапка счёта: ИНН поставщика",
                }
            ],
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "UNSAFE_NON_PRICE_DECISION"
    assert exc_info.value.detail["evidence"][0]["value"] == "7707083893"


def test_guard_override_is_explicit_and_audited(monkeypatch) -> None:
    """Подтверждённый обход проходит, но требует флага и попадает в журнал."""
    from app.review_service import update_decisions

    monkeypatch.setattr("app.review_service.storage.get_price_file", lambda _key: b"fixture")
    monkeypatch.setattr(
        "app.review_service.load_source", lambda _data, _name: ("xlsx", [_residual_requisites_grid()])
    )
    decision = {
        "sheet": "Счёт",
        "row_start": 1,
        "row_end": 1,
        "panel_index": None,
        "decision": "not_price_section",
        "reason_code": "supplier_card",
        "comment": "Шапка счёта: ИНН поставщика",
    }

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            client = ServiceClient(
                name="UI", key_prefix="override-test", key_digest="0" * 64, is_internal=True
            )
            supplier = Supplier(code="override-test", name="Override test")
            session.add_all([client, supplier])
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="invoice.pdf",
                storage_key="stub/invoice.pdf",
                sha256="a" * 64,
                size_bytes=1,
                mime="application/pdf",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id, price_file_id=price_file.id, version_no=1
            )
            session.add(version)
            await session.flush()
            price_import = PriceImport(
                service_client_id=client.id,
                supplier_id=supplier.id,
                price_file_id=price_file.id,
                price_version_id=version.id,
                idempotency_key="override-idempotency",
                request_fingerprint="b" * 64,
                status="review_required",
                stage="review_required",
                progress_percent=100,
            )
            session.add(price_import)
            await session.flush()
            review = PriceImportReview(
                price_import_id=price_import.id,
                revision=1,
                status=PriceImportReviewStatus.draft.value,
                created_by="admin",
                updated_by="admin",
            )
            session.add(review)
            await session.commit()
            actor = AuthUser("admin", "admin")

            with pytest.raises(HTTPException) as exc_info:
                await update_decisions(
                    session,
                    price_import=price_import,
                    revision=1,
                    decisions=[dict(decision)],
                    actor=actor,
                )
            assert exc_info.value.status_code == 422
            assert (await session.get(PriceImportReview, review.id)).revision == 1

            updated = await update_decisions(
                session,
                price_import=price_import,
                revision=1,
                decisions=[{**decision, "override_guard": True}],
                actor=actor,
            )
            assert updated.revision == 2
            assert updated.section_decisions[0]["override_guard"] is True

            events = (
                await session.execute(
                    select(PriceImportReviewEvent).where(
                        PriceImportReviewEvent.price_import_review_id == review.id
                    )
                )
            ).scalars().all()
            assert {event.event_type for event in events} == {
                "decisions_updated",
                "guard_overridden",
            }
            overridden = next(
                event for event in events if event.event_type == "guard_overridden"
            )
            assert overridden.revision == 2
            assert overridden.actor == "admin"
            assert overridden.payload["evidence"] == [
                {"sheet": "Счёт", "row": 1, "column": 3}
            ]
            assert overridden.payload["reason_codes"] == ["supplier_card"]
        await engine.dispose()

    asyncio.run(scenario())


def test_typed_rule_dry_run_preserves_range_and_rate_semantics(monkeypatch) -> None:
    from app.review_validation import loaders

    monkeypatch.setattr(loaders, "detect_loader", lambda *_args: loaders.LOADER_XLSX)
    monkeypatch.setattr(loaders, "load_grids_for", lambda *_args: [_grid()])
    recipe = {
        "sheets": [
            {
                "match": {"name_pattern": "Прайс", "or_index": 0},
                "skip": False,
                "header_row": 0,
                "allow_qualified_prices": True,
                "columns": [
                    {"index": 0, "role": "name", "header": "Наименование"},
                    {"index": 1, "role": "price", "header": "Цена, KZT"},
                ],
            }
        ],
        "options": {},
    }
    _loader, grids, result = parse_review_draft(
        data=b"fixture", filename="fixture.xlsx", mapping=None, recipe=recipe
    )
    summary = dry_run_summary(
        result=result,
        grids=grids,
        decisions=[],
        currency_override="KZT",
        current_item_count=0,
    )
    assert summary["metrics"]["item_count"] == 2
    assert summary["metrics"]["price_count"] == 2


def test_review_is_its_own_resource_with_a_single_draft_patch() -> None:
    """Ручная проверка живёт на `/review`, а черновик правится одним PATCH.

    Четыре прежних PUT (`mapping`, `rule`, `decisions`, `currency`) меняли один
    и тот же черновик под одной ревизией, поэтому существовать по отдельности
    им незачем.
    """

    paths = app.openapi()["paths"]
    routes = {(path, method.upper()) for path, operations in paths.items() for method in operations}
    expected = {
        ("/api/v1/review/queue", "GET"),
        ("/api/v1/review/{import_id}", "GET"),
        ("/api/v1/review/{import_id}", "PATCH"),
        ("/api/v1/review/{import_id}/sections/{section_id}", "GET"),
        ("/api/v1/review/{import_id}/validate", "POST"),
        ("/api/v1/review/{import_id}/apply", "POST"),
        ("/api/v1/review/{import_id}/activate-rule", "POST"),
    }
    assert expected <= routes
    assert not [path for path, _ in routes if path.startswith("/api/v1/price-imports/{import_id}/review")]


def test_duplicate_apply_of_same_revision_does_not_enqueue_again() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            client = ServiceClient(
                name="UI", key_prefix="review-test", key_digest="0" * 64, is_internal=True
            )
            supplier = Supplier(code="review-supplier", name="Review supplier")
            session.add_all([client, supplier])
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="review.xlsx",
                storage_key="test/review.xlsx",
                sha256="a" * 64,
                size_bytes=1,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id, price_file_id=price_file.id, version_no=1
            )
            session.add(version)
            await session.flush()
            price_import = PriceImport(
                service_client_id=client.id,
                supplier_id=supplier.id,
                price_file_id=price_file.id,
                price_version_id=version.id,
                idempotency_key="review-idempotency",
                request_fingerprint="b" * 64,
                status="reparsing",
                stage="reparsing",
                progress_percent=75,
            )
            session.add(price_import)
            await session.flush()
            review = PriceImportReview(
                price_import_id=price_import.id,
                revision=3,
                status=PriceImportReviewStatus.applying.value,
                last_applied_revision=3,
                created_by="admin",
                updated_by="admin",
            )
            session.add(review)
            await session.commit()
            returned, enqueue = await request_apply(
                session,
                price_import=price_import,
                revision=3,
                actor=AuthUser("admin", "admin"),
            )
            assert returned.id == review.id
            assert enqueue is False
        await engine.dispose()

    asyncio.run(scenario())



def test_snapshot_repairs_unopened_review_required_import(monkeypatch) -> None:
    """Scenario E (snapshot): opening the review snapshot of an import that was
    never opened creates the review row plus durable section rows; repeating
    the snapshot keeps the same review and does not duplicate sections."""

    from app.review_service import build_review_snapshot

    def _missing_blob(*_args, **_kwargs):
        raise HTTPException(status_code=404, detail="stub: source blob missing")

    monkeypatch.setattr("app.storage.get_price_file", _missing_blob)

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            client = ServiceClient(
                name="UI", key_prefix="snapshot-repair", key_digest="0" * 64, is_internal=True
            )
            supplier = Supplier(code="snapshot-repair", name="Snapshot repair")
            session.add_all([client, supplier])
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="snapshot.xlsx",
                storage_key="stub/snapshot.xlsx",
                sha256="a" * 64,
                size_bytes=1,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id, price_file_id=price_file.id, version_no=1
            )
            session.add(version)
            await session.flush()
            price_import = PriceImport(
                service_client_id=client.id,
                supplier_id=supplier.id,
                price_file_id=price_file.id,
                price_version_id=version.id,
                idempotency_key="snapshot-repair-idempotency",
                request_fingerprint="b" * 64,
                status="review_required",
                stage="review_required",
                progress_percent=100,
            )
            session.add(price_import)
            await session.flush()
            session.add(
                RuleGenerationAttempt(
                    price_version_id=version.id,
                    price_import_id=price_import.id,
                    batch_id=uuid.uuid4(),
                    section_id="sec_snapshot",
                    status="failed",
                    provider="yandexgpt",
                    model="stub",
                    request_settings={},
                    prompt_version="v1",
                    prompt_id="prompt-1",
                    source_file_sha256="c" * 64,
                    source_context_hash="d" * 64,
                    input_context={
                        "section_id": "sec_snapshot",
                        "sheet_name": "Price",
                        "section_kind": "price_rows",
                        "section_start_row_index": 1,
                        "section_end_row_index": 3,
                        "header_row_index": 0,
                        "header_values": ["Name", "Price"],
                        "sample_rows": [],
                        "horizontal_panels": [],
                    },
                    prompt="prompt",
                )
            )
            await session.commit()

            snapshot = await build_review_snapshot(
                session, price_import=price_import, actor="admin", can_edit=True
            )
            assert snapshot["review_id"] is not None
            assert [row["section_id"] for row in snapshot["sections"]] == ["sec_snapshot"]
            review = await session.get(PriceImportReview, snapshot["review_id"])
            assert review is not None
            assert review.created_by == "admin"
            section_rows = (
                await session.execute(
                    select(PriceImportReviewSection).where(
                        PriceImportReviewSection.price_import_review_id == review.id
                    )
                )
            ).scalars().all()
            assert [row.section_id for row in section_rows] == ["sec_snapshot"]

            repeated = await build_review_snapshot(
                session, price_import=price_import, actor="admin", can_edit=True
            )
            assert repeated["review_id"] == snapshot["review_id"]
            assert (
                await session.scalar(
                    select(func.count()).select_from(PriceImportReviewSection)
                )
            ) == 1
        await engine.dispose()

    asyncio.run(scenario())
