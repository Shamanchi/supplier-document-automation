"""Список версий `/price-versions` отдаёт назначение документа и активацию.

Инженерный UI показывает кнопку «Активировать» только для `validated`
общих прайсов (`PRICE_LIST`); чтобы отличить их от разовых счетов заявки
(`SUPPLIER_QUOTE`), списку нужны `document_type`/`check_batch_id` и
`activated_at`.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import get_session
from app.models import (
    ORIGINAL_PROJECT_ID,
    Base,
    PriceFile,
    PriceVersion,
    Supplier,
    VersionStatus,
)


def test_price_version_listing_exposes_document_type_and_activation(
    tmp_path, monkeypatch
) -> None:
    from app.config import settings
    from app.main import app

    database_path = tmp_path / "versions.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    for key, value in {
        "app_admin_username": "ui-admin",
        "app_admin_password": "ui-admin-password-long",
        "app_user_username": "ui-viewer",
        "app_user_password": "ui-viewer-password-long",
        "app_auth_secret": "ui-auth-secret-that-is-long-enough-123456789",
        "app_auth_secure_cookies": False,
    }.items():
        monkeypatch.setattr(settings, key, value)

    # Логин через `/auth/login` уходит в `SessionLocal` (Postgres из env), а не
    # в переопределённую сессию, поэтому токен выпускаем напрямую.
    from app.modules.identity.security import AccessClaims, create_access_token
    from app.modules.identity.service import signing_secret

    token, _expires_in = create_access_token(
        AccessClaims(
            user_id=uuid.uuid4(), session_id=uuid.uuid4(), roles=("admin",)
        ),
        secret=signing_secret(settings),
        expires_minutes=15,
    )

    check_batch_id = uuid.uuid4()
    activated_at = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)

    async def prepare() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            supplier = Supplier(
                external_id="supplier-a",
                code=f"svc-{uuid.uuid4().hex[:12]}",
                name="Supplier A",
            )
            session.add(supplier)
            await session.flush()
            files = [
                PriceFile(
                    project_id=ORIGINAL_PROJECT_ID,
                    supplier_id=supplier.id,
                    original_filename="common.xlsx",
                    storage_key="sha256/common.xlsx",
                    sha256="a" * 64,
                    size_bytes=10,
                    mime="application/octet-stream",
                ),
                PriceFile(
                    project_id=ORIGINAL_PROJECT_ID,
                    supplier_id=supplier.id,
                    original_filename="invoice.xlsx",
                    storage_key="sha256/invoice.xlsx",
                    sha256="b" * 64,
                    size_bytes=10,
                    mime="application/octet-stream",
                ),
            ]
            session.add_all(files)
            await session.flush()
            session.add(
                PriceVersion(
                    supplier_id=supplier.id,
                    price_file_id=files[0].id,
                    version_no=1,
                    status=VersionStatus.validated.value,
                    document_type="PRICE_LIST",
                )
            )
            session.add(
                PriceVersion(
                    supplier_id=supplier.id,
                    price_file_id=files[1].id,
                    version_no=2,
                    status=VersionStatus.active.value,
                    document_type="PRICE_LIST",
                    activated_at=activated_at,
                )
            )
            session.add(
                PriceVersion(
                    supplier_id=supplier.id,
                    price_file_id=files[0].id,
                    version_no=3,
                    status=VersionStatus.validated.value,
                    document_type="SUPPLIER_QUOTE",
                    check_batch_id=check_batch_id,
                )
            )
            await session.commit()

    asyncio.run(prepare())

    async def session_override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    try:
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {token}"}

        listing = client.get("/api/v1/price-versions", headers=headers)
        assert listing.status_code == 200, listing.text
        items = {row["version_no"]: row for row in listing.json()["items"]}

        assert items[1]["document_type"] == "PRICE_LIST"
        assert items[1]["check_batch_id"] is None
        assert items[1]["activated_at"] is None

        active = items[2]
        assert active["document_type"] == "PRICE_LIST"
        # SQLite хранит наивный UTC: сравниваем как момент времени.
        naive = datetime.fromisoformat(active["activated_at"]).replace(
            tzinfo=timezone.utc
        )
        assert naive == activated_at

        quote = next(
            row
            for row in listing.json()["items"]
            if row["document_type"] == "SUPPLIER_QUOTE"
        )
        assert quote["check_batch_id"] == str(check_batch_id)
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())
