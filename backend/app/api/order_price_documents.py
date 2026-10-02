"""Мостик «заявка → прайс/счёт»: загрузка ценового документа со страницы заявки.

Менеджеру нужен путь получить цену, которой ещё нет в каталоге: пришёл новый
прайс поставщика или вместо прайса — разовый счёт/КП под конкретную заявку.
До этого мостика такой файл можно было загрузить только из сделки Bitrix24,
поэтому локальная заявка оставалась без цен.

Ручка живёт под префиксом `/check-batches`: он уже делегирован middleware
(решение №4 задачи №7), а право на запись проверяет зависимость маршрута —
«прайс везёт тот, кто ведёт заявку» (менеджер/админ). Назначение документа
задаёт его дальнейшую судьбу: `SUPPLIER_QUOTE` действует только в своей заявке
после разбора и проверки, `PRICE_LIST` становится общим прайсом только после
активации администратором в инженерном интерфейсе.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

# Правила ключа идемпотентности и ставки НДС берутся из сервисной загрузки
# прайса, а не описываются заново: это один контракт, и разойтись ему нельзя.
from app.api.price_imports import _normalise_vat_rate_percent, _validate_idempotency_key
from app.api.suppliers import ALLOWED_EXTENSIONS
from app.config import settings
from app.currencies import normalise_currency_hint
from app.db import get_session
from app.import_auth import enforce_service_quotas, internal_service_client
from app.models import PriceImport, PriceImportEvent, PriceImportStatus, Project
from app.modules.checks.models import CheckBatch
from app.modules.documents.router import get_storage
from app.modules.documents.storage import StoragePort
from app.modules.identity.dependencies import require_roles
from app.modules.identity.models import User
from app.price_documents import PriceDocumentError, register_order_price_document
from app.project_context import get_current_project
from app.schemas import OrderPriceDocumentAccepted

router = APIRouter(prefix="/check-batches", tags=["check-batches"])

#: Ценовой документ заявки везёт тот, кто её ведёт: это не общий каталог, а
#: ответ конкретного поставщика на конкретный запрос.
PriceDocumentUploader = Annotated[
    User, Depends(require_roles("manager", "admin", "sysadmin"))
]


async def _publish_import(session: AsyncSession, price_import: PriceImport) -> None:
    """Ставит разбор в очередь; сбой постановки терминален для импорта.

    Молча оставить импорт в `queued` нельзя: воркер его не увидит, а менеджер
    будет ждать позиций, которых не появится. Тот же приём, что у сервисной
    загрузки прайса.
    """
    from app.tasks import run_price_import

    try:
        run_price_import.delay(str(price_import.id))
    except Exception as exc:  # noqa: BLE001 - a created job must be terminal or queued
        price_import.status = PriceImportStatus.failed.value
        price_import.stage = PriceImportStatus.failed.value
        price_import.progress_percent = 100
        price_import.error_code = "QUEUE_PUBLISH_FAILED"
        price_import.error_message = type(exc).__name__
        session.add(
            PriceImportEvent(
                price_import_id=price_import.id,
                event_type="queue_publish_failed",
                from_status=PriceImportStatus.queued.value,
                to_status=PriceImportStatus.failed.value,
            )
        )
        await session.commit()


@router.post(
    "/{batch_id}/price-documents",
    response_model=OrderPriceDocumentAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_batch_price_document(
    batch_id: uuid.UUID,
    user: PriceDocumentUploader,
    project: Annotated[Project, Depends(get_current_project)],
    storage: Annotated[StoragePort, Depends(get_storage)],
    session: Annotated[AsyncSession, Depends(get_session)],
    file: Annotated[UploadFile, File()],
    supplier_external_id: Annotated[str, Form(min_length=1, max_length=255)],
    document_type: Annotated[Literal["PRICE_LIST", "SUPPLIER_QUOTE"], Form()],
    supplier_name: Annotated[str | None, Form(max_length=255)] = None,
    currency_hint: Annotated[str | None, Form()] = None,
    vat_rate_percent: Annotated[str | None, Form()] = None,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
) -> OrderPriceDocumentAccepted:
    """Принять прайс-лист или счёт/КП в заявку и отправить файл на разбор."""
    batch = await session.get(CheckBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Заявка не найдена")

    filename = file.filename or "price.xlsx"
    if not filename.lower().endswith(ALLOWED_EXTENSIONS):
        raise HTTPException(status_code=422, detail="Формат файла не поддерживается")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Пустой файл")

    if idempotency_key:
        _validate_idempotency_key(idempotency_key)
    try:
        currency = normalise_currency_hint(currency_hint)
        vat_rate = _normalise_vat_rate_percent(vat_rate_percent)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Скоп импорта — внутренний клиент интерфейса: у менеджера нет сервисного
    # ключа, а идемпотентности и учёту нужен устойчивый владелец.
    client = await internal_service_client(session)
    await enforce_service_quotas(client, session)

    check_batch_id = batch_id if document_type == "SUPPLIER_QUOTE" else None
    try:
        registration = await register_order_price_document(
            session,
            settings,
            storage,
            document_type=document_type,
            check_batch_id=check_batch_id,
            supplier_external_id=supplier_external_id,
            supplier_name=supplier_name,
            file_name=filename,
            media_type=file.content_type,
            data=data,
            idempotency_key=idempotency_key or None,
            currency_hint=currency,
            vat_rate_percent=vat_rate,
            project_id=project.id,
            actor=user.email,
        )
    except PriceDocumentError as exc:
        raise HTTPException(status_code=422, detail=exc.message) from exc

    await session.commit()
    price_import = registration.price_import
    if registration.created:
        await _publish_import(session, price_import)

    status_url = f"/api/v1/price-imports/{price_import.id}"
    return OrderPriceDocumentAccepted(
        import_id=price_import.id,
        price_version_id=price_import.price_version_id,
        document_type=document_type,
        check_batch_id=check_batch_id,
        status=price_import.status,
        status_url=status_url,
        result_url=f"{status_url}/result",
        replayed=not registration.created,
    )
