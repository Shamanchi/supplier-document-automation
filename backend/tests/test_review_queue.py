"""Section-level operator review queue contracts."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.main import app
from app.models import (
    Base,
    PriceFile,
    PriceImport,
    PriceImportReview,
    PriceImportReviewSection,
    PriceImportReviewSectionStatus,
    PriceVersion,
    Project,
    RuleGenerationAttempt,
    ServiceClient,
    Supplier,
)
from app.review_service import (
    ensure_review_required_invariant,
    section_context_hash,
    sync_review_sections,
    update_review_section_state,
)
from app.review_bootstrap import select_retry_entries
from app.schemas import ReviewSectionDetail


def _context(section_id: str = "sec_example") -> dict:
    return {
        "section_id": section_id,
        "sheet_name": "Прайс",
        "sheet_index": 0,
        "section_kind": "price_rows",
        "section_start_row_index": 1,
        "section_end_row_index": 3,
        "header_row_index": 0,
        "header_values": ["Наименование", "Цена"],
        "header_rows": [
            {
                "row_index": 0,
                "cells": [
                    {"value": "Наименование", "is_bold": True, "is_merged_fill": False},
                    {"value": "Цена", "is_bold": True, "is_merged_fill": False},
                ],
            }
        ],
        "sample_rows": [
            {
                "row_index": 1,
                "cells": [
                    {"value": "Товар", "is_bold": False, "is_merged_fill": False},
                    {"value": "100", "is_bold": False, "is_merged_fill": False},
                ],
            }
        ],
        "horizontal_panels": [],
    }


def test_queue_lives_on_the_review_resource() -> None:
    """`/review/queue` — литеральный путь в собственном префиксе.

    Пока очередь висела на `/price-imports/review-queue`, её объявление
    обязано было стоять выше `/price-imports/{import_id}`, иначе FastAPI
    разбирал слово `review-queue` как идентификатор импорта.
    """

    paths = app.openapi()["paths"]
    assert "/api/v1/review/queue" in paths
    assert "/api/v1/review/{import_id}/sections/{section_id}" in paths


def test_section_detail_exposes_excel_coordinates() -> None:
    detail = ReviewSectionDetail.from_context(
        import_id="00000000-0000-0000-0000-000000000001",
        review_id="00000000-0000-0000-0000-000000000002",
        revision=1,
        context=_context(),
        mapping=None,
        decisions=[],
        attempt=None,
        validation=None,
        actions={"can_edit": False},
    )
    assert detail.rows[0].excel_row == 1
    assert detail.rows[0].cells[0].excel_column == "A"
    assert detail.rows[1].excel_row == 2
    assert detail.rows[1].cells[1].excel_column == "B"


def test_sync_review_sections_is_idempotent() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            client = ServiceClient(
                name="UI", key_prefix="review-queue", key_digest="0" * 64, is_internal=True
            )
            supplier = Supplier(code="review-queue", name="Review queue")
            session.add_all([client, supplier])
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="queue.xlsx",
                storage_key="test/queue.xlsx",
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
                idempotency_key="review-queue-idempotency",
                request_fingerprint="b" * 64,
                status="review_required",
                stage="review_required",
                progress_percent=100,
            )
            session.add(price_import)
            await session.flush()
            review = PriceImportReview(
                price_import_id=price_import.id,
                created_by="admin",
                updated_by="admin",
            )
            session.add(review)
            await session.flush()

            first = await sync_review_sections(
                session, review=review, contexts=[_context()], attempts=[]
            )
            second = await sync_review_sections(
                session, review=review, contexts=[_context()], attempts=[]
            )
            review.section_decisions = [
                {
                    "sheet": "Прайс",
                    "row_start": 2,
                    "row_end": 2,
                    "panel_index": None,
                    "decision": "reject",
                }
            ]
            await update_review_section_state(
                session,
                review=review,
                status=PriceImportReviewSectionStatus.in_progress,
                actor="admin",
            )
            await session.commit()

            count = await session.scalar(select(func.count()).select_from(PriceImportReviewSection))
            assert count == 1
            assert first[0].id == second[0].id
            assert first[0].source_context_hash == section_context_hash(_context())
            assert first[0].status == "in_progress"
            assert first[0].decision_summary == {"count": 1, "accepted": 0, "rejected": 1}
        await engine.dispose()

    asyncio.run(scenario())


def test_retry_selector_builds_exact_pending_queue() -> None:
    checkpoint = {
        "requests": [
            {"price": "xls/a.xls", "section_id": "sec_a", "passed": False},
            {"price": "xls/b.xls", "section_id": "sec_b", "passed": False},
            {"price": "xls/c.xls", "section_id": "sec_c", "passed": False},
        ]
    }
    telemetry = [
        {"dimensions": {"price": "xls/old.xls", "section_id": "sec_old"}},
        {"dimensions": {"price": "xls/a.xls", "section_id": "sec_a"}},
        {"dimensions": {"price": "xls/b.xls", "section_id": "sec_b"}},
        {"dimensions": {"price": "xls/c.xls", "section_id": "sec_c"}},
    ]
    selected = select_retry_entries(
        checkpoint,
        telemetry,
        operation_count=3,
        excluded_section_ids={"sec_b"},
    )
    assert [(row["price"], row["section_id"]) for row in selected] == [
        ("xls/a.xls", "sec_a"),
        ("xls/c.xls", "sec_c"),
    ]


async def _seed_review_required_import(
    session, *, project_id, section_ids, error_code=None
):
    client = ServiceClient(
        name="UI", key_prefix="queue-repair", key_digest="0" * 64, is_internal=True
    )
    supplier = Supplier(code="queue-repair", name="Queue repair supplier")
    session.add_all([client, supplier])
    await session.flush()
    price_file = PriceFile(
        supplier_id=supplier.id,
        project_id=project_id,
        original_filename="historical.xlsx",
        storage_key="stub/historical.xlsx",
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
        idempotency_key="queue-repair-idempotency",
        request_fingerprint="b" * 64,
        status="review_required",
        stage="review_required",
        progress_percent=100,
        error_code=error_code,
    )
    session.add(price_import)
    await session.flush()
    for index, section_id in enumerate(section_ids):
        session.add(
            RuleGenerationAttempt(
                price_version_id=version.id,
                price_import_id=price_import.id,
                batch_id=uuid.uuid4(),
                section_id=section_id,
                status="failed",
                provider="yandexgpt",
                model="stub",
                request_settings={},
                prompt_version="v1",
                prompt_id=f"prompt-{index}",
                source_file_sha256="c" * 64,
                source_context_hash="d" * 64,
                input_context=_context(section_id),
                prompt="prompt",
            )
        )
    await session.commit()
    return price_import.id


def test_ensure_review_required_invariant_repairs_historical_import(monkeypatch) -> None:
    """Scenario E (unit): queue-scope repair materialises a review plus section
    rows for a review_required import that was never opened; repeated calls
    stay idempotent."""

    def _missing_blob(*_args, **_kwargs):
        raise HTTPException(status_code=404, detail="stub: source blob missing")

    monkeypatch.setattr("app.storage.get_price_file", _missing_blob)

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            project = Project(name="Queue project", slug="queue-project")
            session.add(project)
            await session.commit()
            import_id = await _seed_review_required_import(
                session,
                project_id=project.id,
                section_ids=["sec_alpha", "sec_beta"],
            )
            assert (
                await session.scalar(select(func.count()).select_from(PriceImportReview))
            ) == 0

            repaired = await ensure_review_required_invariant(
                session, project_id=project.id, actor="viewer"
            )
            assert repaired == 1
            review = (
                await session.execute(
                    select(PriceImportReview).where(
                        PriceImportReview.price_import_id == import_id
                    )
                )
            ).scalar_one()
            assert review.created_by == "viewer"
            section_ids = set(
                (
                    await session.execute(
                        select(PriceImportReviewSection.section_id).where(
                            PriceImportReviewSection.price_import_review_id == review.id
                        )
                    )
                ).scalars().all()
            )
            assert section_ids == {"sec_alpha", "sec_beta"}
            queued = (
                await session.execute(
                    select(PriceImportReviewSection, PriceImportReview)
                    .join(
                        PriceImportReview,
                        PriceImportReview.id
                        == PriceImportReviewSection.price_import_review_id,
                    )
                )
            ).all()
            assert len(queued) == 2
            assert {_review.id for _section, _review in queued} == {review.id}

            again = await ensure_review_required_invariant(
                session, project_id=project.id, actor="viewer"
            )
            assert again == 0
            assert (
                await session.scalar(select(func.count()).select_from(PriceImportReview))
            ) == 1
            assert (
                await session.scalar(
                    select(func.count()).select_from(PriceImportReviewSection)
                )
            ) == 2
        await engine.dispose()

    asyncio.run(scenario())


def test_queue_endpoint_repairs_and_lists_unopened_review_required_import(
    tmp_path, monkeypatch
) -> None:
    """Scenario E (API): GET /review/queue on a historical review_required
    import without a review returns its review and sections, and a repeated
    call is stable."""

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.review import router as review_router
    from app.auth import AuthUser
    from app.db import get_session
    from app.import_auth import get_import_reader
    from app.project_context import get_current_project

    def _missing_blob(*_args, **_kwargs):
        raise HTTPException(status_code=404, detail="stub: source blob missing")

    monkeypatch.setattr("app.storage.get_price_file", _missing_blob)

    project_id = uuid.uuid4()
    database_path = tmp_path / "review-queue.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def init_schema() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(init_schema())

    async def seed():
        async with factory() as session:
            session.add(
                Project(id=project_id, name="Queue project", slug="queue-project")
            )
            await session.commit()
            return await _seed_review_required_import(
                session, project_id=project_id, section_ids=["sec_only"]
            )

    import_id = asyncio.run(seed())

    async def session_override():
        async with factory() as session:
            yield session

    async def project_override():
        return Project(id=project_id, name="Queue project", slug="queue-project")

    def reader_override() -> AuthUser:
        return AuthUser("viewer", "user")

    test_app = FastAPI()
    test_app.include_router(review_router, prefix="/api/v1")
    test_app.dependency_overrides[get_session] = session_override
    test_app.dependency_overrides[get_current_project] = project_override
    test_app.dependency_overrides[get_import_reader] = reader_override
    try:
        client = TestClient(test_app)
        first = client.get("/api/v1/review/queue")
        assert first.status_code == 200, first.text
        payload = first.json()
        assert payload["total"] == 1
        item = payload["items"][0]
        assert item["import_id"] == str(import_id)
        assert item["section_id"] == "sec_only"
        review_id = item["review_id"]
        assert item["review_id"]

        snapshot = client.get(f"/api/v1/review/{import_id}")
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["review_id"] == review_id
        assert len(snapshot.json()["sections"]) == 1

        second = client.get("/api/v1/review/queue")
        assert second.status_code == 200, second.text
        assert second.json()["total"] == 1
        assert second.json()["items"][0]["review_id"] == review_id
    finally:
        test_app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def test_invariant_skips_vat_rate_required_imports(monkeypatch) -> None:
    """Scenario E hardening: VAT_RATE_REQUIRED imports are not repaired into
    the manual review queue - their operator action is the VAT PATCH on the
    import page, and queue materialisation would leave stale pending rows."""

    def _missing_blob(*_args, **_kwargs):
        raise HTTPException(status_code=404, detail="stub: source blob missing")

    monkeypatch.setattr("app.storage.get_price_file", _missing_blob)

    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            project = Project(name="Queue project", slug="queue-project")
            session.add(project)
            await session.commit()
            await _seed_review_required_import(
                session,
                project_id=project.id,
                section_ids=["sec_vat"],
                error_code="VAT_RATE_REQUIRED",
            )

            repaired = await ensure_review_required_invariant(
                session, project_id=project.id, actor="viewer"
            )
            assert repaired == 0
            assert (
                await session.scalar(select(func.count()).select_from(PriceImportReview))
            ) == 0
            assert (
                await session.scalar(
                    select(func.count()).select_from(PriceImportReviewSection)
                )
            ) == 0
        await engine.dispose()

    asyncio.run(scenario())

