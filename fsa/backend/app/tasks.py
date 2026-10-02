"""Celery-задачи: распознавание документа и импорт файла реестра.

Обе задачи идемпотентны по своему ключу, поэтому повторная постановка (ретрай
брокера, двойной клик в интерфейсе) не приводит к двойной работе.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import storage
from app.config import settings
from app.engines import EngineEndpoint, default_base_url
from app.models import (
    Document,
    RegistryImport,
    RegistryImportKind,
    RegistryImportStatus,
)
from app.ocr_client import OCRClient
from app.repository import RegistryRepository
from app.services.documents import DocumentService
from app.services.registry_import import RegistryFileError, import_payload
from app.worker import celery_app


def _make_engine():
    """Свой engine на задачу: Celery крутит собственный event loop."""
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _ocr_client() -> OCRClient | None:
    if not settings.ocr_engine_key:
        return None
    base_url = settings.ocr_engine_url or default_base_url(
        settings.ocr_engine, settings.ocr_engine_host
    )
    return OCRClient(
        EngineEndpoint(
            name=settings.ocr_engine,
            base_url=base_url,
            service_key=settings.ocr_engine_key,
        ),
        timeout=settings.ocr_request_timeout,
    )


@celery_app.task(name="app.tasks.recognize_document", bind=True, max_retries=5)
def recognize_document(self, document_id: str, checked_on: str | None = None) -> str:
    """Распознать документ и сверить его с реестром."""
    try:
        return asyncio.run(_recognize_document(document_id, checked_on))
    except Exception as error:  # noqa: BLE001 — движок бывает занят
        raise self.retry(exc=error, countdown=15) from error


async def _recognize_document(document_id: str, checked_on: str | None) -> str:
    engine, maker = _make_engine()
    client = _ocr_client()
    try:
        async with maker() as session:
            document = await session.get(Document, uuid.UUID(document_id))
            if document is None:
                return "missing"
            payload = storage.get_document(document.storage_key)
            service = DocumentService(session, client)
            outcome = await service.ingest(
                payload,
                filename=document.filename,
                source_path=document.source_path,
                supplier=document.supplier,
            )
            on = date.fromisoformat(checked_on) if checked_on else date.today()
            await service.check(outcome.document, RegistryRepository(session), on=on)
            await session.commit()
            return outcome.document.status
    finally:
        if client is not None:
            await client.close()
        await engine.dispose()


@celery_app.task(name="app.tasks.import_registry_file", bind=True, max_retries=1)
def import_registry_file(self, import_id: str) -> str:
    return asyncio.run(_import_registry_file(import_id))


async def _import_registry_file(import_id: str) -> str:
    engine, maker = _make_engine()
    try:
        async with maker() as session:
            record = await session.get(RegistryImport, uuid.UUID(import_id))
            if record is None:
                return "missing"
            record.status = RegistryImportStatus.running.value
            record.started_at = datetime.now(timezone.utc)
            await session.commit()

            try:
                payload = storage.get_document(record.storage_key)
                result = import_payload(
                    payload,
                    filename=record.filename,
                    kind=RegistryImportKind(record.kind),
                )
            except (RegistryFileError, Exception) as error:  # noqa: BLE001
                record.status = RegistryImportStatus.failed.value
                record.error = str(error)[:2000]
                record.finished_at = datetime.now(timezone.utc)
                await session.commit()
                return record.status

            record.certificates_loaded = result.certificates
            record.numbers_loaded = result.numbers
            record.status = RegistryImportStatus.completed.value
            record.error = ""
            record.finished_at = datetime.now(timezone.utc)
            await session.commit()
            return record.status
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.recheck_documents")
def recheck_documents(checked_on: str | None = None, limit: int = 500) -> int:
    """Пересверить распознанные документы с реестром без повторного OCR.

    Нужна потому, что вердикт меняется сам: сертификат может быть прекращён
    в любой день, а у 23% актуальных срок истекает в пределах года.
    """
    return asyncio.run(_recheck_documents(checked_on, limit))


async def _recheck_documents(checked_on: str | None, limit: int) -> int:
    engine, maker = _make_engine()
    try:
        async with maker() as session:
            on = date.fromisoformat(checked_on) if checked_on else date.today()
            rows = list(
                await session.scalars(
                    select(Document)
                    .where(Document.status == "recognized")
                    .limit(limit)
                )
            )
            service = DocumentService(session, None)
            repository = RegistryRepository(session)
            for document in rows:
                await service.check(document, repository, on=on)
            await session.commit()
            return len(rows)
    finally:
        await engine.dispose()
