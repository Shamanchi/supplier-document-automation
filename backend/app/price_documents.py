"""Регистрация ценового документа: файл → версия → импорт.

Один и тот же контур Ф2 обслуживает два источника: скачивание из сделки
Bitrix24 (`source="BITRIX"`) и мостик «заявка → прайс/счёт» из интерфейса
(`source="UPLOAD"`). Различаются они происхождением и ключом идемпотентности,
а всё остальное — сверка формата с расширением, дедупликация по sha256,
карантин, нумерация версий, назначение документа и постановка разбора — общее.
Поэтому логика живёт здесь, а `modules/integrations/bitrix/pricing.py` оставлен
тонкой обёрткой с прежней сигнатурой.

Назначение версии (`document_type`) решает, где документ будет виден:
`PRICE_LIST` — общий каталог поставщика, попадает в подбор только после
активации администратором; `SUPPLIER_QUOTE` — разовый счёт/КП, действует
исключительно в своей заявке (`check_batch_id`) и активировать его нельзя.

Разбор запускает вызывающий: строки должны быть зафиксированы в БД раньше, чем
задача попадёт в очередь, иначе воркер не увидит импорт.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.import_auth import internal_service_client
from app.models import (
    ORIGINAL_PROJECT_ID,
    PriceDocumentType,
    PriceFile,
    PriceImport,
    PriceImportEvent,
    PriceImportStatus,
    PriceVersion,
    Supplier,
    VersionStatus,
)
from app.modules.documents.signatures import (
    FileRejected,
    ZipLimits,
    detect,
    safe_file_name,
)
from app.modules.documents.storage import StoragePort

#: Назначения, допустимые для ценового документа Ф2.
DOCUMENT_TYPES = frozenset(item.value for item in PriceDocumentType)

#: Откуда пришёл файл. Значения совпадают с `ck_price_files_source`: новые
#: источники требуют миграции, поэтому список закрыт.
SOURCE_BITRIX = "BITRIX"
SOURCE_UPLOAD = "UPLOAD"
SOURCES = frozenset({SOURCE_BITRIX, SOURCE_UPLOAD})


class PriceDocumentError(Exception):
    """Ценовой документ нельзя зарегистрировать (нет поставщика, формат и т.п.)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def detect_or_reject(data: bytes, *, file_name: str, settings: Settings) -> None:
    """Сверяет содержимое с расширением именем, как это делает ручная загрузка."""
    limits = ZipLimits(
        max_entries=settings.upload_max_zip_entries,
        max_uncompressed_bytes=settings.upload_max_zip_uncompressed_bytes,
    )
    try:
        detect(data, file_name=file_name, limits=limits)
    except FileRejected as rejected:
        raise PriceDocumentError(rejected.message) from rejected


@dataclass(slots=True, frozen=True)
class PriceDocumentRegistration:
    """Результат регистрации: сам импорт и признак «создан сейчас».

    `created=False` означает повтор: тот же файл уже зарегистрирован (по ключу
    идемпотентности или по совпадению sha256 в том же назначении). Повтор не
    требует новой постановки разбора в очередь.
    """

    price_import: PriceImport
    created: bool


async def register_price_document(
    session: AsyncSession,
    settings: Settings,
    storage: StoragePort,
    *,
    supplier_id: uuid.UUID,
    document_type: str,
    check_batch_id: uuid.UUID | None,
    source: str,
    idempotency_key: str,
    file_name: str,
    media_type: str | None,
    data: bytes,
    project_id: uuid.UUID = ORIGINAL_PROJECT_ID,
    currency_hint: str | None = None,
    vat_rate_percent: Decimal | None = None,
    origin: dict[str, str | None] | None = None,
) -> PriceDocumentRegistration:
    """Создаёт `PriceFile` → `PriceVersion` → `PriceImport` для ценового файла.

    `SUPPLIER_QUOTE` привязывается к заявке (`check_batch_id`) и никогда не
    становится общим активным прайсом: активация в любом случае выполняется
    отдельным явным действием администратора.
    """
    if source not in SOURCES:
        raise PriceDocumentError(f"Неизвестный источник ценового документа {source}")
    if document_type not in DOCUMENT_TYPES:
        raise PriceDocumentError(f"Неизвестное назначение ценового документа {document_type}")
    if document_type == PriceDocumentType.supplier_quote.value and check_batch_id is None:
        raise PriceDocumentError("SUPPLIER_QUOTE требует привязки к заявке")
    if document_type == PriceDocumentType.price_list.value and check_batch_id is not None:
        raise PriceDocumentError("PRICE_LIST не привязывается к заявке")
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        raise PriceDocumentError("Поставщик не найден")

    name = safe_file_name(file_name)
    detect_or_reject(data, file_name=name, settings=settings)
    sha256 = hashlib.sha256(data).hexdigest()
    mime = media_type or "application/octet-stream"

    client = await internal_service_client(session)
    existing = (
        await session.execute(
            select(PriceImport).where(
                PriceImport.service_client_id == client.id,
                PriceImport.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return PriceDocumentRegistration(price_import=existing, created=False)

    duplicate = (
        await session.execute(
            select(PriceImport)
            .join(PriceFile, PriceFile.id == PriceImport.price_file_id)
            .join(PriceVersion, PriceVersion.id == PriceImport.price_version_id)
            .where(
                PriceFile.supplier_id == supplier.id,
                PriceFile.sha256 == sha256,
                PriceVersion.document_type == document_type,
                PriceVersion.check_batch_id == check_batch_id,
            )
            .order_by(PriceImport.created_at.asc(), PriceImport.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        return PriceDocumentRegistration(price_import=duplicate, created=False)

    quarantine_key = f"{source.lower()}/{sha256}/{uuid.uuid4()}/{name}"
    storage_key = f"sha256/{sha256}/{name}"
    await storage.put(
        settings.quarantine_bucket,
        quarantine_key,
        data,
        content_type=mime,
    )

    moved = False
    try:
        price_file = PriceFile(
            project_id=project_id,
            supplier_id=supplier.id,
            original_filename=name,
            storage_key=storage_key,
            sha256=sha256,
            size_bytes=len(data),
            mime=mime,
            source=source,
        )
        session.add(price_file)
        await session.flush()

        max_version = (
            await session.execute(
                select(func.coalesce(func.max(PriceVersion.version_no), 0)).where(
                    PriceVersion.supplier_id == supplier.id
                )
            )
        ).scalar_one()
        version = PriceVersion(
            supplier_id=supplier.id,
            price_file_id=price_file.id,
            version_no=max_version + 1,
            status=VersionStatus.uploaded.value,
            document_type=document_type,
            check_batch_id=check_batch_id,
        )
        session.add(version)
        await session.flush()

        price_import = PriceImport(
            service_client_id=client.id,
            supplier_id=supplier.id,
            price_file_id=price_file.id,
            price_version_id=version.id,
            idempotency_key=idempotency_key,
            request_fingerprint=sha256,
            locale="ru-RU",
            policy="strict",
            currency_hint=currency_hint,
            vat_rate_percent=vat_rate_percent,
        )
        session.add(price_import)
        await session.flush()
        await storage.move(
            src_bucket=settings.quarantine_bucket,
            src_key=quarantine_key,
            dst_bucket=settings.minio_bucket_price_files,
            dst_key=storage_key,
        )
        moved = True
        session.add(
            PriceImportEvent(
                price_import_id=price_import.id,
                event_type="import_created",
                to_status=PriceImportStatus.queued.value,
                payload={
                    "file_sha256": sha256,
                    "source": source,
                    "documentType": document_type,
                    **(origin or {}),
                },
            )
        )
        return PriceDocumentRegistration(price_import=price_import, created=True)
    except Exception:
        # The caller rolls back the database transaction. Remove the temporary
        # object too, so failed registration leaves no ownerless upload.
        await storage.delete(
            settings.minio_bucket_price_files if moved else settings.quarantine_bucket,
            storage_key if moved else quarantine_key,
        )
        raise


def _order_idempotency_key(
    explicit: str | None,
    *,
    data: bytes,
    supplier_external_id: str,
    document_type: str,
    check_batch_id: uuid.UUID | None,
) -> str:
    """Ключ идемпотентности загрузки со страницы заявки.

    Явный `Idempotency-Key` (его присылает интерфейс) повторяет поведение
    сервисного API: тот же ключ — тот же импорт. Без заголовка ключ выводится из
    содержимого файла и назначения, поэтому «менеджер нажал дважды» не заводит
    второй разбор, а тот же файл для другой заявки или поставщика — заводит.
    """
    if explicit:
        digest = hashlib.sha256(explicit.encode()).hexdigest()
    else:
        digest = hashlib.sha256(
            (
                f"{supplier_external_id}:{check_batch_id or '-'}:{document_type}:"
                f"{hashlib.sha256(data).hexdigest()}"
            ).encode()
        ).hexdigest()
    return f"order-{digest}"


async def register_order_price_document(
    session: AsyncSession,
    settings: Settings,
    storage: StoragePort,
    *,
    document_type: str,
    check_batch_id: uuid.UUID | None,
    supplier_external_id: str,
    file_name: str,
    media_type: str | None,
    data: bytes,
    supplier_name: str | None = None,
    idempotency_key: str | None = None,
    currency_hint: str | None = None,
    vat_rate_percent: Decimal | None = None,
    project_id: uuid.UUID = ORIGINAL_PROJECT_ID,
    actor: str | None = None,
) -> PriceDocumentRegistration:
    """Ценовой документ, загруженный со страницы заявки.

    Поставщик ищется по `external_id` в том же внутреннем скоупе, что и
    остальные UI-импорты (`internal_service_client`), и заводится, если такого
    ещё нет: у менеджера нет сервисного клиента, а идемпотентности нужен
    устойчивый владелец. Квоты проверяет вызывающий — здесь только БД и
    хранилище, чтобы контур был проверяем без Redis.
    """
    if document_type not in DOCUMENT_TYPES:
        raise PriceDocumentError(f"Неизвестное назначение ценового документа {document_type}")
    if document_type == PriceDocumentType.supplier_quote.value and check_batch_id is None:
        raise PriceDocumentError("SUPPLIER_QUOTE требует привязки к заявке")
    if document_type == PriceDocumentType.price_list.value and check_batch_id is not None:
        raise PriceDocumentError("PRICE_LIST не привязывается к заявке")

    external_id = supplier_external_id.strip()
    if not external_id:
        raise PriceDocumentError("Не указан код поставщика")
    display_name = (supplier_name or "").strip() or external_id

    client = await internal_service_client(session)
    supplier = (
        await session.execute(
            select(Supplier).where(
                Supplier.service_client_id == client.id,
                Supplier.external_id == external_id,
            )
        )
    ).scalar_one_or_none()
    if supplier is None:
        supplier = Supplier(
            service_client_id=client.id,
            external_id=external_id,
            code=f"svc-{client.id.hex[:12]}-{hashlib.sha256(external_id.encode()).hexdigest()[:24]}",
            name=display_name,
        )
        session.add(supplier)
        await session.flush()

    return await register_price_document(
        session,
        settings,
        storage,
        supplier_id=supplier.id,
        document_type=document_type,
        check_batch_id=check_batch_id,
        source=SOURCE_UPLOAD,
        idempotency_key=_order_idempotency_key(
            idempotency_key,
            data=data,
            supplier_external_id=external_id,
            document_type=document_type,
            check_batch_id=check_batch_id,
        ),
        file_name=file_name,
        media_type=media_type,
        data=data,
        project_id=project_id,
        currency_hint=currency_hint,
        vat_rate_percent=vat_rate_percent,
        origin={
            "checkBatchId": str(check_batch_id) if check_batch_id else None,
            "uploadedBy": actor,
        },
    )
