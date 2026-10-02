"""Точка входа сервиса FSA."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import storage
from app.api import (
    documents,
    health,
    lookup,
    registry,
    registry_imports,
    review,
    service_keys,
)
from app.auth import require_admin, require_documents, require_read
from app.config import settings
from app.engines import EngineEndpoint, default_base_url
from app.ocr_client import OCRClient

logger = logging.getLogger(__name__)


def build_ocr_client() -> OCRClient | None:
    """Клиент выбранного движка. Без ключа контур распознавания выключен."""
    if not settings.ocr_engine_key:
        logger.warning("OCR_ENGINE_KEY не задан — распознавание недоступно")
        return None
    base_url = settings.ocr_engine_url or default_base_url(
        settings.ocr_engine, settings.ocr_engine_host
    )
    endpoint = EngineEndpoint(
        name=settings.ocr_engine,
        base_url=base_url,
        service_key=settings.ocr_engine_key,
    )
    return OCRClient(endpoint, timeout=settings.ocr_request_timeout)


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.ocr_client = build_ocr_client()
        try:
            storage.ensure_buckets()
        except Exception as error:  # noqa: BLE001 — S3 не обязателен для чтения
            logger.warning("MinIO недоступен: %s", error)
        try:
            yield
        finally:
            if application.state.ocr_client is not None:
                await application.state.ocr_client.close()

    application = FastAPI(title="DocPrice FSA service", version="0.1.0", lifespan=lifespan)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # /health открыт для healthcheck — как у OCR-движков; всё остальное
    # закрыто ключом. Область прав у каждого раздела своя: клиенту прайсов
    # нужно только читать реестр и получать привязку, и большего он не
    # получает даже при утечке своего ключа.
    application.include_router(health.router)
    read_only = [Depends(require_read)]
    documents_scope = [Depends(require_documents)]
    admin_scope = [Depends(require_admin)]
    application.include_router(registry.router, dependencies=read_only)
    application.include_router(registry_imports.router, dependencies=admin_scope)
    application.include_router(documents.router, dependencies=documents_scope)
    application.include_router(review.router, dependencies=documents_scope)
    application.include_router(lookup.router, dependencies=read_only)
    application.include_router(service_keys.router, dependencies=admin_scope)
    return application


app = create_app()
