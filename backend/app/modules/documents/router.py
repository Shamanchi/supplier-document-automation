"""Маршруты модуля `uploads` (docs/api/uploads/README.md).

Роутер занят транспортом: разбор multipart, предел размера, идемпотентность и
сборка ответа. Проверки содержимого и запись — в `service.py` и `signatures.py`.

Resumable-загрузка (сессии и части) объявлена контрактом как «MAY быть отложена
до появления реальной необходимости». Маршруты объявлены и отвечают `501`, а не
`404`: контракт виден в OpenAPI, а клиент получает внятную причину. Сессии
потребуют своей таблицы — в схеме её нет, это отдельная миграция.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import JSONResponse

from app.config import Settings
from app.core.concurrency import require_idempotency_key
from app.core.errors import ApiError
from app.core.idempotency import IdempotencyStore, idempotent, request_hash
from app.modules.documents.schemas import (
    UploadedDocument,
    UploadResponse,
    UploadState,
)
from app.modules.documents.service import (
    FileTooLarge,
    UploadErrorCode,
    UploadRequest,
    UploadResult,
    UploadsService,
    content_sha256,
    read_limited,
)
from app.modules.documents.storage import S3Storage, StoragePort
from app.modules.identity.dependencies import CurrentUser, SessionDep, SettingsDep

router = APIRouter(prefix="/uploads", tags=["uploads"])

IdempotencyKey = Annotated[str, Depends(require_idempotency_key)]

#: Клиент хранилища на процесс: boto3 держит пул соединений, и создавать его на
#: каждый запрос значило бы открывать новое соединение с MinIO под каждый файл.
_storage: StoragePort | None = None


def get_storage(settings: SettingsDep) -> StoragePort:
    """Хранилище документов.

    Тесты подменяют зависимость на `InMemoryStorage`, поэтому ветки «а если это
    тест» здесь нет: приложение всегда работает с настоящим хранилищем.
    """
    global _storage
    if _storage is None:
        _storage = S3Storage(settings)
    return _storage


StorageDep = Annotated[StoragePort, Depends(get_storage)]


def get_uploads_service(
    session: SessionDep, settings: SettingsDep, storage: StorageDep
) -> UploadsService:
    return UploadsService(session, settings, storage)


ServiceDep = Annotated[UploadsService, Depends(get_uploads_service)]

SESSIONS_NOT_IMPLEMENTED = (
    "Resumable-загрузка не реализована: файлы до предела размера принимает"
    " POST /uploads"
)


def _document_body(result: UploadResult) -> UploadedDocument:
    return UploadedDocument(
        document_id=result.document.id,
        document_version_id=result.version.id,
        check_item_id=result.document.check_item_id,
        role=result.document.role,
        file_name=result.version.file_name,
        media_type=result.version.media_type,
        size_bytes=result.version.size_bytes,
        sha256=result.version.sha256,
        status=result.version.status,
    )


def _sessions_not_implemented() -> None:
    raise ApiError(
        "UPLOAD_SESSIONS_NOT_IMPLEMENTED",
        SESSIONS_NOT_IMPLEMENTED,
        status_code=501,
    )


# Маршруты сессий объявлены раньше `/{upload_id}`: иначе `/uploads/sessions`
# разбирался бы как идентификатор загрузки и отвечал 422 вместо 501.


@router.post("/sessions", response_model=None)
async def create_session(user: CurrentUser) -> None:
    """Создание resumable-сессии. Не реализовано (см. docstring модуля)."""
    _sessions_not_implemented()


@router.put("/sessions/{upload_id}/parts/{part_no}", response_model=None)
async def upload_part(upload_id: UUID, part_no: int, user: CurrentUser) -> None:
    """Передача части файла. Не реализовано."""
    _sessions_not_implemented()


@router.post("/sessions/{upload_id}/complete", response_model=None)
async def complete_session(upload_id: UUID, user: CurrentUser) -> None:
    """Сборка частей в версию документа. Не реализовано."""
    _sessions_not_implemented()


@router.delete("/sessions/{upload_id}", response_model=None)
async def cancel_session(upload_id: UUID, user: CurrentUser) -> None:
    """Отмена незавершённой загрузки. Не реализовано."""
    _sessions_not_implemented()


@router.post("", status_code=status.HTTP_201_CREATED, response_model=None)
async def upload_document(
    request: Request,
    service: ServiceDep,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
    file: Annotated[UploadFile, File()],
    role: Annotated[Literal["REQUIREMENT", "FACT"], Form()],
    check_item_id: Annotated[UUID | None, Form(alias="checkItemId")] = None,
    check_item_id_snake: Annotated[UUID | None, Form(alias="check_item_id")] = None,
    document_id: Annotated[UUID | None, Form(alias="documentId")] = None,
) -> JSONResponse:
    """Загружает PDF/XLSX/DOCX и создаёт неизменяемую версию документа.

    Необязательный `documentId` заменяет файл существующего документа новой
    версией; без него загрузка заводит новый документ. Поле шире контракта,
    который перечисляет только `file`, `check_item_id` и `role`, — но без него
    `document_versions.version_no` и `active_version_id` не могли бы измениться
    ни при каком запросе, а замена перезалитого паспорта нужна экрану Orders.
    """
    item_id = _check_item_id(check_item_id, check_item_id_snake)
    data = await _read_file(file, settings)
    sha256 = content_sha256(data)
    store = IdempotencyStore(session)
    digest = request_hash(
        "POST",
        request.url.path,
        {
            "checkItemId": str(item_id),
            "role": role,
            "documentId": str(document_id) if document_id else None,
            "sha256": sha256,
        },
    )

    async def operation() -> dict:
        result = await service.upload(
            user,
            UploadRequest(
                check_item_id=item_id,
                role=role,
                file_name=file.filename or "document",
                data=data,
                sha256=sha256,
                document_id=document_id,
            ),
        )
        body = UploadResponse(
            upload_id=result.version.id, document=_document_body(result)
        )
        return body.model_dump(mode="json", by_alias=True)

    return await idempotent(
        store, idempotency_key, digest, status.HTTP_201_CREATED, operation
    )


@router.get("/{upload_id}", response_model=UploadState)
async def get_upload(
    upload_id: UUID, service: ServiceDep, user: CurrentUser
) -> UploadState:
    """Состояние загрузки.

    Прямая загрузка завершается в одном запросе, поэтому найденная запись всегда
    `COMPLETED`: отвергнутый файл версии документа не создаёт.
    """
    result = await service.get_upload(upload_id)
    return UploadState(
        upload_id=result.version.id, status="COMPLETED", document=_document_body(result)
    )


def _check_item_id(camel: UUID | None, snake: UUID | None) -> UUID:
    """Позиция из multipart-поля.

    Принимаются оба написания: контракт uploads в разделе «Multipart upload»
    называет поле `check_item_id`, а в таблице экрана Orders — `checkItemId`.
    Расхождение внутри одного документа отмечено как вопрос к согласованию;
    отвергать любое из написаний означало бы сломать один из разделов.
    """
    item_id = camel or snake
    if item_id is None:
        raise ApiError(
            "VALIDATION_ERROR",
            "Не указана позиция проверки: поле checkItemId",
            status_code=422,
            details={"field": "checkItemId"},
        )
    return item_id


async def _read_file(file: UploadFile, settings: Settings) -> bytes:
    try:
        return await read_limited(file.read, settings.upload_max_file_bytes)
    except FileTooLarge as too_large:
        limit = settings.upload_max_file_bytes
        raise ApiError(
            UploadErrorCode.FILE_TOO_LARGE,
            f"Файл больше допустимых {limit // (1024 * 1024)} МБ",
            status_code=413,
            details={"maxBytes": limit},
        ) from too_large
