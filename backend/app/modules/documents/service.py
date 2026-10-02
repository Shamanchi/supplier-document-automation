"""Загрузка документов позиции (docs/api/uploads/README.md).

Порядок операций задан контрактом (§Проверки Backend) и здесь соблюдается
буквально: сначала доступ и состояние позиции, затем карантин, затем проверки
содержимого и только потом — неизменяемая версия документа.

Почему файл сперва уезжает в карантинный бакет, хотя проверки идут по байтам в
памяти: отвергнутый файл должен оставаться доступным для разбора инцидента, а
основной бакет — содержать только проверенное. Карантинный объект удаляется
после успешного переезда, отвергнутый — остаётся.

Права и поиск позиции берутся из `ChecksService`: загрузка документа — это
изменение проверки, и правило «кто может менять проверки» должно быть одно.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.errors import ApiError
from app.modules.checks.models import ITEM_RUNNABLE_STATUSES, CheckItem
from app.modules.checks.service import ChecksService
from app.modules.documents.models import Document, DocumentVersion
from app.modules.documents.signatures import (
    DetectedFormat,
    FileRejected,
    ZipLimits,
    detect,
    safe_file_name,
)
from app.modules.documents.storage import StoragePort
from app.modules.identity.models import User


class UploadErrorCode(StrEnum):
    """Доменные коды (docs/api/uploads §Ошибки)."""

    CHECK_ITEM_NOT_FOUND = "CHECK_ITEM_NOT_FOUND"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    FILE_SIGNATURE_MISMATCH = "FILE_SIGNATURE_MISMATCH"
    DOCUMENT_LIMIT_EXCEEDED = "DOCUMENT_LIMIT_EXCEEDED"
    MALICIOUS_FILE_DETECTED = "MALICIOUS_FILE_DETECTED"
    DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
    UPLOAD_NOT_FOUND = "UPLOAD_NOT_FOUND"


#: HTTP-статус каждой причины отказа: контракт различает «формат не поддержан»
#: (415) и «содержимое противоречит заявленному» (422).
_REJECTION_STATUS: dict[str, int] = {
    UploadErrorCode.UNSUPPORTED_FILE_TYPE: 415,
    UploadErrorCode.FILE_SIGNATURE_MISMATCH: 422,
    UploadErrorCode.MALICIOUS_FILE_DETECTED: 422,
}

#: Статусы позиции, в которых допустимо менять состав документов. Совпадают с
#: теми, из которых позицию можно запустить: документы нужны прогону, и пока
#: прогон возможен — их правка осмысленна.
DOCUMENT_EDITABLE_STATUSES = ITEM_RUNNABLE_STATUSES


class FileTooLarge(Exception):
    """Поток превысил допустимый размер; читать дальше незачем."""


@dataclass(frozen=True)
class UploadRequest:
    check_item_id: uuid.UUID
    role: str
    file_name: str
    data: bytes
    #: SHA-256 содержимого. Считается один раз при приёме потока: он же служит
    #: отпечатком запроса для `Idempotency-Key`, и пересчитывать его в сервисе
    #: значило бы хешировать десятки мегабайт дважды.
    sha256: str
    #: Замена файла существующего документа: создаётся следующая версия.
    #: Без него загрузка заводит новый документ.
    document_id: uuid.UUID | None = None
    #: Происхождение документа (`document_source`). Импорт из сделки обязан
    #: отличаться от ручной загрузки: провенанс отчёта показывает, откуда
    #: пришёл файл.
    source: str = "UPLOAD"


@dataclass(frozen=True)
class UploadResult:
    document: Document
    version: DocumentVersion


def content_sha256(data: bytes) -> str:
    """Отпечаток содержимого файла (контракт uploads, пункт 7)."""
    return hashlib.sha256(data).hexdigest()


async def read_limited(read: Callable[[int], Awaitable[bytes]], limit: int) -> bytes:
    """Читает поток, обрывая чтение сразу за пределом.

    Предел проверяется по мере чтения, а не после: иначе гигабайтный файл сперва
    целиком оказался бы в памяти, а уже потом был отвергнут.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await read(64 * 1024):
        total += len(chunk)
        if total > limit:
            raise FileTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


class UploadsService:
    def __init__(
        self, session: AsyncSession, settings: Settings, storage: StoragePort
    ) -> None:
        self._session = session
        self._settings = settings
        self._storage = storage
        self._checks = ChecksService(session, settings)

    async def upload(self, user: User, request: UploadRequest) -> UploadResult:
        """Принимает файл и создаёт неизменяемую версию документа."""
        ChecksService.assert_can_write(user)
        item = await self._checks.get_item(request.check_item_id)
        self._assert_documents_editable(item)

        file_name = safe_file_name(request.file_name)
        quarantine_key = f"{item.id}/{uuid.uuid4()}"
        await self._storage.put(
            self._settings.quarantine_bucket,
            quarantine_key,
            request.data,
            content_type="application/octet-stream",
        )

        detected = self._detect_or_reject(request.data, file_name=file_name)

        duplicate = await self._existing_version(request, request.sha256)
        if duplicate is not None:
            # Тот же файл уже принят: карантинная копия не нужна, а новая версия
            # нарушила бы uq_docver_sha.
            await self._storage.delete(self._settings.quarantine_bucket, quarantine_key)
            return duplicate

        document = await self._target_document(request, item)
        version = await self._append_version(
            document,
            file_name=file_name,
            media_type=detected.media_type,
            extension=detected.extension,
            size_bytes=len(request.data),
            sha256=request.sha256,
        )

        # Переезд после записи в БД: если вставка упадёт, объект останется в
        # карантине, а не окажется в основном бакете без строки-владельца.
        await self._storage.move(
            src_bucket=self._settings.quarantine_bucket,
            src_key=quarantine_key,
            dst_bucket=self._settings.upload_bucket,
            dst_key=version.storage_key,
        )
        return UploadResult(document, version)

    async def get_upload(self, upload_id: uuid.UUID) -> UploadResult:
        """Состояние загрузки по `uploadId` (он же id версии документа)."""
        version = await self._session.get(DocumentVersion, upload_id)
        if version is None:
            raise ApiError(
                UploadErrorCode.UPLOAD_NOT_FOUND,
                "Загрузка не найдена",
                status_code=404,
                details={"uploadId": str(upload_id)},
            )
        document = await self._session.get(Document, version.document_id)
        if document is None:  # недостижимо: FK гарантирует владельца версии
            raise ApiError(
                UploadErrorCode.UPLOAD_NOT_FOUND,
                "Загрузка не найдена",
                status_code=404,
            )
        return UploadResult(document, version)

    # --- шаги загрузки --------------------------------------------------

    def _assert_documents_editable(self, item: CheckItem) -> None:
        if item.status not in DOCUMENT_EDITABLE_STATUSES:
            raise ApiError(
                UploadErrorCode.INVALID_STATE_TRANSITION,
                f"Документы нельзя менять в статусе {item.status}",
                status_code=409,
                details={"checkItemId": str(item.id), "status": item.status},
            )

    def _detect_or_reject(self, data: bytes, *, file_name: str) -> DetectedFormat:
        limits = ZipLimits(
            max_entries=self._settings.upload_max_zip_entries,
            max_uncompressed_bytes=self._settings.upload_max_zip_uncompressed_bytes,
        )
        try:
            return detect(data, file_name=file_name, limits=limits)
        except FileRejected as rejected:
            raise ApiError(
                rejected.reason,
                rejected.message,
                status_code=_REJECTION_STATUS[rejected.reason],
            ) from rejected

    async def _existing_version(
        self, request: UploadRequest, sha256: str
    ) -> UploadResult | None:
        """Повторная загрузка того же файла возвращает принятую версию.

        Сравнение идёт по содержимому в пределах позиции и роли: тот же паспорт,
        присланный дважды, не должен превращаться ни в новый документ, ни в
        новую версию (schema.sql: `uq_docver_sha`).
        """
        query = (
            select(Document, DocumentVersion)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .where(
                Document.check_item_id == request.check_item_id,
                Document.role == request.role,
                DocumentVersion.sha256 == sha256,
            )
        )
        if request.document_id is not None:
            query = query.where(Document.id == request.document_id)
        found = (await self._session.execute(query.limit(1))).first()
        return UploadResult(found[0], found[1]) if found else None

    async def _target_document(
        self, request: UploadRequest, item: CheckItem
    ) -> Document:
        if request.document_id is not None:
            return await self._document_of_item(request, item)

        used = await self._session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.check_item_id == item.id, Document.role == request.role)
        )
        limit = self._settings.upload_max_documents_per_role
        if (used or 0) >= limit:
            raise ApiError(
                UploadErrorCode.DOCUMENT_LIMIT_EXCEEDED,
                f"Документов роли {request.role} уже {used} при пределе {limit}",
                status_code=422,
                details={"checkItemId": str(item.id), "role": request.role},
            )

        document = Document(
            id=uuid.uuid4(),
            check_item_id=item.id,
            role=request.role,
            source=request.source,
        )
        self._session.add(document)
        await self._session.flush()
        return document

    async def _document_of_item(
        self, request: UploadRequest, item: CheckItem
    ) -> Document:
        """Документ, версию которого требуется заменить."""
        document = await self._session.get(Document, request.document_id)
        # Роль и позиция проверяются вместе: документ чужой позиции не должен
        # отличаться в ответе от несуществующего.
        if (
            document is None
            or document.check_item_id != item.id
            or document.role != request.role
        ):
            raise ApiError(
                UploadErrorCode.DOCUMENT_NOT_FOUND,
                "Документ не найден у этой позиции",
                status_code=404,
                details={"documentId": str(request.document_id)},
            )
        return document

    async def _append_version(
        self,
        document: Document,
        *,
        file_name: str,
        media_type: str,
        extension: str,
        size_bytes: int,
        sha256: str,
    ) -> DocumentVersion:
        next_no = await self._session.scalar(
            select(func.coalesce(func.max(DocumentVersion.version_no), 0) + 1).where(
                DocumentVersion.document_id == document.id
            )
        )

        version_id = uuid.uuid4()
        version = DocumentVersion(
            id=version_id,
            document_id=document.id,
            version_no=next_no or 1,
            file_name=file_name,
            media_type=media_type,
            size_bytes=size_bytes,
            sha256=sha256,
            # Ключ собирается из идентификаторов: имя файла в путь не попадает
            # (контракт, пункт 5), поэтому path traversal невозможен по построению.
            storage_key=(
                f"{document.check_item_id}/{document.id}/{version_id}.{extension}"
            ),
            status="UPLOADED",
        )
        self._session.add(version)
        await self._session.flush()

        # Активной становится последняя версия: карточка позиции и предусловия
        # запуска смотрят именно на неё.
        document.active_version_id = version.id
        await self._session.flush()
        return version
