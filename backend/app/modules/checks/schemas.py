"""Схемы модулей `check-batches` и `check-items`.

Наружу поля идут в camelCase (BaseSchema, API_CONVENTIONS.md §1), уверенность
классификатора — процентами строкой через тип `Confidence` (§3).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from app.core.schemas import BaseSchema, Confidence
from app.modules.checks.models import MAX_ITEMS_PER_BATCH

#: Режимы пакетного запуска (check-batches §Запуск пакета).
RunMode = Literal["READY_ITEMS_ONLY", "ALL_OR_NOTHING"]

#: Статусы сравнений, которые обязана содержать summary даже при нулях.
COMPARISON_STATUSES: tuple[str, ...] = (
    "COMPLIANT",
    "NON_COMPLIANT",
    "REVIEW_REQUIRED",
    "NOT_FOUND_IN_FACT",
    "NOT_REQUIRED",
)


class ItemCreate(BaseSchema):
    """Позиция при создании пакета или добавлении в пакет."""

    # Номер необязателен: без него сервер занимает первый свободный. Это
    # удобнее для клиента и снимает гонку за `uq_item_position`.
    position_no: int | None = Field(default=None, ge=1, le=MAX_ITEMS_PER_BATCH)
    name: str = Field(min_length=1, max_length=500)
    equipment_type_id: UUID | None = None
    supplier: str | None = Field(default=None, max_length=500)


class BatchCreate(BaseSchema):
    tender_id: str | None = Field(default=None, max_length=100)
    customer: str | None = Field(default=None, max_length=500)
    supplier: str | None = Field(default=None, max_length=500)
    title: str | None = Field(default=None, max_length=500)
    items: list[ItemCreate] = Field(min_length=1, max_length=MAX_ITEMS_PER_BATCH)


class BatchUpdate(BaseSchema):
    """Правка реквизитов черновика. Незаданные поля не меняются.

    Отличить «не передали» от «передали null» позволяет `model_fields_set`:
    сервис смотрит именно его, а не значения.
    """

    tender_id: str | None = Field(default=None, max_length=100)
    customer: str | None = Field(default=None, max_length=500)
    supplier: str | None = Field(default=None, max_length=500)
    title: str | None = Field(default=None, max_length=500)


class ItemUpdate(BaseSchema):
    name: str | None = Field(default=None, min_length=1, max_length=500)
    supplier: str | None = Field(default=None, max_length=500)
    equipment_type_id: UUID | None = None


class EquipmentTypeConfirm(BaseSchema):
    """Подтверждение типа оборудования.

    Пустое поле означает «подтвердить предложенный», иначе — заменить на
    указанный (check-items §equipment-type/confirm).
    """

    equipment_type_id: UUID | None = None


class RunRequest(BaseSchema):
    mode: RunMode = "READY_ITEMS_ONLY"
    catalog_version: str | None = None


class ItemRunRequest(BaseSchema):
    catalog_version: str | None = None
    force_reprocess: bool = False


class Author(BaseSchema):
    id: UUID
    display_name: str


class EquipmentTypeRef(BaseSchema):
    id: UUID
    name: str


class ProposedEquipmentType(BaseSchema):
    id: UUID
    name: str
    confidence: Confidence


class DocumentCounts(BaseSchema):
    requirement: int = 0
    fact: int = 0


class ActiveRun(BaseSchema):
    id: UUID
    started_at: datetime | None = None
    stage: str | None = None


class BatchItem(BaseSchema):
    """Позиция в составе ответа пакета (краткая форма)."""

    id: UUID
    position_no: int
    name: str
    status: str


class BatchResponse(BaseSchema):
    id: UUID
    tender_id: str | None = None
    title: str | None = None
    customer: str | None = None
    supplier: str | None = None
    status: str
    final_verdict: str
    version: int
    author: Author
    items: list[BatchItem]
    created_at: datetime
    updated_at: datetime


class CheckItemResponse(BaseSchema):
    """Карточка позиции (check-items §Карточка позиции)."""

    id: UUID
    batch_id: UUID
    position_no: int
    name: str
    supplier: str | None = None
    status: str
    version: int
    equipment_type: EquipmentTypeRef | None = None
    proposed_equipment_types: list[ProposedEquipmentType]
    documents: DocumentCounts
    active_run: ActiveRun | None = None


class DocumentSummary(BaseSchema):
    """Документ позиции в списке `GET /check-items/{id}/documents`."""

    document_id: UUID
    document_version_id: UUID | None = None
    role: str
    file_name: str | None = None
    media_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    status: str | None = None
    created_at: datetime


class ItemDocuments(BaseSchema):
    requirement: list[DocumentSummary]
    fact: list[DocumentSummary]


class ParameterRawValue(BaseSchema):
    name: str
    value_text: str | None = None
    unit_text: str | None = None


class ParameterNormalizedValue(BaseSchema):
    id: UUID
    canonical_parameter_id: UUID | None = None
    canonical_name: str | None = None
    value_kind: str
    typed_value: dict[str, Any] | None = None
    normalized_ok: bool
    version: int


class ParameterIssue(BaseSchema):
    issue_type: str
    description: str | None = None
    severity: str


class ParameterEvidence(BaseSchema):
    fragment_id: UUID
    quote: str
    quote_start: int | None = None
    quote_end: int | None = None
    value_start: int | None = None
    value_end: int | None = None
    verified: bool
    locator: dict[str, Any] | None = None
    #: Подтверждает ли цитата текущее значение параметра. После ручной правки
    #: машинная цитата остаётся в истории (`extraction_evidence` неизменяема),
    #: но за подтверждение правленого значения её выдавать нельзя.
    current: bool = True


class ItemParameterResponse(BaseSchema):
    id: UUID
    role: str
    raw: ParameterRawValue
    normalized: ParameterNormalizedValue | None = None
    extraction_method: str
    confidence: str | None = None
    issues: list[ParameterIssue] = Field(default_factory=list)
    evidence: list[ParameterEvidence] = Field(default_factory=list)
    manually_edited: bool = False
    excluded: bool
    version: int
    created_at: datetime


class ItemParametersPage(BaseSchema):
    items: list[ItemParameterResponse]
    next_cursor: str | None = None


class RunAccepted(BaseSchema):
    """Ответ `202` на запуск позиции (check-items §Запуск)."""

    check_item_id: UUID
    pipeline_run_id: UUID
    status: str
    events_url: str


class RunRejection(BaseSchema):
    """Позиция, которую запустить не удалось: код и причина."""

    check_item_id: UUID
    code: str
    message: str


class BatchRunAccepted(BaseSchema):
    """Ответ `202` на пакетный запуск: что пошло в очередь, что нет."""

    batch_id: UUID
    started: list[RunAccepted]
    rejected: list[RunRejection]


class CancelAccepted(BaseSchema):
    batch_id: UUID | None = None
    check_item_ids: list[UUID]
    status: Literal["CANCEL_REQUESTED"] = "CANCEL_REQUESTED"


class PipelineRunSummary(BaseSchema):
    id: UUID
    run_no: int
    status: str
    catalog_version: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime


class BatchSummaryResponse(BaseSchema):
    batch_id: UUID
    items_total: int
    items_by_status: dict[str, int]
    comparisons: dict[str, int]
    computed_verdict: str
    manager_verdict: str
