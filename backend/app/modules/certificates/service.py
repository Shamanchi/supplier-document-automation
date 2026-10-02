"""Certificate registry, immutable file upload and download."""

from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from io import BytesIO
from typing import Any

from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from pypdf import PdfReader

from app.config import Settings
from app.core.errors import ApiError
from app.core.pagination import decode_cursor, encode_cursor
from app.modules.certificates.models import Certificate, PROCESSING_STATUSES
from app.modules.documents.models import Document, DocumentVersion
from app.modules.documents.service import UploadErrorCode
from app.modules.documents.signatures import (
    DetectedFormat,
    FileRejected,
    ZipLimits,
    detect,
    safe_file_name,
)
from app.modules.documents.storage import StoragePort
from app.modules.extraction.models import (
    DocumentPage,
    ExtractionEvidence,
    ExtractionRun,
    RawParameter,
    SourceFragment,
)
from app.modules.extraction.parsers import PARSER_VERSION
from app.modules.extraction.parsers import NativeParseError, parse_document
from app.modules.extraction.preset_mapping import (
    document_signature,
    match_presets,
    preset_from_model,
    tables_from_document,
)
from app.modules.extraction.preset_service import ExtractionPresetService
from app.modules.extraction.providers import PopplerPdfRenderer
from app.modules.extraction.queue import (
    PIPELINE_VERSION,
    enqueue_certificate_extraction,
    extraction_key,
)
from app.modules.identity.models import User

_REJECTION_STATUS: dict[str, int] = {
    UploadErrorCode.UNSUPPORTED_FILE_TYPE: 415,
    UploadErrorCode.FILE_SIGNATURE_MISMATCH: 422,
    UploadErrorCode.MALICIOUS_FILE_DETECTED: 422,
}


@dataclass(frozen=True)
class RegisterCertificate:
    supplier: str
    product: str
    number: str
    valid_until: date | None
    file_name: str
    data: bytes
    sha256: str


@dataclass(frozen=True)
class CertificateFile:
    certificate: Certificate
    document: Document
    version: DocumentVersion


@dataclass(frozen=True)
class CertificateParameterData:
    name: str
    value: str
    fragment_id: uuid.UUID
    document: str
    page: int | None
    quote: str
    locator: dict[str, Any] | None


@dataclass(frozen=True)
class ReprocessResult:
    certificate: Certificate
    run: ExtractionRun


@dataclass(frozen=True)
class CertificateProcessingData:
    progress: int
    stage: str | None
    error_code: str | None = None
    error_details: dict[str, Any] | None = None


@dataclass(frozen=True)
class CertificateStructureData:
    certificate: Certificate
    file: CertificateFile
    pages: int
    tables: list
    auto_preset_code: str | None
    auto_preset_score: float | None
    auto_preset_method: str | None
    auto_table_id: str | None


@dataclass(frozen=True)
class PageInfo:
    page: int
    width: int
    height: int
    pdf_width: float
    pdf_height: float


class CertificatesService:
    def __init__(
        self, session: AsyncSession, settings: Settings, storage: StoragePort
    ) -> None:
        self._session = session
        self._settings = settings
        self._storage = storage

    async def list(
        self,
        *,
        supplier: str | None,
        product: str | None,
        status: str | None,
        processing_status: str | None,
        search: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[Certificate], str | None]:
        query = select(Certificate).order_by(
            Certificate.created_at.desc(), Certificate.id.desc()
        )
        if supplier:
            query = query.where(Certificate.supplier.ilike(f"%{supplier.strip()}%"))
        if product:
            query = query.where(Certificate.product.ilike(f"%{product.strip()}%"))
        if search:
            pattern = f"%{search.strip()}%"
            query = query.where(
                Certificate.number.ilike(pattern)
                | Certificate.supplier.ilike(pattern)
                | Certificate.product.ilike(pattern)
            )
        if status:
            query = self._filter_validity(query, status)
        if processing_status:
            processing = processing_status.strip().upper()
            if processing not in PROCESSING_STATUSES:
                raise ApiError(
                    "VALIDATION_ERROR",
                    f"Неизвестный статус обработки: {processing_status}",
                    status_code=422,
                    details={"allowed": list(PROCESSING_STATUSES)},
                )
            query = query.where(Certificate.processing_status == processing)
        if cursor:
            created_at, last_id = self._decode_cursor(cursor)
            query = query.where(
                tuple_(Certificate.created_at, Certificate.id) < (created_at, last_id)
            )

        rows = list((await self._session.scalars(query.limit(limit + 1))).all())
        page = rows[:limit]
        next_cursor = (
            encode_cursor(f"{page[-1].created_at.isoformat()}|{page[-1].id}")
            if len(rows) > limit and page
            else None
        )
        return page, next_cursor

    async def get(self, certificate_id: uuid.UUID) -> Certificate:
        certificate = await self._session.get(Certificate, certificate_id)
        if certificate is None:
            raise ApiError(
                "CERTIFICATE_NOT_FOUND",
                "Сертификат не найден",
                status_code=404,
                details={"certificateId": str(certificate_id)},
            )
        return certificate

    async def parameters_for(
        self, certificates: list[Certificate]
    ) -> dict[uuid.UUID, list[CertificateParameterData]]:
        result = {certificate.id: [] for certificate in certificates}
        if not result:
            return result

        ranked_runs = (
            select(
                ExtractionRun.id.label("run_id"),
                ExtractionRun.document_version_id.label("document_version_id"),
                func.row_number()
                .over(
                    partition_by=ExtractionRun.document_version_id,
                    order_by=(
                        ExtractionRun.created_at.desc(),
                        ExtractionRun.id.desc(),
                    ),
                )
                .label("run_position"),
            )
            .where(ExtractionRun.status == "COMPLETED")
            .subquery()
        )
        rows = (
            await self._session.execute(
                select(
                    Certificate.id.label("certificate_id"),
                    RawParameter.name_text,
                    RawParameter.value_text,
                    RawParameter.unit_text,
                    SourceFragment.id.label("fragment_id"),
                    DocumentVersion.file_name,
                    DocumentPage.page_no,
                    ExtractionEvidence.quote,
                    SourceFragment.locator,
                )
                .select_from(Certificate)
                .join(Document, Document.id == Certificate.document_id)
                .join(
                    DocumentVersion,
                    DocumentVersion.id == Document.active_version_id,
                )
                .join(
                    ranked_runs,
                    and_(
                        ranked_runs.c.document_version_id == DocumentVersion.id,
                        ranked_runs.c.run_position == 1,
                    ),
                )
                .join(
                    RawParameter,
                    RawParameter.extraction_run_id == ranked_runs.c.run_id,
                )
                .join(
                    ExtractionEvidence,
                    ExtractionEvidence.raw_parameter_id == RawParameter.id,
                )
                .join(
                    SourceFragment,
                    SourceFragment.id == ExtractionEvidence.source_fragment_id,
                )
                .outerjoin(DocumentPage, DocumentPage.id == SourceFragment.page_id)
                .where(
                    Certificate.id.in_(result),
                    RawParameter.excluded.is_(False),
                    ExtractionEvidence.verified.is_(True),
                )
                .order_by(
                    Certificate.id,
                    RawParameter.created_at,
                    RawParameter.id,
                )
            )
        ).all()

        for row in rows:
            value = row.value_text or ""
            if row.unit_text:
                value = f"{value} {row.unit_text}".strip()
            result[row.certificate_id].append(
                CertificateParameterData(
                    name=row.name_text,
                    value=value,
                    fragment_id=row.fragment_id,
                    document=row.file_name,
                    page=row.page_no,
                    quote=row.quote,
                    locator=row.locator,
                )
            )
        return result

    async def processing_for(
        self, certificates: list[Certificate]
    ) -> dict[uuid.UUID, CertificateProcessingData]:
        defaults = {
            certificate.id: CertificateProcessingData(
                progress={
                    "UPLOADED": 5,
                    "PROCESSING": 10,
                    "READY": 100,
                    "FAILED": 100,
                }[certificate.processing_status],
                stage=certificate.processing_status,
            )
            for certificate in certificates
        }
        if not defaults:
            return defaults
        ranked_runs = (
            select(
                ExtractionRun.document_version_id.label("document_version_id"),
                ExtractionRun.usage.label("usage"),
                ExtractionRun.error_code.label("error_code"),
                ExtractionRun.error_message.label("error_message"),
                func.row_number()
                .over(
                    partition_by=ExtractionRun.document_version_id,
                    order_by=(
                        ExtractionRun.created_at.desc(),
                        ExtractionRun.id.desc(),
                    ),
                )
                .label("run_position"),
            )
            .subquery()
        )
        rows = (
            await self._session.execute(
                select(
                    Certificate.id.label("certificate_id"),
                    ranked_runs.c.usage,
                    ranked_runs.c.error_code,
                    ranked_runs.c.error_message,
                )
                .select_from(Certificate)
                .join(Document, Document.id == Certificate.document_id)
                .join(
                    ranked_runs,
                    and_(
                        ranked_runs.c.document_version_id
                        == Document.active_version_id,
                        ranked_runs.c.run_position == 1,
                    ),
                )
                .where(Certificate.id.in_(defaults))
            )
        ).all()
        for row in rows:
            usage = row.usage or {}
            defaults[row.certificate_id] = CertificateProcessingData(
                progress=max(0, min(100, int(usage.get("progress", 5)))),
                stage=usage.get("stage"),
                error_code=row.error_code or usage.get("errorCode"),
                error_details=_error_details(usage, row.error_message),
            )
        return defaults

    async def register(
        self, user: User, request: RegisterCertificate
    ) -> CertificateFile:
        supplier = self._required(request.supplier, "supplier")
        product = self._required(request.product, "product")
        number = self._required(request.number, "number")
        await self._assert_number_available(supplier, number)

        file_name = safe_file_name(request.file_name)
        quarantine_key = f"certificates/{uuid.uuid4()}"
        await self._storage.put(
            self._settings.quarantine_bucket,
            quarantine_key,
            request.data,
            content_type="application/octet-stream",
        )
        detected = self._detect_or_reject(request.data, file_name=file_name)
        await self._assert_file_is_new(request.sha256, quarantine_key)

        certificate_id = uuid.uuid4()
        document = Document(
            id=uuid.uuid4(),
            check_item_id=None,
            role="FACT",
            source="UPLOAD",
            supplier_id=supplier,
        )
        self._session.add(document)
        await self._session.flush()

        version = self._new_document_version(
            certificate_id, document, request, file_name, detected
        )
        self._session.add(version)
        await self._session.flush()
        document.active_version_id = version.id

        certificate = Certificate(
            id=certificate_id,
            supplier=supplier,
            product=product,
            number=number,
            valid_until=request.valid_until,
            document_id=document.id,
            registered_by=user.id,
            processing_status="UPLOADED",
            version=1,
        )
        self._session.add(certificate)
        await self._session.flush()

        await self._storage.move(
            src_bucket=self._settings.quarantine_bucket,
            src_key=quarantine_key,
            dst_bucket=self._settings.upload_bucket,
            dst_key=version.storage_key,
        )
        await enqueue_certificate_extraction(
            self._session,
            certificate_id=certificate.id,
            document_version_id=version.id,
        )
        return CertificateFile(certificate, document, version)

    async def reprocess(
        self,
        certificate_id: uuid.UUID,
        *,
        expected_version: int,
        preset_code: str | None = None,
        table_id: str | None = None,
        include_all: bool = False,
        use_llm: bool = False,
    ) -> ReprocessResult:
        certificate = await self._session.scalar(
            select(Certificate)
            .where(Certificate.id == certificate_id)
            .with_for_update()
        )
        if certificate is None:
            raise ApiError(
                "CERTIFICATE_NOT_FOUND",
                "Сертификат не найден",
                status_code=404,
                details={"certificateId": str(certificate_id)},
            )
        self._assert_version(certificate.version, expected_version)
        if certificate.processing_status in {"UPLOADED", "PROCESSING"}:
            raise ApiError(
                "CERTIFICATE_PROCESSING",
                "Сертификат уже поставлен в обработку",
                status_code=409,
                details={"certificateId": str(certificate.id)},
            )
        if use_llm and not self._llm_provider_configured():
            raise ApiError(
                "LLM_PROVIDER_NOT_CONFIGURED",
                "LLM-извлечение не настроено",
                status_code=422,
            )

        document = await self._session.get(Document, certificate.document_id)
        if document is None or document.active_version_id is None:
            raise ApiError(
                "CERTIFICATE_DOCUMENT_NOT_FOUND",
                "Файл сертификата не найден",
                status_code=404,
                details={"certificateId": str(certificate_id)},
            )
        version = await self._session.get(DocumentVersion, document.active_version_id)
        if version is None:
            raise ApiError(
                "CERTIFICATE_DOCUMENT_NOT_FOUND",
                "Файл сертификата не найден",
                status_code=404,
                details={"certificateId": str(certificate_id)},
            )

        run_id = uuid.uuid4()
        key = f"{extraction_key(certificate.id, version.id)}:run:{run_id}"
        preset_snapshot = await ExtractionPresetService(self._session).snapshot(
            preset_code
        )
        options = {
            "presetCode": preset_code,
            "tableId": table_id,
            "includeAll": include_all,
            "useLlm": use_llm,
            **({"presetSnapshot": preset_snapshot} if preset_snapshot else {}),
        }
        run = ExtractionRun(
            id=run_id,
            check_item_id=None,
            document_version_id=version.id,
            method="REGEX",
            provider="native",
            model=PARSER_VERSION,
            status="QUEUED",
            pipeline_version=PIPELINE_VERSION,
            idempotency_key=key,
            usage={
                "stage": "QUEUED",
                "progress": 5,
                "extractionOptions": options,
            },
        )
        self._session.add(run)
        certificate.processing_status = "UPLOADED"
        certificate.processing_error = None
        certificate.version += 1
        await enqueue_certificate_extraction(
            self._session,
            certificate_id=certificate.id,
            document_version_id=version.id,
            run_id=run.id,
            event_key=key,
        )
        await self._session.flush()
        return ReprocessResult(certificate=certificate, run=run)

    async def structure(self, certificate_id: uuid.UUID) -> CertificateStructureData:
        file = await self.file(certificate_id)
        data = await self._storage.get(
            self._settings.upload_bucket, file.version.storage_key
        )
        try:
            parsed = parse_document(data, file.version.media_type)
        except NativeParseError as exc:
            code = str(exc) or "DOCUMENT_PARSE_FAILED"
            raise ApiError(
                code,
                _parse_failure_message(code),
                status_code=422,
                details={"certificateId": str(certificate_id)},
            ) from exc
        tables = tables_from_document(parsed)
        presets = [
            preset_from_model(item)
            for item in await ExtractionPresetService(self._session).list()
        ]
        matched = match_presets(presets, document_signature(parsed))
        return CertificateStructureData(
            certificate=file.certificate,
            file=file,
            pages=len(parsed.pages),
            tables=tables,
            auto_preset_code=matched.preset.code if matched.preset else None,
            auto_preset_score=round(matched.score, 3) if matched.preset else None,
            auto_preset_method=matched.method if matched.preset else None,
            auto_table_id=matched.table_id,
        )

    async def page_image(self, certificate_id: uuid.UUID, page: int) -> tuple[bytes, str]:
        file = await self.file(certificate_id)
        self._assert_pdf(file.version.media_type)
        data = await self._storage.get(
            self._settings.upload_bucket, file.version.storage_key
        )
        image = await PopplerPdfRenderer().render(data, page, dpi=self._settings.ocr_dpi)
        return image, "image/png"

    async def page_info(self, certificate_id: uuid.UUID, page: int) -> PageInfo:
        file = await self.file(certificate_id)
        self._assert_pdf(file.version.media_type)
        data = await self._storage.get(
            self._settings.upload_bucket, file.version.storage_key
        )
        try:
            reader = PdfReader(BytesIO(data), strict=False)
            source_page = reader.pages[page - 1]
        except IndexError as exc:
            raise ApiError(
                "PAGE_NOT_FOUND",
                "Страница не найдена",
                status_code=404,
                details={"page": page},
            ) from exc
        pdf_width = float(source_page.mediabox.width)
        pdf_height = float(source_page.mediabox.height)
        image = await PopplerPdfRenderer().render(data, page, dpi=self._settings.ocr_dpi)
        width, height = _png_size(image)
        return PageInfo(
            page=page,
            width=width,
            height=height,
            pdf_width=pdf_width,
            pdf_height=pdf_height,
        )

    async def preset_from_extraction(
        self,
        user: User,
        certificate_id: uuid.UUID,
        *,
        name: str,
        description: str,
        preset_code: str | None,
    ):
        file = await self.file(certificate_id)
        data = await self._storage.get(
            self._settings.upload_bucket, file.version.storage_key
        )
        try:
            parsed = parse_document(data, file.version.media_type)
        except NativeParseError as exc:
            code = str(exc) or "DOCUMENT_PARSE_FAILED"
            raise ApiError(code, _parse_failure_message(code), status_code=422) from exc
        extracted = await self.parameters_for([file.certificate])
        return await ExtractionPresetService(self._session).create_from_analysis(
            user,
            name=name,
            description=description,
            source_preset_code=preset_code,
            parsed_document=parsed,
            labels_from_result=[
                parameter.name for parameter in extracted[file.certificate.id]
            ],
        )

    async def file(self, certificate_id: uuid.UUID) -> CertificateFile:
        certificate = await self.get(certificate_id)
        document = await self._session.get(Document, certificate.document_id)
        if document is None or document.active_version_id is None:
            raise ApiError(
                "CERTIFICATE_DOCUMENT_NOT_FOUND",
                "Файл сертификата не найден",
                status_code=404,
                details={"certificateId": str(certificate_id)},
            )
        version = await self._session.get(DocumentVersion, document.active_version_id)
        if version is None:
            raise ApiError(
                "CERTIFICATE_DOCUMENT_NOT_FOUND",
                "Файл сертификата не найден",
                status_code=404,
                details={"certificateId": str(certificate_id)},
            )
        return CertificateFile(certificate, document, version)

    async def download(
        self, certificate_id: uuid.UUID
    ) -> tuple[CertificateFile, bytes]:
        result = await self.file(certificate_id)
        data = await self._storage.get(
            self._settings.upload_bucket, result.version.storage_key
        )
        return result, data

    @staticmethod
    def validity_status(certificate: Certificate, *, today: date | None = None) -> str:
        boundary = today or datetime.now(UTC).date()
        if certificate.valid_until is not None and certificate.valid_until < boundary:
            return "EXPIRED"
        return "ACTIVE"

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

    async def _assert_number_available(self, supplier: str, number: str) -> None:
        existing = await self._session.scalar(
            select(Certificate.id).where(
                Certificate.supplier == supplier, Certificate.number == number
            )
        )
        if existing is not None:
            raise ApiError(
                "CERTIFICATE_ALREADY_EXISTS",
                "Сертификат с таким номером у поставщика уже зарегистрирован",
                status_code=409,
                details={"certificateId": str(existing)},
            )

    async def _assert_file_is_new(self, sha256: str, quarantine_key: str) -> None:
        duplicate = await self._session.scalar(
            select(Certificate.id)
            .join(Document, Document.id == Certificate.document_id)
            .join(DocumentVersion, DocumentVersion.document_id == Document.id)
            .where(DocumentVersion.sha256 == sha256)
            .limit(1)
        )
        if duplicate is None:
            return
        await self._storage.delete(self._settings.quarantine_bucket, quarantine_key)
        raise ApiError(
            "CERTIFICATE_FILE_DUPLICATE",
            "Этот файл сертификата уже зарегистрирован",
            status_code=409,
            details={"certificateId": str(duplicate)},
        )

    @staticmethod
    def _new_document_version(
        certificate_id: uuid.UUID,
        document: Document,
        request: RegisterCertificate,
        file_name: str,
        detected: DetectedFormat,
    ) -> DocumentVersion:
        version_id = uuid.uuid4()
        return DocumentVersion(
            id=version_id,
            document_id=document.id,
            version_no=1,
            file_name=file_name,
            media_type=detected.media_type,
            size_bytes=len(request.data),
            sha256=request.sha256,
            storage_key=(
                f"certificates/{certificate_id}/{document.id}/"
                f"{version_id}.{detected.extension}"
            ),
            status="UPLOADED",
        )

    @staticmethod
    def _filter_validity(query, status: str):
        value = status.strip().upper()
        today = datetime.now(UTC).date()
        if value == "ACTIVE":
            return query.where(
                or_(Certificate.valid_until.is_(None), Certificate.valid_until >= today)
            )
        if value == "EXPIRED":
            return query.where(Certificate.valid_until < today)
        raise ApiError(
            "VALIDATION_ERROR",
            f"Неизвестный статус сертификата: {status}",
            status_code=422,
            details={"allowed": ["ACTIVE", "EXPIRED"]},
        )

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
        try:
            raw_created, raw_id = decode_cursor(cursor).split("|", 1)
            return datetime.fromisoformat(raw_created), uuid.UUID(raw_id)
        except ValueError as exc:
            raise ApiError(
                "VALIDATION_ERROR", "Некорректный курсор", status_code=422
            ) from exc

    @staticmethod
    def _required(value: str, field: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ApiError(
                "VALIDATION_ERROR",
                f"Поле {field} не может быть пустым",
                status_code=422,
                details={"field": field},
            )
        return normalized

    @staticmethod
    def _assert_version(actual: int, expected: int) -> None:
        if actual != expected:
            raise ApiError(
                "EDIT_CONFLICT",
                "Сертификат изменён другим пользователем",
                status_code=409,
                details={
                    "currentVersion": actual,
                    "expectedVersion": expected,
                },
            )

    @staticmethod
    def _assert_pdf(media_type: str) -> None:
        if media_type != "application/pdf":
            raise ApiError(
                "NOT_PDF",
                "Предпросмотр страниц доступен только для PDF",
                status_code=400,
            )

    def _llm_provider_configured(self) -> bool:
        return bool(
            self._settings.llm_extraction_enabled
            and self._settings.llm_extraction_endpoint
            and self._settings.llm_extraction_api_key
        )


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ApiError(
            "PDF_PAGE_RENDER_FAILED",
            "Не удалось построить изображение страницы",
            status_code=502,
        )
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _error_details(usage: dict, error_message: str | None) -> dict[str, Any] | None:
    details = usage.get("errorDetails")
    if isinstance(details, dict):
        return details
    if error_message:
        return {"message": error_message}
    return None


def _parse_failure_message(code: str) -> str:
    return {
        "UNSUPPORTED_MEDIA_TYPE": "Тип файла не поддерживается для извлечения",
        "CSV_DECODE_FAILED": "Не удалось прочитать CSV как UTF-8",
        "XLSX_PARSE_FAILED": "Не удалось прочитать таблицу Excel",
        "DOCX_PARSE_FAILED": "Не удалось прочитать документ Word",
        "PDF_PARSE_FAILED": "Не удалось прочитать PDF",
    }.get(code, "Не удалось разобрать документ")
