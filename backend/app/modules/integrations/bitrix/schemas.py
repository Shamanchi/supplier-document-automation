"""Публичные схемы модуля интеграции.

Ни одна схема не повторяет структуру ответа Bitrix24: наружу отдаётся только
нормализованная модель (контракт §Карточка сделки). Секреты не описаны здесь
вовсе — их нельзя вернуть даже случайно.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.core.schemas import BaseSchema, Confidence
from app.modules.checks.models import MAX_ITEMS_PER_BATCH

#: Тип файла сделки: роль Ф3 либо ценовой документ Ф2. `PRICE` не является
#: ролью `CheckItem`: ценовой файл может относиться сразу к нескольким позициям.
FileKind = Literal["REQUIREMENT", "FACT", "PRICE"]
#: Роль файла в заявке Ф3 — её менеджер подтверждает для `CheckItem`.
DocumentRole = Literal["REQUIREMENT", "FACT"]
#: Назначение ценового документа Ф2 (docs/PHASE2_PHASE3_ADAPTATION_PLAN.md §4).
PriceDocumentKind = Literal["PRICE_LIST", "SUPPLIER_QUOTE"]

JobStatus = Literal[
    "QUEUED", "RUNNING", "RETRY_SCHEDULED", "COMPLETED", "FAILED", "CANCELLED"
]


class BitrixHealth(BaseSchema):
    """Состояние конфигурации и последней проверки связи — без секретов."""

    configured: bool
    mode: Literal["disabled", "fake", "webhook"]
    status: Literal["OK", "ERROR", "DISABLED"]
    checked_at: datetime
    #: Код класса сбоя (`BitrixTransportCode`), если связь не установилась.
    error_code: str | None = None


class DealResponse(BaseSchema):
    tender_id: str
    bitrix_deal_id: str
    title: str | None = None
    customer: str | None = None
    stage: str | None = None
    assignee: str | None = None
    updated_at: datetime | None = None
    external_url: str | None = None


class DealFile(BaseSchema):
    """Файл сделки. `suggestedKind` — только предложение, решает менеджер.

    `suggestedRole`/`roleConfidence` сохранены для сценария Ф3: это тот же
    `suggestedKind`, если тип укладывается в роль позиции (REQUIREMENT/FACT).
    Для `PRICE` роль пуста, а признак импорта смотрит и на ценовой контур.
    """

    external_file_id: str
    name: str
    size_bytes: int | None = None
    media_type: str | None = None
    source: str
    created_at: datetime | None = None
    suggested_kind: FileKind | None = None
    kind_confidence: Confidence | None = None
    already_imported: bool = False
    suggested_role: DocumentRole | None = None
    role_confidence: Confidence | None = None
    already_imported_document_id: uuid.UUID | None = None
    already_imported_price_file_id: uuid.UUID | None = None


class DealFileList(BaseSchema):
    items: list[DealFile] = Field(default_factory=list)
    #: Текст комментариев ленты сделки. Показывается менеджеру: там бывает
    #: перечень вложений, которые Фаза 1 не смогла положить на портал.
    notes: list[str] = Field(default_factory=list)


#: Статус комплекта заявки (docs/PHASE2_PHASE3_ADAPTATION_PLAN.md §7).
KitStatus = Literal[
    "BITRIX_NOT_LINKED",
    "BITRIX_DEAL_FOUND",
    "FILES_AVAILABLE",
    "DOCUMENTS_IMPORTED",
    "PRICE_IMPORT_RUNNING",
    "PRICE_REVIEW_REQUIRED",
    "PRICE_READY",
    "READY_FOR_CALCULATION",
]


class BatchKitStatusResponse(BaseSchema):
    """Состояние комплекта заявки: связь, документы Ф3 и ценовые документы Ф2."""

    check_batch_id: uuid.UUID
    status: KitStatus
    deal_linked: bool
    deal_id: str | None = None
    tender_id: str | None = None
    files_available: bool = False
    files_count: int | None = None
    documents_imported: int = 0
    price_documents: int = 0
    price_ready: int = 0
    price_running: int = 0
    price_attention: int = 0


class PrepareItem(BaseSchema):
    """Позиция для нового draft-пакета, создаваемого из сделки."""

    position_no: int | None = Field(default=None, ge=1, le=MAX_ITEMS_PER_BATCH)
    name: str = Field(min_length=1, max_length=500)
    supplier: str | None = Field(default=None, max_length=500)


class PrepareDealRequest(BaseSchema):
    """Подготовка локальной заявки к работе с комплектом сделки.

    `checkBatchId` используется для уже созданной локальной заявки. Если он
    отсутствует, сервер создаёт новый пакет только при наличии позиций: пустой
    `CheckBatch` противоречит инварианту домена «1..5 позиций».
    """

    check_batch_id: uuid.UUID | None = None
    items: list[PrepareItem] = Field(
        default_factory=list, max_length=MAX_ITEMS_PER_BATCH
    )


class PreparedDealResponse(BaseSchema):
    """Сделка, локальная заявка и доступный комплект файлов."""

    check_batch_id: uuid.UUID
    tender_ref_id: uuid.UUID
    created: bool
    status: Literal["FILES_AVAILABLE"]
    deal: DealResponse
    files: list[DealFile] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ImportFile(BaseSchema):
    """Выбранный менеджером файл сделки и его назначение.

    `kind` — подтверждённый тип файла. Для Ф3 (`REQUIREMENT`/`FACT`) нужны
    позиция `check_item_id` и роль; для `PRICE` — назначение
    `price_document_type` и локальный поставщик `supplier_id`. Старые
    клиенты, присылающие только `role`, трактуются как Ф3: тип выводится из
    роли, поэтому контракт остаётся совместимым.
    """

    external_file_id: str
    kind: FileKind | None = None
    check_item_id: uuid.UUID | None = None
    role: DocumentRole | None = None
    price_document_type: PriceDocumentKind | None = None
    supplier_id: uuid.UUID | None = None


class ImportRequest(BaseSchema):
    check_batch_id: uuid.UUID
    files: list[ImportFile] = Field(min_length=1)


class ImportAccepted(BaseSchema):
    job_id: uuid.UUID
    status: JobStatus
    files_total: int


class PublishRequest(BaseSchema):
    report_artifact_ids: list[uuid.UUID] = Field(min_length=1)
    add_timeline_comment: bool = True
    include_key_mismatches: bool = True


class PublishAccepted(BaseSchema):
    job_id: uuid.UUID
    status: JobStatus
    tender_id: str


class JobError(BaseSchema):
    code: str
    message: str | None = None
    retryable: bool = False


class JobResponse(BaseSchema):
    id: uuid.UUID
    type: Literal["IMPORT_FILE", "PUBLISH_REPORT"]
    status: JobStatus
    attempt: int
    next_retry_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: JobError | None = None
