"""HTTP-маршруты интеграции с Bitrix24.

Соответствие контракту (docs/api/integrations/bitrix/README.md §Маршруты) —
один к одному. Операции, зависящие от сети и rate limits, отвечают `202` и
идентификатором задания; синхронно выполняются только чтения.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from app.core.concurrency import require_idempotency_key
from app.core.idempotency import IdempotencyStore, idempotent, request_hash
from app.modules.identity.dependencies import (
    CurrentUser,
    SessionDep,
    SettingsDep,
)
from app.modules.integrations.bitrix.client import build_bitrix_client
from app.modules.integrations.bitrix.models import IntegrationJob
from app.modules.integrations.bitrix.port import BitrixPort
from app.modules.integrations.bitrix.schemas import (
    BatchKitStatusResponse,
    BitrixHealth,
    DealFile,
    DealFileList,
    DealResponse,
    ImportAccepted,
    ImportRequest,
    JobError,
    JobResponse,
    PrepareDealRequest,
    PreparedDealResponse,
    PublishAccepted,
    PublishRequest,
)
from app.modules.integrations.bitrix.service import (
    BitrixService,
    DealFileInfo,
    ImportSpec,
)

router = APIRouter(prefix="/integrations/bitrix", tags=["integrations"])
IdempotencyKey = Annotated[str, Depends(require_idempotency_key)]


def get_bitrix_client(settings: SettingsDep) -> BitrixPort | None:
    """Клиент портала. Тесты подменяют зависимость на стенд."""
    return build_bitrix_client(settings)


ClientDep = Annotated[BitrixPort | None, Depends(get_bitrix_client)]


def get_bitrix_service(
    session: SessionDep, settings: SettingsDep, client: ClientDep
) -> BitrixService:
    return BitrixService(session, settings, client)


ServiceDep = Annotated[BitrixService, Depends(get_bitrix_service)]


def _job_response(job: IntegrationJob) -> JobResponse:
    error = job.error or None
    return JobResponse(
        id=job.id,
        type=job.type,
        status=job.status,
        attempt=job.attempt,
        next_retry_at=job.next_retry_at,
        result=job.result,
        error=(
            JobError(
                code=str(error.get("code")),
                message=error.get("message"),
                retryable=bool(error.get("retryable")),
            )
            if error
            else None
        ),
    )


def _deal_file(info: DealFileInfo) -> DealFile:
    """Нормализованный файл сделки: подсказка типа и признаки импорта."""
    suggestion = info.suggestion
    return DealFile(
        external_file_id=info.file.external_file_id,
        name=info.file.name,
        size_bytes=info.file.size_bytes,
        media_type=info.file.media_type,
        source=info.file.source,
        created_at=info.file.created_at,
        suggested_kind=suggestion.kind,
        kind_confidence=suggestion.confidence,
        already_imported=info.already_imported,
        suggested_role=suggestion.role,
        role_confidence=suggestion.role_confidence,
        already_imported_document_id=info.document_id,
        already_imported_price_file_id=info.price_file_id,
    )


@router.get("/health", response_model=BitrixHealth)
async def bitrix_health(
    service: ServiceDep, settings: SettingsDep, user: CurrentUser
) -> BitrixHealth:
    """Состояние конфигурации и связи. Секреты не раскрываются."""
    health_status, error_code = await service.health()
    return BitrixHealth(
        configured=settings.bitrix_configured,
        mode=settings.bitrix_mode,
        status=health_status,
        checked_at=datetime.now(UTC),
        error_code=error_code,
    )


@router.get("/deals/{tender_id}", response_model=DealResponse)
async def get_deal(
    tender_id: str, service: ServiceDep, user: CurrentUser
) -> DealResponse:
    deal = await service.get_deal(tender_id)
    return DealResponse(
        tender_id=deal.tender_id,
        bitrix_deal_id=deal.deal_id,
        title=deal.title,
        customer=deal.customer,
        stage=deal.stage,
        assignee=deal.assignee,
        updated_at=deal.updated_at,
        external_url=deal.external_url,
    )


@router.get("/deals/{tender_id}/files", response_model=DealFileList)
async def list_deal_files(
    tender_id: str, service: ServiceDep, user: CurrentUser
) -> DealFileList:
    rows, notes = await service.list_files(tender_id)
    return DealFileList(
        notes=notes,
        items=[_deal_file(info) for info in rows],
    )


@router.post(
    "/deals/{tender_id}/prepare",
    status_code=status.HTTP_201_CREATED,
    response_model=PreparedDealResponse,
)
async def prepare_deal(
    tender_id: str,
    payload: PrepareDealRequest,
    request: Request,
    service: ServiceDep,
    session: SessionDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    """Подготовить локальную заявку и показать комплект файлов сделки."""
    digest = request_hash(
        "POST", request.url.path, payload.model_dump(mode="json", by_alias=True)
    )

    async def operation() -> dict:
        prepared = await service.prepare_deal(
            user,
            tender_id,
            check_batch_id=payload.check_batch_id,
            items=[
                (item.position_no, item.name, item.supplier) for item in payload.items
            ],
        )
        deal = prepared.deal
        return PreparedDealResponse(
            check_batch_id=prepared.batch.id,
            tender_ref_id=prepared.tender.id,
            created=prepared.created,
            status="FILES_AVAILABLE",
            deal=DealResponse(
                tender_id=deal.tender_id,
                bitrix_deal_id=deal.deal_id,
                title=deal.title,
                customer=deal.customer,
                stage=deal.stage,
                assignee=deal.assignee,
                updated_at=deal.updated_at,
                external_url=deal.external_url,
            ),
            files=[_deal_file(info) for info in prepared.files],
            notes=prepared.notes,
        ).model_dump(mode="json", by_alias=True)

    return await idempotent(
        IdempotencyStore(session),
        idempotency_key,
        digest,
        status.HTTP_201_CREATED,
        operation,
    )


@router.post(
    "/deals/{tender_id}/imports",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=None,
)
async def import_deal_files(
    tender_id: str,
    payload: ImportRequest,
    request: Request,
    service: ServiceDep,
    session: SessionDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    digest = request_hash(
        "POST", request.url.path, payload.model_dump(mode="json", by_alias=True)
    )

    async def operation() -> dict:
        job = await service.create_import(
            user,
            tender_id,
            check_batch_id=payload.check_batch_id,
            files=[
                ImportSpec(
                    external_file_id=file.external_file_id,
                    kind=file.kind,
                    check_item_id=file.check_item_id,
                    role=file.role,
                    price_document_type=file.price_document_type,
                    supplier_id=file.supplier_id,
                )
                for file in payload.files
            ],
            idempotency_key=idempotency_key,
        )
        return ImportAccepted(
            job_id=job.id, status=job.status, files_total=len(payload.files)
        ).model_dump(mode="json", by_alias=True)

    return await idempotent(
        IdempotencyStore(session),
        idempotency_key,
        digest,
        status.HTTP_202_ACCEPTED,
        operation,
    )


@router.get(
    "/check-batches/{batch_id}/status", response_model=BatchKitStatusResponse
)
async def get_batch_kit_status(
    batch_id: UUID, service: ServiceDep, user: CurrentUser
) -> BatchKitStatusResponse:
    """Состояние комплекта заявки из сделки.

    Читается только из локальных данных: восстановление статуса после
    перезагрузки страницы не должно упираться в доступность Bitrix24.
    """
    snapshot = await service.batch_kit_status(batch_id)
    return BatchKitStatusResponse(
        check_batch_id=snapshot.check_batch_id,
        status=snapshot.status,
        deal_linked=snapshot.deal_linked,
        deal_id=snapshot.deal_id,
        tender_id=snapshot.tender_id,
        files_available=snapshot.files_available,
        files_count=snapshot.files_count,
        documents_imported=snapshot.documents_imported,
        price_documents=snapshot.price_documents,
        price_ready=snapshot.price_ready,
        price_running=snapshot.price_running,
        price_attention=snapshot.price_attention,
    )


@router.post(
    "/check-batches/{batch_id}/publish",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=None,
)
async def publish_report(
    batch_id: UUID,
    payload: PublishRequest,
    request: Request,
    service: ServiceDep,
    session: SessionDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    digest = request_hash(
        "POST", request.url.path, payload.model_dump(mode="json", by_alias=True)
    )

    async def operation() -> dict:
        job = await service.create_publication(
            user,
            batch_id,
            artifact_ids=payload.report_artifact_ids,
            add_timeline_comment=payload.add_timeline_comment,
            include_key_mismatches=payload.include_key_mismatches,
            idempotency_key=idempotency_key,
        )
        return PublishAccepted(
            job_id=job.id, status=job.status, tender_id=job.tender_id or ""
        ).model_dump(mode="json", by_alias=True)

    return await idempotent(
        IdempotencyStore(session),
        idempotency_key,
        digest,
        status.HTTP_202_ACCEPTED,
        operation,
    )


@router.get("/imports/{job_id}", response_model=JobResponse)
async def get_import_job(
    job_id: UUID, service: ServiceDep, user: CurrentUser
) -> JobResponse:
    return _job_response(await service.get_job(job_id))


@router.get("/publications/{job_id}", response_model=JobResponse)
async def get_publication_job(
    job_id: UUID, service: ServiceDep, user: CurrentUser
) -> JobResponse:
    return _job_response(await service.get_job(job_id))


@router.post(
    "/publications/{job_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobResponse,
)
async def retry_publication(
    job_id: UUID, service: ServiceDep, user: CurrentUser
) -> JobResponse:
    """Ручной повтор задания по policy (контракт §Retry policy)."""
    return _job_response(await service.retry_job(user, job_id))
