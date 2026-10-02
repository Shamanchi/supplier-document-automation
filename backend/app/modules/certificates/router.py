"""HTTP routes for the certificate registry."""

from __future__ import annotations

from datetime import date
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse

from app.config import Settings
from app.core.concurrency import require_idempotency_key, require_if_match
from app.core.errors import ApiError
from app.core.idempotency import IdempotencyStore, idempotent, request_hash
from app.core.pagination import CursorPage, CursorParams, cursor_params
from app.modules.certificates.models import Certificate
from app.modules.certificates.schemas import (
    CertificateEvidence,
    CertificateParameter,
    CertificateReprocessAccepted,
    CertificateResponse,
)
from app.modules.certificates.service import (
    CertificateParameterData,
    CertificateProcessingData,
    CertificatesService,
    RegisterCertificate,
)
from app.modules.documents.router import StorageDep
from app.modules.documents.service import (
    FileTooLarge,
    UploadErrorCode,
    content_sha256,
    read_limited,
)
from app.modules.identity.dependencies import (
    CurrentUser,
    SessionDep,
    SettingsDep,
    require_roles,
)
from app.modules.identity.models import User
from app.modules.extraction.preset_schemas import (
    CertificateReprocessRequest,
    CertificateStructureResponse,
    ExtractionPresetRequest,
    ExtractionPresetResponse,
    PageInfoResponse,
    PresetFromExtractionRequest,
    StructureCell,
    StructureTable,
)
from app.modules.extraction.preset_service import (
    ExtractionPresetService,
    preset_input_from_api,
)

router = APIRouter(prefix="/certificates", tags=["certificates"])
IdempotencyKey = Annotated[str, Depends(require_idempotency_key)]
IfMatch = Annotated[int, Depends(require_if_match)]
AdminUser = Annotated[User, Depends(require_roles("admin", "sysadmin"))]
CursorDep = Annotated[CursorParams, Depends(cursor_params)]


def get_certificates_service(
    session: SessionDep, settings: SettingsDep, storage: StorageDep
) -> CertificatesService:
    return CertificatesService(session, settings, storage)


ServiceDep = Annotated[CertificatesService, Depends(get_certificates_service)]


def get_preset_service(session: SessionDep) -> ExtractionPresetService:
    return ExtractionPresetService(session)


PresetServiceDep = Annotated[ExtractionPresetService, Depends(get_preset_service)]


def _response(
    service: CertificatesService,
    item: Certificate,
    parameters: list[CertificateParameterData] | None = None,
    processing: CertificateProcessingData | None = None,
) -> CertificateResponse:
    processing = processing or CertificateProcessingData(
        progress=100 if item.processing_status in {"READY", "FAILED"} else 5,
        stage=(
            "QUEUED"
            if item.processing_status == "UPLOADED"
            else item.processing_status
        ),
    )
    return CertificateResponse(
        id=item.id,
        supplier=item.supplier,
        product=item.product,
        number=item.number,
        status=service.validity_status(item),
        valid_until=item.valid_until,
        document_id=item.document_id,
        processing_status=item.processing_status,
        processing_error=item.processing_error,
        processing_error_code=processing.error_code,
        processing_error_details=processing.error_details,
        processing_progress=processing.progress,
        processing_stage=processing.stage,
        parameters=[
            CertificateParameter(
                name=parameter.name,
                value=parameter.value,
                evidence=CertificateEvidence(
                    fragment_id=parameter.fragment_id,
                    document=parameter.document,
                    page=parameter.page,
                    quote=parameter.quote,
                    verified=True,
                    locator=parameter.locator,
                ),
            )
            for parameter in parameters or []
        ],
        version=item.version,
    )


async def _responses(
    service: CertificatesService, items: list[Certificate]
) -> list[CertificateResponse]:
    parameters = await service.parameters_for(items)
    processing = await service.processing_for(items)
    return [
        _response(
            service,
            item,
            parameters.get(item.id, []),
            processing.get(item.id),
        )
        for item in items
    ]


def _preset_response(item) -> ExtractionPresetResponse:
    return ExtractionPresetResponse(
        code=item.code,
        name=item.name,
        description=item.description,
        doc_type=item.doc_type,
        columns=item.columns or {},
        labels=item.labels or [],
        section_keywords=item.section_keywords or [],
        header_keywords=item.header_keywords or [],
        units=item.units or [],
        include_all=item.include_all,
        keyvalue=item.keyvalue,
        fingerprint=item.fingerprint,
        builtin=item.builtin,
        version=item.version,
    )


def _structure_response(data) -> CertificateStructureResponse:
    return CertificateStructureResponse(
        certificate_id=str(data.certificate.id),
        document_id=str(data.file.document.id),
        file_name=data.file.version.file_name,
        pages=data.pages,
        auto_preset_code=data.auto_preset_code,
        auto_preset_score=data.auto_preset_score,
        auto_preset_method=data.auto_preset_method,
        auto_table_id=data.auto_table_id,
        tables=[
            StructureTable(
                id=table.table_id,
                page=table.page_no,
                sheet=table.sheet_name,
                bbox=list(table.bbox or ()),
                source=table.source,
                n_rows=table.n_rows,
                n_cols=table.n_cols,
                cells=[
                    StructureCell(
                        text=cell_text,
                        row=row.row_no,
                        col=col_index + 1,
                        page=_cell_value(row, col_index, "page"),
                        x0=_cell_value(row, col_index, "x0"),
                        y0=_cell_value(row, col_index, "y0"),
                        x1=_cell_value(row, col_index, "x1"),
                        y1=_cell_value(row, col_index, "y1"),
                    )
                    for row in table.rows
                    for col_index, cell_text in enumerate(row.cells)
                ],
            )
            for table in data.tables
        ],
    )


def _cell_value(row, col_index: int, key: str):
    if row.cell_boxes is None or col_index >= len(row.cell_boxes):
        return None
    return row.cell_boxes[col_index].get(key)


@router.get("/presets", response_model=list[ExtractionPresetResponse])
async def list_extraction_presets(
    service: PresetServiceDep, user: CurrentUser
) -> list[ExtractionPresetResponse]:
    return [_preset_response(item) for item in await service.list()]


@router.post(
    "/presets",
    status_code=status.HTTP_201_CREATED,
    response_model=ExtractionPresetResponse,
)
async def create_extraction_preset(
    payload: ExtractionPresetRequest,
    service: PresetServiceDep,
    user: AdminUser,
) -> ExtractionPresetResponse:
    return _preset_response(await service.create(user, preset_input_from_api(payload)))


@router.put("/presets/{preset_code}", response_model=ExtractionPresetResponse)
async def update_extraction_preset(
    preset_code: str,
    payload: ExtractionPresetRequest,
    service: PresetServiceDep,
    user: AdminUser,
    if_match: IfMatch,
) -> ExtractionPresetResponse:
    return _preset_response(
        await service.update(
            user,
            preset_code,
            preset_input_from_api(payload),
            expected_version=if_match,
        )
    )


@router.delete(
    "/presets/{preset_code}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_extraction_preset(
    preset_code: str,
    service: PresetServiceDep,
    user: AdminUser,
) -> Response:
    await service.delete(preset_code)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("", response_model=CursorPage[CertificateResponse])
async def list_certificates(
    service: ServiceDep,
    user: CurrentUser,
    pagination: CursorDep,
    supplier: Annotated[str | None, Query()] = None,
    product: Annotated[str | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    processing_status: Annotated[str | None, Query()] = None,
    search: Annotated[str | None, Query()] = None,
) -> CursorPage[CertificateResponse]:
    items, next_cursor = await service.list(
        supplier=supplier,
        product=product,
        status=status_filter,
        processing_status=processing_status,
        search=search,
        cursor=pagination.cursor,
        limit=pagination.limit,
    )
    return CursorPage[CertificateResponse](
        items=await _responses(service, items),
        next_cursor=next_cursor,
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=None)
async def register_certificate(
    request: Request,
    service: ServiceDep,
    session: SessionDep,
    settings: SettingsDep,
    user: AdminUser,
    idempotency_key: IdempotencyKey,
    file: Annotated[UploadFile, File()],
    supplier: Annotated[str, Form()],
    product: Annotated[str, Form()],
    number: Annotated[str, Form()],
    valid_until: Annotated[date | None, Form(alias="validUntil")] = None,
) -> JSONResponse:
    data = await _read_file(file, settings)
    sha256 = content_sha256(data)
    digest = request_hash(
        "POST",
        request.url.path,
        {
            "supplier": supplier.strip(),
            "product": product.strip(),
            "number": number.strip(),
            "validUntil": valid_until,
            "sha256": sha256,
        },
    )

    async def operation() -> dict:
        result = await service.register(
            user,
            RegisterCertificate(
                supplier=supplier,
                product=product,
                number=number,
                valid_until=valid_until,
                file_name=file.filename or "certificate",
                data=data,
                sha256=sha256,
            ),
        )
        return _response(service, result.certificate).model_dump(
            mode="json", by_alias=True
        )

    return await idempotent(
        IdempotencyStore(session),
        idempotency_key,
        digest,
        status.HTTP_201_CREATED,
        operation,
    )


@router.get("/{certificate_id}", response_model=CertificateResponse)
async def get_certificate(
    certificate_id: UUID,
    service: ServiceDep,
    user: CurrentUser,
    response: Response,
) -> CertificateResponse:
    certificate = await service.get(certificate_id)
    response.headers["ETag"] = f'"{certificate.version}"'
    parameters = await service.parameters_for([certificate])
    processing = await service.processing_for([certificate])
    return _response(
        service,
        certificate,
        parameters[certificate.id],
        processing[certificate.id],
    )


@router.get(
    "/{certificate_id}/structure",
    response_model=CertificateStructureResponse,
)
async def get_certificate_structure(
    certificate_id: UUID,
    service: ServiceDep,
    user: CurrentUser,
) -> CertificateStructureResponse:
    return _structure_response(await service.structure(certificate_id))


@router.get("/{certificate_id}/pages/{page}/image", response_model=None)
async def get_certificate_page_image(
    certificate_id: UUID,
    page: int,
    service: ServiceDep,
    user: CurrentUser,
) -> Response:
    image, media_type = await service.page_image(certificate_id, page)
    return Response(
        content=image,
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{certificate_id}/pages/{page}/info", response_model=PageInfoResponse)
async def get_certificate_page_info(
    certificate_id: UUID,
    page: int,
    service: ServiceDep,
    user: CurrentUser,
) -> PageInfoResponse:
    return PageInfoResponse.model_validate(await service.page_info(certificate_id, page))


@router.post(
    "/{certificate_id}/presets/from-extraction",
    status_code=status.HTTP_201_CREATED,
    response_model=ExtractionPresetResponse,
)
async def create_preset_from_extraction(
    certificate_id: UUID,
    payload: PresetFromExtractionRequest,
    service: ServiceDep,
    user: AdminUser,
) -> ExtractionPresetResponse:
    return _preset_response(
        await service.preset_from_extraction(
            user,
            certificate_id,
            name=payload.name,
            description=payload.description,
            preset_code=payload.preset_code,
        )
    )


@router.post(
    "/{certificate_id}/reprocess",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=None,
)
async def reprocess_certificate(
    certificate_id: UUID,
    request: Request,
    service: ServiceDep,
    session: SessionDep,
    user: AdminUser,
    idempotency_key: IdempotencyKey,
    if_match: IfMatch,
    payload: Annotated[CertificateReprocessRequest | None, Body()] = None,
) -> JSONResponse:
    options = payload.model_dump(mode="json", by_alias=True) if payload else {}
    digest = request_hash(
        "POST",
        request.url.path,
        {"certificateId": certificate_id, "ifMatch": if_match, **options},
    )

    async def operation() -> dict:
        result = await service.reprocess(
            certificate_id,
            expected_version=if_match,
            preset_code=payload.preset_code if payload else None,
            table_id=payload.table_id if payload else None,
            include_all=payload.include_all if payload else False,
            use_llm=payload.use_llm if payload else False,
        )
        return CertificateReprocessAccepted(
            certificate_id=result.certificate.id,
            extraction_run_id=result.run.id,
            processing_status="UPLOADED",
            version=result.certificate.version,
        ).model_dump(mode="json", by_alias=True)

    return await idempotent(
        IdempotencyStore(session),
        idempotency_key,
        digest,
        status.HTTP_202_ACCEPTED,
        operation,
    )


@router.get("/{certificate_id}/document/download", response_model=None)
async def download_certificate_document(
    certificate_id: UUID, service: ServiceDep, user: CurrentUser
) -> Response:
    result, data = await service.download(certificate_id)
    encoded_name = quote(result.version.file_name)
    return Response(
        content=data,
        media_type=result.version.media_type,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
            "Content-Length": str(len(data)),
            "X-Content-Type-Options": "nosniff",
        },
    )


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
