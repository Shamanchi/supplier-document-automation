# -*- coding: utf-8 -*-
"""Мостик «заявка → прайс/счёт»: загрузка ценового документа со страницы заявки.

Два уровня проверки. Сервисный (`register_order_price_document`) прогоняется на
SQLite и проверяет предметную часть: назначение документа, привязку к заявке,
дедупликацию и то, что файл уходит в тот же разбор Ф2, что и ручная загрузка.
HTTP-ручка проверяется там же с подменённой аутентификацией, а роли
(менеджер грузит, наблюдатель — нет) — на живой PostgreSQL: `require_roles`
читает пользователя из канона identity.
"""

from __future__ import annotations

import asyncio
import io
import os
import uuid
from types import SimpleNamespace

import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import get_session
from app.models import (
    ORIGINAL_PROJECT_ID,
    Base,
    PriceFile,
    PriceImport,
    PriceImportEvent,
    PriceVersion,
    Supplier,
)
from app.modules.checks.models import CheckBatch
from app.modules.documents.storage import InMemoryStorage
from app.price_documents import (
    PriceDocumentError,
    register_order_price_document,
)
from tests.conftest import requires_db

UPLOADS = "/api/v1/check-batches"
FILE_NAME = "счёт-Таурус.xlsx"


def _xlsx(price: str = "3400") -> bytes:
    """Минимальный валидный XLSX: контур проверяет формат по содержимому."""
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "Счёт"
    worksheet.append(["Наименование", "Цена"])
    worksheet.append(["Насос Н1", price])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


async def _session_factory(tmp_path):
    # Импорт приложения регистрирует в `Base.metadata` таблицы всех модулей:
    # без него схема собирается неполной и внешние ключи не разрешаются.
    from app.main import app  # noqa: F401

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bridge.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _run(coro):
    return asyncio.run(coro)


def _settings():
    from app.config import settings

    return settings


@pytest.fixture(autouse=True)
def bridge_settings(monkeypatch):
    """Секреты приложения.

    Внутренний скоуп UI (`internal_service_client`) хеширует свой ключ, поэтому
    ему нужен `SERVICE_API_KEY_PEPPER` (или секрет подписи не короче 32 знаков),
    а middleware — секрет для проверки выпущенных токенов.
    """
    from app.config import settings

    for key, value in {
        "service_api_key_pepper": "b" * 48,
        "app_auth_secret": "bridge-secret-that-is-long-enough-123456789",
        "app_admin_username": "ui-admin",
        "app_admin_password": "ui-admin-password-long",
        "app_user_username": "ui-viewer",
        "app_user_password": "ui-viewer-password-long",
    }.items():
        monkeypatch.setattr(settings, key, value)


# --- сервисный уровень ------------------------------------------------------


def test_supplier_quote_is_bound_to_its_batch_and_queued(tmp_path) -> None:
    """Разовый счёт/КП уходит на разбор как документ своей заявки."""

    async def scenario() -> None:
        engine, factory = await _session_factory(tmp_path)
        batch_id = uuid.uuid4()
        storage = InMemoryStorage()
        settings = _settings()
        try:
            async with factory() as session:
                registration = await register_order_price_document(
                    session,
                    settings,
                    storage,
                    document_type="SUPPLIER_QUOTE",
                    check_batch_id=batch_id,
                    supplier_external_id="tavrus-1",
                    supplier_name="ООО «Таурус»",
                    file_name=FILE_NAME,
                    media_type="application/vnd.ms-excel",
                    data=_xlsx(),
                    actor="manager@docprice.internal",
                )
                await session.commit()

                assert registration.created is True
                price_import = registration.price_import
                version = await session.get(PriceVersion, price_import.price_version_id)
                price_file = await session.get(PriceFile, price_import.price_file_id)
                supplier = await session.get(Supplier, price_import.supplier_id)

                # Файл — из интерфейса (`ck_price_files_source` знает UPLOAD).
                assert price_file.source == "UPLOAD"
                assert price_file.project_id == ORIGINAL_PROJECT_ID
                assert price_file.original_filename == FILE_NAME
                # Назначение и привязка: счёт действует только в своей заявке.
                assert version.document_type == "SUPPLIER_QUOTE"
                assert version.check_batch_id == batch_id
                assert version.status == "uploaded"
                # Поставщик заведён в том же скоупе, что и остальные UI-импорты.
                assert supplier.external_id == "tavrus-1"
                assert supplier.name == "ООО «Таурус»"
                assert supplier.code.startswith("svc-")

                event = (
                    await session.execute(
                        select(PriceImportEvent).where(
                            PriceImportEvent.price_import_id == price_import.id
                        )
                    )
                ).scalar_one()
                assert event.to_status == "queued"
                assert event.payload["source"] == "UPLOAD"
                assert event.payload["documentType"] == "SUPPLIER_QUOTE"
                assert event.payload["checkBatchId"] == str(batch_id)

                # Карантин отдан наружу, файл лежит под своим sha256.
                assert price_file.storage_key.startswith("sha256/")
                assert all(
                    not key[0].startswith("q") for key in storage.objects
                ), storage.objects
                assert len(storage.objects) == 1
        finally:
            await engine.dispose()

    _run(scenario())


def test_same_file_without_key_is_not_registered_twice(tmp_path) -> None:
    """Повторное нажатие «Загрузить» не заводит второй разбор."""

    async def scenario() -> None:
        engine, factory = await _session_factory(tmp_path)
        storage = InMemoryStorage()
        settings = _settings()
        data = _xlsx()
        try:
            async with factory() as session:
                first = await register_order_price_document(
                    session,
                    settings,
                    storage,
                    document_type="PRICE_LIST",
                    check_batch_id=None,
                    supplier_external_id="prc-1",
                    file_name="прайс.xlsx",
                    media_type=None,
                    data=data,
                )
                await session.commit()
                # Тот же файл в другую заявку — это другой документ.
                second = await register_order_price_document(
                    session,
                    settings,
                    storage,
                    document_type="SUPPLIER_QUOTE",
                    check_batch_id=uuid.uuid4(),
                    supplier_external_id="prc-1",
                    file_name="прайс.xlsx",
                    media_type=None,
                    data=data,
                )
                await session.commit()
                repeat = await register_order_price_document(
                    session,
                    settings,
                    storage,
                    document_type="PRICE_LIST",
                    check_batch_id=None,
                    supplier_external_id="prc-1",
                    file_name="прайс.xlsx",
                    media_type=None,
                    data=data,
                )
                await session.commit()

                assert first.created is True
                assert second.created is True
                assert repeat.created is False
                assert repeat.price_import.id == first.price_import.id
                assert (
                    await session.scalar(
                        select(func.count(PriceImport.id)).where(
                            PriceImport.supplier_id == first.price_import.supplier_id
                        )
                    )
                ) == 2
        finally:
            await engine.dispose()

    _run(scenario())


def test_document_type_defines_batch_binding(tmp_path) -> None:
    """Привязка к заявке — свойство назначения, а не параметр запроса."""

    async def scenario() -> None:
        engine, factory = await _session_factory(tmp_path)
        settings = _settings()
        common = {
            "supplier_external_id": "prc-2",
            "file_name": "док.xlsx",
            "media_type": None,
            "data": _xlsx(),
        }
        try:
            async with factory() as session:
                with pytest.raises(PriceDocumentError):
                    await register_order_price_document(
                        session,
                        settings,
                        InMemoryStorage(),
                        document_type="PRICE_LIST",
                        check_batch_id=uuid.uuid4(),
                        **common,
                    )
                with pytest.raises(PriceDocumentError):
                    await register_order_price_document(
                        session,
                        settings,
                        InMemoryStorage(),
                        document_type="SUPPLIER_QUOTE",
                        check_batch_id=None,
                        **common,
                    )
                with pytest.raises(PriceDocumentError):
                    await register_order_price_document(
                        session,
                        settings,
                        InMemoryStorage(),
                        document_type="WHATEVER",
                        check_batch_id=None,
                        **common,
                    )
        finally:
            await engine.dispose()

    _run(scenario())


def test_broken_file_is_rejected_before_registration(tmp_path) -> None:
    """Формат сверяется по содержимому: переименованный текст не проходит."""

    async def scenario() -> None:
        engine, factory = await _session_factory(tmp_path)
        storage = InMemoryStorage()
        try:
            async with factory() as session:
                with pytest.raises(PriceDocumentError):
                    await register_order_price_document(
                        session,
                        _settings(),
                        storage,
                        document_type="SUPPLIER_QUOTE",
                        check_batch_id=uuid.uuid4(),
                        supplier_external_id="prc-3",
                        file_name="счёт.xlsx",
                        media_type=None,
                        data="Наименование;Цена\nНасос;3400\n".encode(),
                    )
            # Отказ не оставляет ни строк в БД, ни объектов в карантине.
            assert storage.objects == {}
        finally:
            await engine.dispose()

    _run(scenario())




def test_bitrix_wrapper_keeps_its_contract(tmp_path) -> None:
    """Общий модуль не изменил поведение интеграции со сделкой.

    Ключ идемпотентности, источник файла и метаданные события — то, по чему
    узнаются уже импортированные файлы сделки; их переименование сделало бы
    повторную доставку нового файла новым импортом.
    """
    import hashlib

    from app.modules.integrations.bitrix.pricing import register_price_document

    async def scenario() -> None:
        engine, factory = await _session_factory(tmp_path)
        storage = InMemoryStorage()
        external_file_id = "3856291"
        try:
            async with factory() as session:
                supplier = Supplier(
                    external_id="bitrix-supplier", code="bitrix-1", name="Поставщик сделки"
                )
                session.add(supplier)
                await session.flush()
                first = await register_price_document(
                    session,
                    _settings(),
                    storage,
                    supplier_id=supplier.id,
                    document_type="PRICE_LIST",
                    check_batch_id=None,
                    external_file_id=external_file_id,
                    file_name="прайс.xlsx",
                    media_type=None,
                    data=_xlsx(),
                )
                await session.commit()
                repeat = await register_price_document(
                    session,
                    _settings(),
                    storage,
                    supplier_id=supplier.id,
                    document_type="PRICE_LIST",
                    check_batch_id=None,
                    external_file_id=external_file_id,
                    file_name="прайс.xlsx",
                    media_type=None,
                    data=_xlsx(),
                )
                await session.commit()

                assert first.id == repeat.id
                assert first.idempotency_key == (
                    "bitrix-"
                    + hashlib.sha256(external_file_id.encode()).hexdigest()
                )
                price_file = await session.get(PriceFile, first.price_file_id)
                assert price_file.source == "BITRIX"
                assert price_file.project_id == ORIGINAL_PROJECT_ID
                event = (
                    await session.execute(
                        select(PriceImportEvent).where(
                            PriceImportEvent.price_import_id == first.id
                        )
                    )
                ).scalar_one()
                assert event.payload["externalFileId"] == external_file_id
                assert event.payload["source"] == "BITRIX"
                assert (
                    await session.scalar(select(func.count(PriceImport.id)))
                ) == 1
        finally:
            await engine.dispose()

    _run(scenario())


# --- HTTP-ручка -------------------------------------------------------------


def _manager_headers() -> dict[str, str]:
    from app.config import settings
    from app.modules.identity.security import AccessClaims, create_access_token
    from app.modules.identity.service import signing_secret

    token, _expires_in = create_access_token(
        AccessClaims(
            user_id=uuid.uuid4(), session_id=uuid.uuid4(), roles=("manager",)
        ),
        secret=signing_secret(settings),
        expires_minutes=15,
    )
    return {"Authorization": f"Bearer {token}"}


def _with_db(db_path, operation):
    """Обращение к той же БД извне цикла TestClient — своим движком.

    Переиспользовать движок приложения нельзя: пул отдаёт соединение, чья
    транзакция чтения началась в другом event loop'е, и тест видит устаревший
    снимок базы (проверено: `replayed` в ответе расходился с содержимым таблиц).
    """

    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                return await operation(session)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _http_stand(tmp_path, monkeypatch):
    """Приложение на SQLite: подменены сессия, хранилище, роли и очередь."""
    from app.main import app
    from app.modules.documents.router import get_storage
    from app.modules.identity.dependencies import get_current_user
    import app.api.order_price_documents as bridge_module
    import app.tasks as tasks_module

    async def no_quotas(*_args, **_kwargs) -> None:
        return None

    published: list[str] = []

    class FakeTask:
        @staticmethod
        def delay(import_id: str) -> None:
            published.append(import_id)

    monkeypatch.setattr(bridge_module, "enforce_service_quotas", no_quotas)
    monkeypatch.setattr(tasks_module, "run_price_import", FakeTask())

    # Схема создаётся одноразовым движком: движок приложения должен открыть свои
    # соединения уже внутри цикла TestClient.
    db_path = tmp_path / "bridge.sqlite"

    async def create_schema() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    _run(create_schema())
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def session_override():
        async with factory() as session:
            yield session

    storage = InMemoryStorage()
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        role_names=["manager"], email="manager@docprice.internal"
    )
    return app, db_path, storage, published


def _drop_overrides(app) -> None:
    from app.modules.documents.router import get_storage
    from app.modules.identity.dependencies import get_current_user

    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_storage, None)
    app.dependency_overrides.pop(get_current_user, None)


def test_manager_uploads_quote_through_the_http_bridge(tmp_path, monkeypatch) -> None:
    """Менеджер кладёт счёт в заявку и получает задание разбора."""
    app_, db_path, storage, published = _http_stand(tmp_path, monkeypatch)
    batch_id = str(uuid.uuid4())
    batch_uuid = uuid.UUID(batch_id)

    async def prepare(session) -> None:
        session.add(
            CheckBatch(
                id=batch_uuid,
                title="Насосы для котельной",
                customer="АО Заказчик",
                status="DRAFT",
                final_verdict="PENDING",
                author_id=uuid.uuid4(),
            )
        )
        await session.commit()

    _with_db(db_path, prepare)
    try:
        client = TestClient(app_)
        response = client.post(
            f"{UPLOADS}/{batch_id}/price-documents",
            headers={**_manager_headers(), "Idempotency-Key": str(uuid.uuid4())},
            data={
                "supplier_external_id": "tavrus-http",
                "supplier_name": "ООО «Таурус»",
                "document_type": "SUPPLIER_QUOTE",
                "currency_hint": "RUB",
            },
            files={"file": (FILE_NAME, _xlsx(), "application/octet-stream")},
        )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["document_type"] == "SUPPLIER_QUOTE"
        assert body["check_batch_id"] == batch_id
        assert body["status"] == "queued"
        assert body["replayed"] is False
        assert body["status_url"] == f"/api/v1/price-imports/{body['import_id']}"
        # Разбор поставлен в очередь ровно один раз.
        assert published == [body["import_id"]]

        async def check(session) -> None:
            price_import = await session.get(PriceImport, uuid.UUID(body["import_id"]))
            version = await session.get(
                PriceVersion, uuid.UUID(body["price_version_id"])
            )
            assert price_import.status == "queued"
            assert price_import.currency_hint == "RUB"
            assert version.check_batch_id == batch_uuid

        _with_db(db_path, check)

        # Тот же ключ со свежим содержимым — повтор, а не второй разбор: клиент,
        # повторивший запрос (обрыв связи, двойное нажатие), получает тот же
        # импорт. Тот же самый файл в ту же заявку отсекается и без ключа — по
        # sha256 (см. сервисный тест).
        repeat = client.post(
            f"{UPLOADS}/{batch_id}/price-documents",
            headers={**_manager_headers(), "Idempotency-Key": "stable-bridge-key"},
            data={
                "supplier_external_id": "tavrus-http",
                "document_type": "SUPPLIER_QUOTE",
            },
            files={"file": (FILE_NAME, _xlsx("3500"), "application/octet-stream")},
        )
        again = client.post(
            f"{UPLOADS}/{batch_id}/price-documents",
            headers={**_manager_headers(), "Idempotency-Key": "stable-bridge-key"},
            data={
                "supplier_external_id": "tavrus-http",
                "document_type": "SUPPLIER_QUOTE",
            },
            files={"file": (FILE_NAME, _xlsx("3600"), "application/octet-stream")},
        )
        assert repeat.status_code == 202, repeat.text
        assert again.status_code == 202, again.text
        assert repeat.json()["replayed"] is False
        assert again.json()["replayed"] is True
        assert again.json()["import_id"] == repeat.json()["import_id"]
        # В очереди только два файла: первая загрузка и первый из пары повторов.
        assert len(published) == 2
    finally:
        _drop_overrides(app_)


def test_unknown_batch_is_rejected(tmp_path, monkeypatch) -> None:
    """Заявки нет — 404 до постановки разбора в очередь."""
    app_, _db_path, _storage, published = _http_stand(tmp_path, monkeypatch)
    try:
        client = TestClient(app_)
        missing = client.post(
            f"{UPLOADS}/{uuid.uuid4()}/price-documents",
            headers={**_manager_headers(), "Idempotency-Key": str(uuid.uuid4())},
            data={
                "supplier_external_id": "tavrus-http",
                "document_type": "SUPPLIER_QUOTE",
            },
            files={"file": (FILE_NAME, _xlsx(), "application/octet-stream")},
        )
        assert missing.status_code == 404
        assert published == []
    finally:
        _drop_overrides(app_)


# --- роли (нужна живая PostgreSQL: identity читает пользователя из БД) -------


@requires_db
def test_viewer_cannot_upload_price_document(client) -> None:
    """Заявку ведёт менеджер: наблюдателю мостик недоступен.

    Право проверяет зависимость маршрута (`require_roles`), а не middleware —
    префикс `/check-batches` делегирован контуру заявок, поэтому роль здесь
    единственная защита записи.
    """
    viewer = {
        "email": "viewer@docprice.internal",
        "password": os.getenv("APP_VIEWER_PASSWORD")
        or os.getenv("BOOTSTRAP_VIEWER_PASSWORD")
        or "viewer",
    }
    token = client.post("/api/v1/auth/login", json=viewer).json()["accessToken"]

    response = client.post(
        f"{UPLOADS}/{uuid.uuid4()}/price-documents",
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": str(uuid.uuid4()),
        },
        data={"supplier_external_id": "x", "document_type": "SUPPLIER_QUOTE"},
        files={"file": (FILE_NAME, _xlsx(), "application/octet-stream")},
    )

    assert response.status_code == 403, response.text
