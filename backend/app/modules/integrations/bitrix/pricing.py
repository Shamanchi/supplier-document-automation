"""Регистрация ценового документа, скачанного из сделки.

Сама логика переехала в `app.price_documents`: её переиспользует мостик
«заявка → прайс/счёт», который менеджер запускает из интерфейса. Здесь остаётся
тонкая обёртка с прежней сигнатурой — вызывающий код интеграции не должен
знать, что у регистрации появился второй источник.

Ключ идемпотентности (`bitrix-<sha256 внешнего id файла>`) сохранён дословно:
переименование сделало бы уже импортированные файлы сделок новыми.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import PriceImport
from app.modules.documents.storage import StoragePort
from app.price_documents import (
    DOCUMENT_TYPES,
    SOURCE_BITRIX,
    PriceDocumentError,
    detect_or_reject,
    register_price_document as _register_price_document,
)

__all__ = [
    "DOCUMENT_TYPES",
    "PriceDocumentError",
    "detect_or_reject",
    "register_price_document",
]


async def register_price_document(
    session: AsyncSession,
    settings: Settings,
    storage: StoragePort,
    *,
    supplier_id: uuid.UUID,
    document_type: str,
    check_batch_id: uuid.UUID | None,
    external_file_id: str,
    file_name: str,
    media_type: str | None,
    data: bytes,
) -> PriceImport:
    """Создаёт `PriceFile` → `PriceVersion` → `PriceImport` для файла сделки."""

    registration = await _register_price_document(
        session,
        settings,
        storage,
        supplier_id=supplier_id,
        document_type=document_type,
        check_batch_id=check_batch_id,
        source=SOURCE_BITRIX,
        idempotency_key=f"bitrix-{hashlib.sha256(external_file_id.encode()).hexdigest()}",
        file_name=file_name,
        media_type=media_type,
        data=data,
        origin={"externalFileId": external_file_id},
    )
    return registration.price_import
