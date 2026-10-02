"""Схемы модуля `uploads` (docs/api/uploads/README.md).

Наружу camelCase — как во всём контуре Фазы 3 (API_CONVENTIONS §1).
"""

from __future__ import annotations

from uuid import UUID

from app.core.schemas import BaseSchema


class UploadedDocument(BaseSchema):
    """Документ и его версия в ответе загрузки."""

    document_id: UUID
    document_version_id: UUID
    check_item_id: UUID
    role: str
    file_name: str
    media_type: str
    size_bytes: int
    sha256: str
    status: str


class UploadResponse(BaseSchema):
    """Ответ `201` на `POST /uploads`.

    `uploadId` совпадает с `documentVersionId`: прямая multipart-загрузка
    завершается за один запрос, отдельной сущности сессии у неё нет. Различие
    появится вместе с resumable-загрузкой.
    """

    upload_id: UUID
    document: UploadedDocument


class UploadState(BaseSchema):
    """Ответ `GET /uploads/{uploadId}` — состояние загрузки.

    Прямая загрузка либо завершилась (`COMPLETED`), либо не оставила следа:
    отвергнутый файл не создаёт версию документа, поэтому `REJECTED` здесь не
    встречается — причина отказа приходит ошибкой самого `POST /uploads`.
    """

    upload_id: UUID
    status: str
    document: UploadedDocument
