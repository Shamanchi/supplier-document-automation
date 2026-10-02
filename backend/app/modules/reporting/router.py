"""Отчёты (docs/api/reports).

Расхождение с контрактом, продиктованное схемой: `reportId` — это
идентификатор **прогона** REPORTING, а не строки `report_snapshots`. Строка
snapshot защищена триггером `prevent_modify` и появляется только готовой, со
своим итоговым статусом; отдать её идентификатор в ответе `202` невозможно —
её ещё нет. Прогон же существует с момента запроса, у него есть статус, этап и
события, поэтому именно он и есть «запрос отчёта». Отмечено в docs/todo.

PDF не поддерживается: нужен отдельный рендерер и кириллические шрифты в
образе. Запрос PDF отвергается явно, а не выдаёт XLSX вместо него.

Сводный отчёт заявки отвечает `201` с готовым снимком, а не `202`: своего
прогона у него нет (`pipeline_runs.check_item_id` NOT NULL), а сборка идёт
синхронно. Читается он тем же `GET /reports/{id}` — идентификатор снимка там
разрешается наравне с идентификатором прогона.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.core.concurrency import require_idempotency_key
from app.core.errors import ApiError
from app.core.idempotency import IdempotencyStore, idempotent, request_hash
from app.core.schemas import BaseSchema
from app.modules.checks.models import PipelineRun, StageRun
from app.modules.checks.service import ChecksService
from app.modules.documents.router import get_storage
from app.modules.documents.storage import StoragePort
from app.modules.identity.dependencies import CurrentUser, SessionDep, SettingsDep
from app.modules.reporting.models import ReportArtifact, ReportSnapshot
from app.modules.reporting.service import (
    SUPPORTED_FORMATS,
    ReportingError,
    ReportingService,
)
from app.modules.reporting.xlsx import MEDIA_TYPE

item_router = APIRouter(prefix="/check-items", tags=["reports"])
router = APIRouter(prefix="/reports", tags=["reports"])
batch_router = APIRouter(prefix="/check-batches", tags=["reports"])
artifacts_router = APIRouter(prefix="/report-artifacts", tags=["reports"])

IdempotencyKey = Annotated[str, Depends(require_idempotency_key)]

#: Статус прогона → статус генерации отчёта (reports §Состояния).
_RUN_STATUS = {
    "QUEUED": "QUEUED",
    "RUNNING": "GENERATING",
    "FAILED": "FAILED",
    "CANCELLED": "FAILED",
}


class ReportRequest(BaseSchema):
    formats: list[str] = ["XLSX"]
    language: str = "ru"
    include_evidence_quotes: bool = True
    publish_to_bitrix: bool = False


class ReportAccepted(BaseSchema):
    report_id: UUID
    status: str
    requested_formats: list[str]


class ArtifactResponse(BaseSchema):
    id: UUID
    format: str
    file_name: str
    size_bytes: int | None = None
    sha256: str | None = None
    created_at: str


class ReportResponse(BaseSchema):
    id: UUID
    snapshot_id: UUID | None = None
    status: str
    checksum: str | None = None
    artifacts: list[ArtifactResponse] = []


def get_checks_service(session: SessionDep, settings: SettingsDep) -> ChecksService:
    return ChecksService(session, settings)


ServiceDep = Annotated[ChecksService, Depends(get_checks_service)]
StorageDep = Annotated[StoragePort, Depends(get_storage)]


@item_router.post(
    "/{item_id}/reports", status_code=status.HTTP_202_ACCEPTED, response_model=None
)
async def request_report(
    item_id: UUID,
    payload: ReportRequest,
    request: Request,
    service: ServiceDep,
    session: SessionDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    """Запрашивает отчёт позиции: создаёт прогон REPORTING."""
    _assert_supported(payload.formats)

    store = IdempotencyStore(session)
    digest = request_hash(
        "POST", request.url.path, payload.model_dump(by_alias=True, mode="json")
    )

    async def operation() -> dict:
        started = await service.start_reporting(item_id, user)
        return ReportAccepted(
            report_id=started.pipeline_run_id,
            status="QUEUED",
            requested_formats=payload.formats,
        ).model_dump(by_alias=True, mode="json")

    return await idempotent(
        store, idempotency_key, digest, status.HTTP_202_ACCEPTED, operation
    )


@batch_router.post(
    "/{batch_id}/reports", status_code=status.HTTP_201_CREATED, response_model=None
)
async def request_batch_report(
    batch_id: UUID,
    payload: ReportRequest,
    request: Request,
    service: ServiceDep,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    """Собирает сводный отчёт заявки: сразу и целиком."""
    _assert_supported(payload.formats)
    service.assert_can_write(user)
    store = IdempotencyStore(session)
    digest = request_hash(
        "POST", request.url.path, payload.model_dump(by_alias=True, mode="json")
    )

    async def operation() -> dict:
        batch = await service.get_batch(batch_id)
        reporting = ReportingService(session, settings, storage)
        try:
            stats = await reporting.build_batch(batch)
        except ReportingError as error:
            raise ApiError(error.code, error.message, status_code=409) from error
        # Заявка закрывается сводным отчётом — так же, как позиция закрывается
        # своим: этап REPORTING переводит её в COMPLETED.
        batch.status = "COMPLETED"
        batch.completed_at = datetime.now(UTC)
        batch.version += 1
        snapshot = await session.get(ReportSnapshot, stats.report_snapshot_id)
        return ReportResponse(
            id=stats.report_snapshot_id,
            snapshot_id=stats.report_snapshot_id,
            status=snapshot.status if snapshot else "COMPLETED",
            checksum=snapshot.checksum if snapshot else None,
            artifacts=await _artifacts(session, stats.report_snapshot_id),
        ).model_dump(by_alias=True, mode="json")

    return await idempotent(
        store, idempotency_key, digest, status.HTTP_201_CREATED, operation
    )


@router.get("/{report_id}", response_model=ReportResponse)
async def get_report(
    report_id: UUID, session: SessionDep, service: ServiceDep, user: CurrentUser
) -> ReportResponse:
    """Состояние генерации и метаданные готовых файлов."""
    direct = await _batch_snapshot(session, service, report_id)
    if direct is not None:
        return ReportResponse(
            id=report_id,
            snapshot_id=direct.id,
            status=direct.status,
            checksum=direct.checksum,
            artifacts=await _artifacts(session, direct.id),
        )
    run = await _run(session, service, report_id)
    snapshot = await _snapshot(session, run)
    if snapshot is None:
        return ReportResponse(
            id=report_id, status=_RUN_STATUS.get(run.status, "GENERATING")
        )
    artifacts = await _artifacts(session, snapshot.id)
    return ReportResponse(
        id=report_id,
        snapshot_id=snapshot.id,
        status=snapshot.status,
        checksum=snapshot.checksum,
        artifacts=artifacts,
    )


@router.get("/{report_id}/artifacts", response_model=list[ArtifactResponse])
async def report_artifacts(
    report_id: UUID, session: SessionDep, service: ServiceDep, user: CurrentUser
) -> list[ArtifactResponse]:
    direct = await _batch_snapshot(session, service, report_id)
    if direct is not None:
        return await _artifacts(session, direct.id)
    run = await _run(session, service, report_id)
    snapshot = await _snapshot(session, run)
    if snapshot is None:
        return []
    return await _artifacts(session, snapshot.id)


@artifacts_router.get("/{artifact_id}/download", response_model=None)
async def download_artifact(
    artifact_id: UUID,
    session: SessionDep,
    service: ServiceDep,
    settings: SettingsDep,
    storage: StorageDep,
    user: CurrentUser,
) -> Response:
    """Отдаёт файл отчёта.

    Постоянной публичной ссылки у файла нет: доступ проверяется по позиции при
    каждом скачивании (reports §Download).
    """
    artifact = await session.get(ReportArtifact, artifact_id)
    if artifact is None:
        raise ApiError(
            "REPORT_ARTIFACT_NOT_FOUND",
            "Файл отчёта не найден",
            status_code=404,
            details={"artifactId": str(artifact_id)},
        )
    snapshot = await session.get(ReportSnapshot, artifact.report_snapshot_id)
    if snapshot is not None and snapshot.check_item_id is not None:
        await service.get_item(snapshot.check_item_id)
    elif snapshot is not None and snapshot.check_batch_id is not None:
        await service.get_batch(snapshot.check_batch_id)

    content = await storage.get(settings.upload_bucket, artifact.storage_key)
    # Имя файла кодируется по RFC 5987, как в скачивании сертификата:
    # заголовок HTTP обязан быть ASCII, а кириллица в имени отчёта роняет
    # ответ ещё до отправки.
    encoded_name = quote(artifact.file_name)
    return Response(
        content=content,
        media_type=MEDIA_TYPE,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
            "ETag": f'"{artifact.checksum}"',
            "Cache-Control": "private, no-store",
        },
    )


async def _batch_snapshot(
    session, service: ChecksService, report_id: UUID
) -> ReportSnapshot | None:
    """Снимок сводного отчёта по его собственному идентификатору."""
    snapshot = await session.get(ReportSnapshot, report_id)
    if snapshot is None or snapshot.check_item_id is not None:
        return None
    await service.get_batch(snapshot.check_batch_id)
    return snapshot


def _assert_supported(formats: list[str]) -> None:
    unsupported = [item for item in formats if item not in SUPPORTED_FORMATS]
    if unsupported:
        raise ApiError(
            "UNSUPPORTED_REPORT_FORMAT",
            "Формат отчёта не поддерживается: " + ", ".join(unsupported),
            status_code=422,
            details={"supported": list(SUPPORTED_FORMATS)},
        )


async def _run(session, service: ChecksService, report_id: UUID) -> PipelineRun:
    run = await session.get(PipelineRun, report_id)
    stages = (
        set(
            (
                await session.scalars(
                    select(StageRun.stage).where(StageRun.pipeline_run_id == report_id)
                )
            ).all()
        )
        if run is not None
        else set()
    )
    if run is None or "REPORTING" not in stages:
        raise ApiError(
            "REPORT_NOT_FOUND",
            "Запрос отчёта не найден",
            status_code=404,
            details={"reportId": str(report_id)},
        )
    await service.get_item(run.check_item_id)
    return run


async def _snapshot(session, run: PipelineRun) -> ReportSnapshot | None:
    """Snapshot, собранный этим прогоном.

    Связь — через `payload.versions.reportingRunId`: своей колонки под прогон у
    `report_snapshots` в схеме нет, а снимок обязан помнить, чем он собран.
    """
    snapshots = (
        await session.scalars(
            select(ReportSnapshot)
            .where(ReportSnapshot.check_item_id == run.check_item_id)
            .order_by(ReportSnapshot.created_at.desc())
        )
    ).all()
    for snapshot in snapshots:
        versions = (snapshot.payload or {}).get("versions") or {}
        if versions.get("reportingRunId") == str(run.id):
            return snapshot
    return None


async def _artifacts(session, snapshot_id: UUID) -> list[ArtifactResponse]:
    rows = (
        await session.scalars(
            select(ReportArtifact)
            .where(ReportArtifact.report_snapshot_id == snapshot_id)
            .order_by(ReportArtifact.format)
        )
    ).all()
    return [
        ArtifactResponse(
            id=row.id,
            format=row.format,
            file_name=row.file_name,
            size_bytes=row.size_bytes,
            sha256=row.checksum,
            created_at=row.created_at.isoformat().replace("+00:00", "Z"),
        )
        for row in rows
    ]
