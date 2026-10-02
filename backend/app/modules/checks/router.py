"""Маршруты модулей `check-batches` и `check-items`.

Роутер занят только транспортом: разбор запроса, ETag/If-Match, идемпотентность
и сборка ответа. Инварианты и переходы состояний — в `service.py`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.concurrency import (
    format_etag,
    require_idempotency_key,
    require_if_match,
)
from app.core.errors import ApiError
from app.core.idempotency import IdempotencyStore, idempotent, request_hash
from app.core.pagination import CursorPage, CursorParams, cursor_params
from app.db import SessionLocal
from app.matching.order_binding import ensure_request_for_batch
from app.modules.checks.models import CheckBatch, CheckItem
from app.modules.checks.schemas import (
    ActiveRun,
    Author,
    BatchCreate,
    BatchItem,
    BatchResponse,
    BatchRunAccepted,
    BatchSummaryResponse,
    BatchUpdate,
    CancelAccepted,
    CheckItemResponse,
    DocumentCounts,
    DocumentSummary,
    EquipmentTypeConfirm,
    EquipmentTypeRef,
    ItemCreate,
    ItemDocuments,
    ItemParameterResponse,
    ItemParametersPage,
    ItemRunRequest,
    ItemUpdate,
    ParameterEvidence,
    ParameterIssue,
    ParameterNormalizedValue,
    ParameterRawValue,
    PipelineRunSummary,
    RunAccepted,
    RunRejection,
    RunRequest,
)
from app.modules.checks.service import ChecksService
from app.modules.extraction.models import (
    ExtractionEvidence,
    ExtractionRun,
    RawParameter,
    SourceFragment,
)
from app.modules.identity.dependencies import CurrentUser, SessionDep, SettingsDep
from app.modules.identity.models import User
from app.modules.normalization.models import NormalizationIssue, NormalizedParameter
from app.modules.parameters.models import ParameterRevision
from app.modules.pipeline.events import TERMINAL_EVENTS, render_event
from app.modules.pipeline.models import PipelineEvent

batches_router = APIRouter(prefix="/check-batches", tags=["check-batches"])
items_router = APIRouter(prefix="/check-items", tags=["check-items"])

IdempotencyKey = Annotated[str, Depends(require_idempotency_key)]
IfMatch = Annotated[int, Depends(require_if_match)]

#: Сколько событий отдаётся за один опрос — ограничение на догон длинной истории.
SSE_BATCH_LIMIT = 200


def get_checks_service(session: SessionDep, settings: SettingsDep) -> ChecksService:
    return ChecksService(session, settings)


ServiceDep = Annotated[ChecksService, Depends(get_checks_service)]


def events_url(item_id: UUID) -> str:
    return f"/api/v1/check-items/{item_id}/events"


# --- сборка ответов ---------------------------------------------------------


def _batch_body(
    batch: CheckBatch, author: User | None, tender_id: str | None
) -> BatchResponse:
    return BatchResponse(
        id=batch.id,
        tender_id=tender_id,
        title=batch.title,
        customer=batch.customer,
        supplier=batch.supplier,
        status=batch.status,
        final_verdict=batch.final_verdict,
        version=batch.version,
        author=Author(
            id=batch.author_id,
            # Автор мог быть удалён: пакет остаётся, ответ не должен падать.
            display_name=author.display_name if author else "—",
        ),
        items=[
            BatchItem(
                id=item.id,
                position_no=item.position_no,
                name=item.name,
                status=item.status,
            )
            for item in batch.items
        ],
        created_at=batch.created_at,
        updated_at=batch.updated_at,
    )


async def _one_batch(service: ChecksService, batch: CheckBatch) -> BatchResponse:
    authors = await service.authors_of([batch])
    tenders = await service.tender_ids_of([batch])
    return _batch_body(
        batch, authors.get(batch.author_id), tenders.get(batch.tender_ref_id)
    )


async def _item_body(service: ChecksService, item: CheckItem) -> CheckItemResponse:
    counts = await service.document_counts(item.id)
    active = await service.active_run(item.id)
    confirmed = await service.equipment_type(item)
    proposed = await service.proposed_types(item)

    return CheckItemResponse(
        id=item.id,
        batch_id=item.batch_id,
        position_no=item.position_no,
        name=item.name,
        supplier=item.supplier,
        status=item.status,
        version=item.version,
        equipment_type=(
            EquipmentTypeRef(id=confirmed.id, name=confirmed.name)
            if confirmed
            else None
        ),
        proposed_equipment_types=[
            {
                "id": kind.id,
                "name": kind.name,
                # Уверенность хранится долей 0..1; тип Confidence отдаёт проценты.
                "confidence": (item.type_confidence or 0) * 100,
            }
            for kind in proposed
        ],
        documents=DocumentCounts(
            requirement=counts["REQUIREMENT"], fact=counts["FACT"]
        ),
        active_run=(
            ActiveRun(id=active[0].id, started_at=active[0].started_at, stage=active[1])
            if active
            else None
        ),
    )


def _json(body: dict, status_code: int, version: int | None = None) -> JSONResponse:
    headers = {"ETag": format_etag(version)} if version is not None else None
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def _dump(model) -> dict:
    """Тело ответа в том же виде, в каком его отдал бы `response_model`."""
    return model.model_dump(mode="json", by_alias=True)


# --- check-batches ----------------------------------------------------------


@batches_router.post("", status_code=status.HTTP_201_CREATED, response_model=None)
async def create_batch(
    payload: BatchCreate,
    request: Request,
    service: ServiceDep,
    session: SessionDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    """Создать пакет проверки вместе с позициями (1..5)."""
    store = IdempotencyStore(session)
    digest = request_hash("POST", request.url.path, _dump(payload))

    async def operation() -> dict:
        batch = await service.create_batch(user, payload)
        # Заявка и её расчётный лист существуют 1:1 с момента создания
        # (задача №7, решение №1): вкладки цены/КП контура Фазы 2 читают
        # `GET /orders/{batch_id}` сразу, без отдельной команды «связать».
        await ensure_request_for_batch(session, batch.id)
        return _dump(await _one_batch(service, batch))

    return await idempotent(
        store, idempotency_key, digest, status.HTTP_201_CREATED, operation
    )


@batches_router.get("", response_model=CursorPage[BatchResponse])
async def list_batches(
    service: ServiceDep,
    user: CurrentUser,
    params: Annotated[CursorParams, Depends(cursor_params)],
    batch_status: Annotated[str | None, Query(alias="status")] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> CursorPage[BatchResponse]:
    """Список пакетов для экрана Orders: фильтры `status`/`search`, cursor."""
    batches, next_cursor = await service.list_batches(
        status=batch_status, search=search, cursor=params.cursor, limit=params.limit
    )
    authors = await service.authors_of(batches)
    tenders = await service.tender_ids_of(batches)
    return CursorPage[BatchResponse](
        items=[
            _batch_body(
                batch, authors.get(batch.author_id), tenders.get(batch.tender_ref_id)
            )
            for batch in batches
        ],
        next_cursor=next_cursor,
    )


@batches_router.get("/{batch_id}", response_model=None)
async def get_batch(
    batch_id: UUID, service: ServiceDep, user: CurrentUser
) -> JSONResponse:
    """Пакет с позициями. Версия сущности отдаётся в ETag."""
    batch = await service.get_batch(batch_id)
    body = await _one_batch(service, batch)
    return _json(_dump(body), status.HTTP_200_OK, batch.version)


@batches_router.patch("/{batch_id}", response_model=None)
async def update_batch(
    batch_id: UUID,
    payload: BatchUpdate,
    service: ServiceDep,
    user: CurrentUser,
    if_match: IfMatch,
) -> JSONResponse:
    """Правка реквизитов черновика. Требует актуальный ETag в `If-Match`."""
    batch = await service.update_batch(batch_id, if_match, payload, user)
    body = await _one_batch(service, batch)
    return _json(_dump(body), status.HTTP_200_OK, batch.version)


@batches_router.delete(
    "/{batch_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_batch(batch_id: UUID, service: ServiceDep, user: CurrentUser) -> None:
    """Удаляет черновик без прогонов; пакет с историей уходит в архив."""
    await service.delete_batch(batch_id, user)


@batches_router.post(
    "/{batch_id}/items", status_code=status.HTTP_201_CREATED, response_model=None
)
async def add_item(
    batch_id: UUID,
    payload: ItemCreate,
    service: ServiceDep,
    user: CurrentUser,
) -> JSONResponse:
    """Добавить позицию в черновик (не больше пяти на пакет)."""
    item = await service.add_item(batch_id, payload, user)
    body = await _item_body(service, item)
    return _json(_dump(body), status.HTTP_201_CREATED, item.version)


@batches_router.post(
    "/{batch_id}/run", status_code=status.HTTP_202_ACCEPTED, response_model=None
)
async def run_batch(
    batch_id: UUID,
    payload: RunRequest,
    request: Request,
    service: ServiceDep,
    session: SessionDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    """Запустить готовые позиции пакета. Политика запуска — в `mode`."""
    store = IdempotencyStore(session)
    digest = request_hash("POST", request.url.path, _dump(payload))

    async def operation() -> dict:
        result = await service.run_batch(batch_id, payload, user)
        return _dump(
            BatchRunAccepted(
                batch_id=batch_id,
                started=[
                    RunAccepted(
                        check_item_id=run.check_item_id,
                        pipeline_run_id=run.pipeline_run_id,
                        status=run.status,
                        events_url=events_url(run.check_item_id),
                    )
                    for run in result.started
                ],
                rejected=[
                    RunRejection(
                        check_item_id=item.check_item_id,
                        code=item.code,
                        message=item.message,
                    )
                    for item in result.rejected
                ],
            )
        )

    return await idempotent(
        store, idempotency_key, digest, status.HTTP_202_ACCEPTED, operation
    )


@batches_router.post(
    "/{batch_id}/cancel", status_code=status.HTTP_202_ACCEPTED, response_model=None
)
async def cancel_batch(
    batch_id: UUID, service: ServiceDep, user: CurrentUser
) -> CancelAccepted:
    """Запросить отмену незавершённых позиций пакета."""
    cancelled = await service.cancel_batch(batch_id, user)
    return CancelAccepted(batch_id=batch_id, check_item_ids=cancelled)


@batches_router.get("/{batch_id}/summary", response_model=BatchSummaryResponse)
async def batch_summary(
    batch_id: UUID, service: ServiceDep, user: CurrentUser
) -> BatchSummaryResponse:
    """Агрегат статусов позиций и распределения сравнений."""
    return BatchSummaryResponse(**await service.batch_summary(batch_id))


# --- check-items ------------------------------------------------------------


@items_router.get("/{item_id}", response_model=None)
async def get_item(
    item_id: UUID, service: ServiceDep, user: CurrentUser
) -> JSONResponse:
    """Карточка позиции: документы, тип оборудования, активный прогон."""
    item = await service.get_item(item_id)
    body = await _item_body(service, item)
    return _json(_dump(body), status.HTTP_200_OK, item.version)


@items_router.patch("/{item_id}", response_model=None)
async def update_item(
    item_id: UUID,
    payload: ItemUpdate,
    service: ServiceDep,
    user: CurrentUser,
    if_match: IfMatch,
) -> JSONResponse:
    """Правка позиции-черновика: имя, поставщик, тип оборудования."""
    item = await service.update_item(item_id, if_match, payload, user)
    body = await _item_body(service, item)
    return _json(_dump(body), status.HTTP_200_OK, item.version)


@items_router.get("/{item_id}/documents", response_model=ItemDocuments)
async def item_documents(
    item_id: UUID, service: ServiceDep, user: CurrentUser
) -> ItemDocuments:
    """Документы позиции, сгруппированные по роли."""
    await service.get_item(item_id)
    grouped = await service.item_documents(item_id)
    return ItemDocuments(
        requirement=[_document(row) for row in grouped["REQUIREMENT"]],
        fact=[_document(row) for row in grouped["FACT"]],
    )


@items_router.get("/{item_id}/parameters", response_model=ItemParametersPage)
async def item_parameters(
    item_id: UUID,
    session: SessionDep,
    service: ServiceDep,
    user: CurrentUser,
    role: Annotated[str | None, Query()] = None,
    include_excluded: Annotated[bool, Query(alias="includeExcluded")] = True,
) -> ItemParametersPage:
    """Извлечённые параметры последнего успешного run по каждой роли документа.

    Endpoint intentionally read-only: ручные add/revision/exclude/restore живут
    в модуле `parameters` (docs/api/parameters) и меняют состояние позиции.
    """
    await service.get_item(item_id)
    if role is not None and role not in {"REQUIREMENT", "FACT"}:
        raise ApiError(
            "INVALID_PARAMETER_ROLE",
            f"Неизвестная роль параметра: {role}",
            status_code=422,
            details={"allowed": ["REQUIREMENT", "FACT"]},
        )
    rows = await _parameter_rows(
        session, item_id, role=role, include_excluded=include_excluded
    )
    return ItemParametersPage(items=await _parameter_responses(session, rows))


def _document(row: dict) -> DocumentSummary:
    return DocumentSummary(
        document_id=row["id"],
        document_version_id=row["version_id"],
        role=row["role"],
        file_name=row["file_name"],
        media_type=row["media_type"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        status=row["status"],
        created_at=row["created_at"],
    )


async def _parameter_rows(
    session: AsyncSession,
    item_id: UUID,
    *,
    role: str | None,
    include_excluded: bool,
) -> list[tuple[RawParameter, NormalizedParameter | None]]:
    runs_by_role = (
        select(
            RawParameter.role.label("role"),
            ExtractionRun.id.label("run_id"),
            ExtractionRun.created_at.label("created_at"),
        )
        .join(ExtractionRun, ExtractionRun.id == RawParameter.extraction_run_id)
        .where(
            RawParameter.check_item_id == item_id,
            ExtractionRun.status == "COMPLETED",
        )
        .group_by(RawParameter.role, ExtractionRun.id, ExtractionRun.created_at)
        .subquery()
    )
    ranked = (
        select(
            runs_by_role.c.role,
            runs_by_role.c.run_id,
            func.row_number()
            .over(
                partition_by=runs_by_role.c.role,
                order_by=(
                    runs_by_role.c.created_at.desc(),
                    runs_by_role.c.run_id.desc(),
                ),
            )
            .label("position"),
        )
        .subquery()
    )

    query = (
        select(RawParameter, NormalizedParameter)
        .join(
            ranked,
            and_(
                ranked.c.role == RawParameter.role,
                ranked.c.run_id == RawParameter.extraction_run_id,
                ranked.c.position == 1,
            ),
        )
        .outerjoin(
            NormalizedParameter,
            NormalizedParameter.raw_parameter_id == RawParameter.id,
        )
        .where(RawParameter.check_item_id == item_id)
        .order_by(RawParameter.role, RawParameter.created_at, RawParameter.id)
    )
    if role is not None:
        query = query.where(RawParameter.role == role)
    if not include_excluded:
        query = query.where(RawParameter.excluded.is_(False))

    return list((await session.execute(query)).all())


async def _parameter_responses(
    session: AsyncSession, rows: list[tuple[RawParameter, NormalizedParameter | None]]
) -> list[ItemParameterResponse]:
    raw_ids = [raw.id for raw, _ in rows]
    normalized_ids = [normalized.id for _, normalized in rows if normalized]
    issues = await _parameter_issues(session, normalized_ids)
    evidence = await _parameter_evidence(
        session, {raw.id: raw.value_revision for raw, _ in rows}
    )
    manual_ids = await _manual_parameter_ids(session, raw_ids)

    return [
        ItemParameterResponse(
            id=raw.id,
            role=raw.role,
            raw=ParameterRawValue(
                name=raw.name_text,
                value_text=raw.value_text,
                unit_text=raw.unit_text,
            ),
            normalized=(
                ParameterNormalizedValue(
                    id=normalized.id,
                    canonical_parameter_id=normalized.canonical_parameter_id,
                    canonical_name=normalized.canonical_name,
                    value_kind=normalized.value_kind,
                    typed_value=normalized.typed_value,
                    normalized_ok=normalized.normalized_ok,
                    version=normalized.version,
                )
                if normalized
                else None
            ),
            extraction_method=raw.extraction_method,
            confidence=None if raw.confidence is None else str(raw.confidence),
            issues=issues.get(normalized.id if normalized else None, []),
            evidence=evidence.get(raw.id, []),
            manually_edited=raw.extraction_method == "MANUAL" or raw.id in manual_ids,
            excluded=raw.excluded,
            version=raw.version,
            created_at=raw.created_at,
        )
        for raw, normalized in rows
    ]


async def _parameter_issues(
    session: AsyncSession, normalized_ids: list[UUID]
) -> dict[UUID | None, list[ParameterIssue]]:
    if not normalized_ids:
        return {}
    rows = (
        await session.scalars(
            select(NormalizationIssue)
            .where(NormalizationIssue.normalized_parameter_id.in_(normalized_ids))
            .order_by(NormalizationIssue.created_at, NormalizationIssue.id)
        )
    ).all()
    result: dict[UUID | None, list[ParameterIssue]] = {}
    for issue in rows:
        result.setdefault(issue.normalized_parameter_id, []).append(
            ParameterIssue(
                issue_type=issue.issue_type,
                description=issue.description,
                severity=issue.severity,
            )
        )
    return result


async def _manual_parameter_ids(
    session: AsyncSession, raw_ids: list[UUID]
) -> set[UUID]:
    if not raw_ids:
        return set()
    rows = (
        await session.scalars(
            select(ParameterRevision.raw_parameter_id)
            .where(ParameterRevision.raw_parameter_id.in_(raw_ids))
            .distinct()
        )
    ).all()
    return set(rows)


async def _parameter_evidence(
    session: AsyncSession, current_revisions: dict[UUID, int]
) -> dict[UUID, list[ParameterEvidence]]:
    """Evidence параметров позиции с пометкой актуальности цитаты.

    Ручная правка не удаляет машинную цитату — таблица неизменяема, — но и не
    выдаёт её за подтверждение нового значения: актуальны только строки того же
    поколения (`value_revision`), что и сам параметр.
    """
    if not current_revisions:
        return {}
    rows = (
        await session.execute(
            select(ExtractionEvidence, SourceFragment)
            .join(
                SourceFragment,
                SourceFragment.id == ExtractionEvidence.source_fragment_id,
            )
            .where(ExtractionEvidence.raw_parameter_id.in_(list(current_revisions)))
            .order_by(ExtractionEvidence.created_at, ExtractionEvidence.id)
        )
    ).all()
    result: dict[UUID, list[ParameterEvidence]] = {}
    for evidence, fragment in rows:
        locator = dict(fragment.locator or {})
        locator.setdefault("kind", fragment.locator_kind)
        result.setdefault(evidence.raw_parameter_id, []).append(
            ParameterEvidence(
                fragment_id=fragment.id,
                quote=evidence.quote,
                quote_start=evidence.quote_start,
                quote_end=evidence.quote_end,
                value_start=evidence.value_start,
                value_end=evidence.value_end,
                verified=evidence.verified,
                locator=locator,
                current=(
                    evidence.raw_parameter_value_revision
                    == current_revisions[evidence.raw_parameter_id]
                ),
            )
        )
    return result


@items_router.post(
    "/{item_id}/run", status_code=status.HTTP_202_ACCEPTED, response_model=None
)
async def run_item(
    item_id: UUID,
    payload: ItemRunRequest,
    request: Request,
    service: ServiceDep,
    session: SessionDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    """Запустить новый прогон позиции."""
    store = IdempotencyStore(session)
    digest = request_hash("POST", request.url.path, _dump(payload))

    async def operation() -> dict:
        started = await service.run_item(item_id, payload, user)
        return _dump(
            RunAccepted(
                check_item_id=started.check_item_id,
                pipeline_run_id=started.pipeline_run_id,
                status=started.status,
                events_url=events_url(started.check_item_id),
            )
        )

    return await idempotent(
        store, idempotency_key, digest, status.HTTP_202_ACCEPTED, operation
    )


@items_router.post(
    "/{item_id}/retry", status_code=status.HTTP_202_ACCEPTED, response_model=None
)
async def retry_item(
    item_id: UUID,
    payload: ItemRunRequest,
    request: Request,
    service: ServiceDep,
    session: SessionDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    """Повторить FAILED/CANCELLED прогон новым запуском."""
    store = IdempotencyStore(session)
    digest = request_hash("POST", request.url.path, _dump(payload))

    async def operation() -> dict:
        started = await service.retry_item(item_id, payload, user)
        return _dump(
            RunAccepted(
                check_item_id=started.check_item_id,
                pipeline_run_id=started.pipeline_run_id,
                status=started.status,
                events_url=events_url(started.check_item_id),
            )
        )

    return await idempotent(
        store, idempotency_key, digest, status.HTTP_202_ACCEPTED, operation
    )


@items_router.post(
    "/{item_id}/cancel", status_code=status.HTTP_202_ACCEPTED, response_model=None
)
async def cancel_item(
    item_id: UUID, service: ServiceDep, user: CurrentUser
) -> CancelAccepted:
    """Запросить отмену активного прогона позиции."""
    cancelled = await service.cancel_item(item_id, user)
    return CancelAccepted(check_item_ids=cancelled)


@items_router.get("/{item_id}/runs", response_model=list[PipelineRunSummary])
async def item_runs(
    item_id: UUID, service: ServiceDep, user: CurrentUser
) -> list[PipelineRunSummary]:
    """История прогонов позиции: свежие первыми."""
    await service.get_item(item_id)
    return [
        PipelineRunSummary(
            id=run.id,
            run_no=run.run_no,
            status=run.status,
            catalog_version=code,
            started_at=run.started_at,
            finished_at=run.finished_at,
            created_at=run.created_at,
        )
        for run, code in await service.item_runs(item_id)
    ]


@items_router.post("/{item_id}/equipment-type/confirm", response_model=None)
async def confirm_equipment_type(
    item_id: UUID,
    payload: EquipmentTypeConfirm,
    service: ServiceDep,
    user: CurrentUser,
    if_match: IfMatch,
) -> JSONResponse:
    """Подтвердить предложенный тип оборудования либо заменить его."""
    item = await service.confirm_equipment_type(item_id, if_match, payload, user)
    body = await _item_body(service, item)
    return _json(_dump(body), status.HTTP_200_OK, item.version)


@items_router.post(
    "/{item_id}/parameters/confirm",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=None,
)
async def confirm_parameters(
    item_id: UUID,
    request: Request,
    service: ServiceDep,
    session: SessionDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    """Подтвердить разобранные параметры и запустить сравнение.

    Канон §16: переход `AWAITING_PARAMETER_REVIEW → MATCHING` возможен только
    этой командой, и она идемпотентна — повтор с тем же ключом отдаёт тот же
    прогон, а не заводит второй.
    """
    store = IdempotencyStore(session)
    digest = request_hash("POST", request.url.path, {})

    async def operation() -> dict:
        started = await service.confirm_parameters(item_id, user)
        return _dump(
            RunAccepted(
                check_item_id=started.check_item_id,
                pipeline_run_id=started.pipeline_run_id,
                status=started.status,
                events_url=events_url(started.check_item_id),
            )
        )

    return await idempotent(
        store, idempotency_key, digest, status.HTTP_202_ACCEPTED, operation
    )


@items_router.get("/{item_id}/events", response_model=None)
async def item_events(
    item_id: UUID,
    service: ServiceDep,
    settings: SettingsDep,
    user: CurrentUser,
    request: Request,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    last_event_id_query: Annotated[int | None, Query(alias="lastEventId")] = None,
) -> StreamingResponse:
    """SSE прогресса прогона (check-items §SSE).

    Курсор — `Last-Event-ID`: после обрыва клиент присылает последний
    полученный `id` и добирает пропущенное. Дубликаты возможны (клиент обязан их
    переживать), потери — нет: события читаются из журнала, а не из состояния.

    Поток закрывается на терминальном событии прогона: ждать больше нечего, а
    висящее соединение занимало бы ресурсы и у сервера, и у прокси. Клиент, как
    и требует контракт, перечитывает позицию через REST — SSE лишь уведомление.
    """
    await service.get_item(item_id)
    cursor = _event_cursor(last_event_id, last_event_id_query)

    async def stream() -> AsyncIterator[str]:
        position = cursor
        deadline = time.monotonic() + settings.sse_max_lifetime_seconds
        while time.monotonic() < deadline:
            if await request.is_disconnected():
                return
            # Своя короткая сессия на каждый опрос: держать соединение с базой
            # открытым всё время подписки нельзя — пул кончится раньше клиентов.
            async with SessionLocal() as session:
                events = (
                    await session.scalars(
                        select(PipelineEvent)
                        .where(
                            PipelineEvent.check_item_id == item_id,
                            PipelineEvent.id > position,
                        )
                        .order_by(PipelineEvent.id)
                        .limit(SSE_BATCH_LIMIT)
                    )
                ).all()
                finished = await _run_finished(session, item_id, after=position)
            for event in events:
                position = event.id
                yield render_event(event)
                if event.event_type in TERMINAL_EVENTS:
                    return
            if not events:
                if finished:
                    # Клиент переподключился после конца прогона: новых событий
                    # не будет, и держать соединение до таймаута бессмысленно.
                    return
                # Комментарий-heartbeat не даёт прокси закрыть тихое соединение.
                yield ": keep-alive\n\n"
            await asyncio.sleep(settings.sse_poll_interval_seconds)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx иначе буферизует поток и прогресс приходит рывками в конце.
            "X-Accel-Buffering": "no",
        },
    )


def _event_cursor(header_value: str | None, query_value: int | None) -> int:
    """Курсор из `Last-Event-ID`; заголовок важнее query, мусор — как «с начала».

    EventSource умеет только заголовок, но у него нельзя задать заголовки при
    первом подключении, поэтому query-параметр остаётся для явного докачивания.
    """
    if header_value is not None:
        try:
            return max(0, int(header_value))
        except ValueError:
            return 0
    return max(0, query_value or 0)


async def _run_finished(session: AsyncSession, item_id: UUID, *, after: int) -> bool:
    """Последнее событие позиции терминально и клиент его уже получил."""
    last = await session.scalar(
        select(PipelineEvent)
        .where(PipelineEvent.check_item_id == item_id)
        .order_by(PipelineEvent.id.desc())
        .limit(1)
    )
    return last is not None and last.id <= after and last.event_type in TERMINAL_EVENTS
