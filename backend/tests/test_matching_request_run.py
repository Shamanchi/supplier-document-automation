"""Подбор строк заявки с сохранением результата в БД.

Сценарии приёмки issue «Подбор строк заявки»:
- строка с уверенным совпадением (auto/review) — поля matched_canonical_id /
  match_stage / match_score записались в строку;
- строка без совпадения (not_found) — canonical_id остаётся NULL, ступень и
  балл по факту результата;
- ручной выбор канона /select — строка получает match_stage=manual и канон,
  назначенный менеджером;
- принадлежность строки заявке проверяется.

Каталог подбора строится так же, как в test_matching.py: позиция прайса →
CanonicalService.rebuild → каскад MatchService. Роутер matching проверяется
через TestClient на sqlite-схеме боевых моделей. Приложение собирается
минимальным (только matching-роутер): боевой вход через /auth/login требует
живого Postgres (identity-модуль), поэтому здесь роли подставляются на уровне
зависимостей — тестируется контракт подбора, а не стендовая аутентификация.
"""

from __future__ import annotations

import asyncio
import uuid

from datetime import datetime, timezone

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.matching import router as matching_router
from app.auth import AuthUser
from app.db import get_session
from app.import_auth import get_import_admin, get_import_reader
from app.matching.canonical import CanonicalService
from app.matching.text import normalize_article, normalize_name
from app.models import (
    ORIGINAL_PROJECT_ID,
    Base,
    CanonicalProduct,
    PriceFile,
    PriceRequest,
    PriceRequestLine,
    PriceVersion,
    Project,
    Supplier,
    SupplierPriceItem,
)
from app.project_context import get_current_project


async def _seed_catalog(db: AsyncSession) -> uuid.UUID:
    """Позиция «ОП-5» в активной версии прайса → канон с артикулом ОП-5."""
    project = Project(
        id=ORIGINAL_PROJECT_ID,
        name="Оригинальные прайсы",
        slug="original-prices",
        is_default=True,
    )
    db.add(project)
    supplier = Supplier(code="request-supplier", name="Supplier A")
    db.add(supplier)
    await db.flush()
    price_file = PriceFile(
        project_id=project.id,
        supplier_id=supplier.id,
        original_filename="request-catalog.xlsx",
        storage_key="request-catalog.xlsx",
        sha256="r" * 64,
        size_bytes=1,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    db.add(price_file)
    await db.flush()
    version = PriceVersion(
        supplier_id=supplier.id,
        price_file_id=price_file.id,
        version_no=1,
        status="active",
    )
    db.add(version)
    await db.flush()
    item = SupplierPriceItem(
        price_version_id=version.id,
        source_name="Огнетушитель ОП-5(з)-АВСЕ",
        normalized_name=normalize_name("Огнетушитель ОП-5(з)-АВСЕ"),
        source_article="ОП-5",
        normalized_article=normalize_article("ОП-5"),
        source_sheet="Прайс",
        source_row=3,
        created_at=datetime.now(timezone.utc),
    )
    db.add(item)
    await db.flush()
    rebuilt = await CanonicalService(db).rebuild()
    assert rebuilt.canonicals == 1
    canonical = await db.scalar(
        select(CanonicalProduct).where(CanonicalProduct.match_key.like("article:%"))
    )
    assert canonical is not None
    return canonical.id


async def _seed_request(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """Заявка из двух строк: уверенное совпадение и отсутствующий товар."""
    canonical_id = await _seed_catalog(db)
    request = PriceRequest(
        title="Заявка на ОП-5",
        source="manual",
        raw_text="1. ОП-5 — 10 шт\n2. Сервер стоечный 2U — 1 шт",
    )
    db.add(request)
    await db.flush()
    db.add_all(
        [
            PriceRequestLine(
                request_id=request.id,
                ordinal=1,
                source_line="ОП-5 — 10 шт",
                query_name="ОП-5",
                quantity=10,
                unit_code="шт",
            ),
            PriceRequestLine(
                request_id=request.id,
                ordinal=2,
                source_line="Сервер стоечный 2U — 1 шт",
                query_name="Сервер стоечный 2U",
                quantity=1,
                unit_code="шт",
            ),
        ]
    )
    await db.commit()
    return request.id, canonical_id


async def _seed_select_scenario(
    db: AsyncSession,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Нераспознанная строка заявки + канон для ручного выбора."""
    canonical_id = await _seed_catalog(db)
    request = PriceRequest(title="Заявка на сервер", source="manual")
    db.add(request)
    await db.flush()
    line = PriceRequestLine(
        request_id=request.id,
        ordinal=1,
        source_line="Сервер стоечный 2U — 1 шт",
        query_name="Сервер стоечный 2U",
        quantity=1,
        unit_code="шт",
    )
    db.add(line)
    await db.commit()
    return request.id, line.id, canonical_id


async def _seed_foreign_line(db: AsyncSession) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Строка второй заявки + канон: для проверки принадлежности строки."""
    canonical_id = await _seed_catalog(db)
    first = PriceRequest(title="Первая заявка", source="manual")
    second = PriceRequest(title="Вторая заявка", source="manual")
    db.add_all([first, second])
    await db.flush()
    line = PriceRequestLine(
        request_id=second.id,
        ordinal=1,
        source_line="ОП-5 — 2 шт",
        query_name="ОП-5",
    )
    db.add(line)
    await db.commit()
    return first.id, line.id, canonical_id


def _client(tmp_path) -> tuple[TestClient, async_sessionmaker, object]:
    """SQLite-база на тест + TestClient только с matching-роутером."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'request.sqlite'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def init_schema():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    asyncio.run(init_schema())

    async def session_override():
        async with factory() as session:
            yield session

    async def project_override():
        return Project(
            id=ORIGINAL_PROJECT_ID,
            name="Оригинальные прайсы",
            slug="original-prices",
            is_default=True,
        )

    def reader_override() -> AuthUser:
        return AuthUser("viewer", "user")

    def admin_override() -> AuthUser:
        return AuthUser("admin", "admin")

    app = FastAPI()
    app.include_router(matching_router, prefix="/api/v1")
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_current_project] = project_override
    app.dependency_overrides[get_import_reader] = reader_override
    app.dependency_overrides[get_import_admin] = admin_override
    return TestClient(app), factory, engine


def test_run_persists_confident_match_and_leaves_not_found_empty(tmp_path):
    """Авто-совпадение пишется в строку, not_found остаётся без канона."""
    client, factory, engine = _client(tmp_path)
    try:
        async def seed():
            async with factory() as db:
                return await _seed_request(db)

        request_id, canonical_id = asyncio.run(seed())

        response = client.post(f"/api/v1/matching/request/{request_id}")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["request_id"] == str(request_id)
        assert len(payload["lines"]) == 2
        matched, missing = payload["lines"]

        assert matched["ordinal"] == 1
        assert matched["decision"] in {"auto", "review"}
        assert matched["matched_canonical_id"] == str(canonical_id)
        assert matched["matched_stage"] == matched["stage"]
        assert matched["matched_score"] is not None

        assert missing["ordinal"] == 2
        assert missing["decision"] == "not_found"
        assert missing["matched_canonical_id"] is None
        assert missing["matched_stage"] == "none"
        assert missing["matched_score"] is None

        async def verify():
            async with factory() as db:
                lines = list(
                    (
                        await db.execute(
                            select(PriceRequestLine)
                            .where(PriceRequestLine.request_id == request_id)
                            .order_by(PriceRequestLine.ordinal)
                        )
                    ).scalars()
                )
                assert len(lines) == 2
                assert lines[0].matched_canonical_id == canonical_id
                assert lines[0].match_stage == "article"
                assert lines[0].match_score is not None
                assert lines[1].matched_canonical_id is None
                assert lines[1].match_stage == "none"
                assert lines[1].match_score is None

        asyncio.run(verify())
    finally:
        asyncio.run(engine.dispose())


def test_run_returns_404_for_unknown_request(tmp_path):
    client, factory, engine = _client(tmp_path)
    try:
        response = client.post(f"/api/v1/matching/request/{uuid.uuid4()}")
        assert response.status_code == 404
    finally:
        asyncio.run(engine.dispose())


def test_select_assigns_manual_canonical_to_unmatched_line(tmp_path):
    """/select ставит строке канон менеджера: stage=manual, score NULL."""
    client, factory, engine = _client(tmp_path)
    try:
        async def seed():
            async with factory() as db:
                return await _seed_select_scenario(db)

        request_id, line_id, canonical_id = asyncio.run(seed())

        selected = client.post(
            f"/api/v1/matching/request/{request_id}/lines/{line_id}/select",
            json={"canonical_id": str(canonical_id)},
        )
        assert selected.status_code == 200, selected.text
        assert selected.json()["canonical_id"] == str(canonical_id)
        assert selected.json()["matched_stage"] == "manual"
        assert selected.json()["matched_score"] is None

        async def verify():
            async with factory() as db:
                line = await db.get(PriceRequestLine, line_id)
                assert line is not None
                assert line.matched_canonical_id == canonical_id
                assert line.match_stage == "manual"
                assert line.match_score is None

        asyncio.run(verify())
    finally:
        asyncio.run(engine.dispose())


def test_select_rejects_line_from_another_request(tmp_path):
    """Принадлежность строки заявке обязательна: чужая строка → 404."""
    client, factory, engine = _client(tmp_path)
    try:
        async def seed():
            async with factory() as db:
                return await _seed_foreign_line(db)

        other_request_id, line_id, canonical_id = asyncio.run(seed())

        rejected = client.post(
            f"/api/v1/matching/request/{other_request_id}/lines/{line_id}/select",
            json={"canonical_id": str(canonical_id)},
        )
        assert rejected.status_code == 404
    finally:
        asyncio.run(engine.dispose())
