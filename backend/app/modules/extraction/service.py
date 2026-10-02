"""Application service for idempotent certificate extraction."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.modules.certificates.models import Certificate
from app.modules.documents.models import Document, DocumentVersion
from app.modules.documents.storage import StoragePort
from app.modules.extraction.engine import (
    DocumentAnalysis,
    DocumentAnalyzer,
    normalize_parameters,
    persist_analysis,
)
from app.modules.extraction.models import ExtractionRun
from app.modules.extraction.preset_service import (
    ExtractionPresetService,
    preset_config_from_snapshot,
)
from app.modules.extraction.parsers import PARSER_VERSION, NativeParseError
from app.modules.extraction.providers import (
    LlmExtractionProvider,
    OcrProvider,
    PdfPageRenderer,
    ProviderError,
    ProviderTransientError,
)
from app.modules.extraction.queue import PIPELINE_VERSION, extraction_key

LEASE_TIME = timedelta(minutes=15)
logger = structlog.get_logger(__name__)


class ExtractionRejected(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ExtractionOutcome:
    status: str
    run_id: uuid.UUID | None
    parameters: int = 0


@dataclass(frozen=True)
class _Work:
    certificate_id: uuid.UUID
    document_version_id: uuid.UUID
    run_id: uuid.UUID
    media_type: str
    storage_key: str
    options: dict


class CertificateExtractionService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        storage: StoragePort,
        *,
        renderer: PdfPageRenderer | None = None,
        ocr_provider: OcrProvider | None = None,
        llm_provider: LlmExtractionProvider | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._storage = storage
        self._analyzer = DocumentAnalyzer(
            settings,
            renderer=renderer,
            ocr_provider=ocr_provider,
            llm_provider=llm_provider,
        )

    async def process(
        self,
        certificate_id: uuid.UUID,
        *,
        worker_token: str,
        requested_run_id: uuid.UUID | None = None,
    ) -> ExtractionOutcome:
        work = await self._prepare(
            certificate_id, worker_token, requested_run_id=requested_run_id
        )
        if isinstance(work, ExtractionOutcome):
            return work

        started = time.monotonic()

        async def report(stage: str, percent: int) -> None:
            await self._progress(work, worker_token, stage=stage, percent=percent)

        try:
            data = await self._storage.get(
                self._settings.upload_bucket, work.storage_key
            )
            analysis = await self._analyzer.analyze(
                data,
                work.media_type,
                correlation_id=str(work.run_id),
                on_progress=report,
                presets=await self._preset_configs(work.options),
                preset_code=work.options.get("presetCode"),
                table_id=work.options.get("tableId"),
                include_all=bool(work.options.get("includeAll", False)),
                use_llm=work.options.get("useLlm"),
            )
            return await self._persist(
                work, worker_token, analysis=analysis, started=started
            )
        except NativeParseError as exc:
            code = str(exc) or "DOCUMENT_PARSE_FAILED"
            return await self._fail(work, worker_token, code, _failure_message(code))
        except ProviderTransientError:
            await self._fail(
                work,
                worker_token,
                "PROVIDER_TRANSIENT_ERROR",
                "Сервис распознавания временно недоступен",
            )
            raise
        except ProviderError as exc:
            code = str(exc) or "PROVIDER_FAILED"
            return await self._fail(work, worker_token, code, _failure_message(code))
        except Exception as exc:
            await self._fail(
                work,
                worker_token,
                "EXTRACTION_FAILED",
                "Не удалось обработать файл сертификата",
                details=_exception_details(exc),
            )
            raise

    async def _progress(
        self,
        work: _Work,
        worker_token: str,
        *,
        stage: str,
        percent: int,
    ) -> None:
        run = await self._session.scalar(
            select(ExtractionRun)
            .where(ExtractionRun.id == work.run_id)
            .with_for_update()
        )
        if run is None or run.worker_token != worker_token or run.status != "RUNNING":
            await self._session.rollback()
            return
        run.usage = {
            **(run.usage or {}),
            "stage": stage,
            "progress": percent,
        }
        await self._session.commit()

    async def _prepare(
        self,
        certificate_id: uuid.UUID,
        worker_token: str,
        *,
        requested_run_id: uuid.UUID | None,
    ) -> _Work | ExtractionOutcome:
        certificate = await self._session.scalar(
            select(Certificate)
            .where(Certificate.id == certificate_id)
            .with_for_update()
        )
        if certificate is None:
            await self._session.rollback()
            return ExtractionOutcome("MISSING", None)

        document = await self._session.get(Document, certificate.document_id)
        if document is None or document.active_version_id is None:
            await self._session.rollback()
            return ExtractionOutcome("MISSING_DOCUMENT", None)
        version = await self._session.get(DocumentVersion, document.active_version_id)
        if version is None:
            await self._session.rollback()
            return ExtractionOutcome("MISSING_DOCUMENT", None)

        key = extraction_key(certificate.id, version.id)
        if requested_run_id is None:
            run = await self._session.scalar(
                select(ExtractionRun).where(ExtractionRun.idempotency_key == key)
            )
        else:
            run = await self._session.scalar(
                select(ExtractionRun).where(
                    ExtractionRun.id == requested_run_id,
                    ExtractionRun.document_version_id == version.id,
                )
            )
            if run is None:
                await self._session.rollback()
                return ExtractionOutcome("SUPERSEDED", requested_run_id)

        if run is not None:
            latest_run_id = await self._session.scalar(
                select(ExtractionRun.id)
                .where(ExtractionRun.document_version_id == version.id)
                .order_by(ExtractionRun.created_at.desc(), ExtractionRun.id.desc())
                .limit(1)
            )
            if latest_run_id != run.id:
                await self._session.rollback()
                return ExtractionOutcome("SUPERSEDED", run.id)

        if run is not None and run.status == "COMPLETED":
            run_id = run.id
            count = int((run.usage or {}).get("verifiedParameters", 0))
            if certificate.processing_status != "READY":
                certificate.processing_status = "READY"
                certificate.processing_error = None
                certificate.version += 1
                await self._session.commit()
            else:
                await self._session.rollback()
            return ExtractionOutcome("READY", run_id, count)

        now = datetime.now(UTC)
        if (
            run is not None
            and run.status == "RUNNING"
            and run.worker_token != worker_token
            and run.started_at is not None
            and now - run.started_at < LEASE_TIME
        ):
            await self._session.rollback()
            return ExtractionOutcome("IN_PROGRESS", run.id)

        if run is None:
            run = ExtractionRun(
                id=uuid.uuid4(),
                check_item_id=None,
                document_version_id=version.id,
                method="REGEX",
                provider="native",
                model=PARSER_VERSION,
                status="RUNNING",
                pipeline_version=PIPELINE_VERSION,
                idempotency_key=key,
                worker_token=worker_token,
                started_at=now,
                usage={"stage": "PARSING", "progress": 10},
            )
            self._session.add(run)
        else:
            options = (run.usage or {}).get("extractionOptions") or {}
            run.status = "RUNNING"
            run.worker_token = worker_token
            run.started_at = now
            run.finished_at = None
            run.error_code = None
            run.error_message = None
            run.usage = {
                "stage": "PARSING",
                "progress": 10,
                **({"extractionOptions": options} if options else {}),
            }

        certificate.processing_status = "PROCESSING"
        certificate.processing_error = None
        certificate.version += 1
        await self._session.commit()
        return _Work(
            certificate_id=certificate.id,
            document_version_id=version.id,
            run_id=run.id,
            media_type=version.media_type,
            storage_key=version.storage_key,
            options=(run.usage or {}).get("extractionOptions") or {},
        )

    async def _persist(
        self,
        work: _Work,
        worker_token: str,
        *,
        analysis: DocumentAnalysis,
        started: float,
    ) -> ExtractionOutcome:
        run = await self._session.scalar(
            select(ExtractionRun)
            .where(ExtractionRun.id == work.run_id)
            .with_for_update()
        )
        certificate = await self._session.get(Certificate, work.certificate_id)
        version = await self._session.get(DocumentVersion, work.document_version_id)
        if run is None or certificate is None or version is None:
            await self._session.rollback()
            return ExtractionOutcome("SUPERSEDED", work.run_id)
        if run.worker_token != worker_token or run.status == "COMPLETED":
            await self._session.rollback()
            return ExtractionOutcome("SUPERSEDED", run.id)

        persisted = await persist_analysis(
            self._session,
            document_version_id=work.document_version_id,
            run=run,
            analysis=analysis,
            min_confidence=self._settings.extraction_min_confidence,
        )
        # У сертификата нет прогона pipeline и зафиксированной версии каталога:
        # нормализация идёт по версии по умолчанию сразу за записью.
        normalization = await normalize_parameters(self._session, persisted.parameters)

        ocr_stats = analysis.ocr_stats
        llm_stats = analysis.llm_stats
        if llm_stats.candidates:
            run.method = "LLM"
            run.provider = llm_stats.provider
            run.model = llm_stats.model
            run.prompt_version = llm_stats.prompt_version
        elif ocr_stats.recognized_pages:
            run.provider = ocr_stats.provider
            run.model = ocr_stats.model

        verified_count = persisted.verified
        now = datetime.now(UTC)
        run.usage = {
            **(run.usage or {}),
            "stage": "COMPLETED" if verified_count else "FAILED",
            "progress": 100,
            "pages": persisted.pages,
            "fragments": persisted.fragments,
            "candidates": persisted.candidates,
            "verifiedParameters": verified_count,
            "ocr": {
                "attemptedPages": ocr_stats.attempted_pages,
                "recognizedPages": ocr_stats.recognized_pages,
                "provider": ocr_stats.provider,
                "model": ocr_stats.model,
            },
            "llm": {
                "suppliedFragments": llm_stats.supplied_fragments,
                "candidates": llm_stats.candidates,
                "provider": llm_stats.provider,
                "model": llm_stats.model,
                "promptVersion": llm_stats.prompt_version,
                "error": analysis.llm_error,
            },
            "normalization": {
                "catalogVersionId": (
                    str(normalization.catalog_version_id)
                    if normalization.catalog_version_id
                    else None
                ),
                "normalized": normalization.normalized,
                "rejected": normalization.rejected,
            },
            "preset": {
                "code": analysis.preset_code,
                "name": analysis.preset_name,
                "matchScore": analysis.preset_match_score,
                "matchMethod": analysis.preset_match_method,
                "tableId": analysis.selected_table_id,
                "warnings": list(analysis.preset_warnings),
            },
            "durationMs": round((time.monotonic() - started) * 1000),
        }
        run.finished_at = now
        run.worker_token = None
        certificate.version += 1
        if verified_count:
            run.status = "COMPLETED"
            run.error_code = None
            run.error_message = None
            certificate.processing_status = "READY"
            certificate.processing_error = None
            status = "READY"
        else:
            run.status = "FAILED"
            run.error_code = "NO_VERIFIED_PARAMETERS"
            run.error_message = "В документе не найдены подтверждённые характеристики"
            certificate.processing_status = "FAILED"
            certificate.processing_error = run.error_message
            status = "FAILED"
        await self._session.commit()
        logger.info(
            "certificate_extraction_completed",
            certificate_id=str(work.certificate_id),
            extraction_run_id=str(run.id),
            status=status,
            parameters=verified_count,
            duration_ms=run.usage["durationMs"],
        )
        return ExtractionOutcome(status, run.id, verified_count)

    async def _fail(
        self,
        work: _Work,
        worker_token: str,
        code: str,
        message: str,
        details: dict | None = None,
    ) -> ExtractionOutcome:
        await self._session.rollback()
        run = await self._session.scalar(
            select(ExtractionRun)
            .where(ExtractionRun.id == work.run_id)
            .with_for_update()
        )
        certificate = await self._session.get(Certificate, work.certificate_id)
        if run is None or run.worker_token != worker_token:
            await self._session.rollback()
            return ExtractionOutcome("SUPERSEDED", work.run_id)
        run.status = "FAILED"
        run.error_code = code
        run.error_message = message
        run.finished_at = datetime.now(UTC)
        run.worker_token = None
        run.usage = {
            **(run.usage or {}),
            "stage": "FAILED",
            "progress": 100,
            "errorCode": code,
            **({"errorDetails": details} if details else {}),
        }
        if certificate is not None:
            certificate.processing_status = "FAILED"
            certificate.processing_error = message
            certificate.version += 1
        await self._session.commit()
        logger.warning(
            "certificate_extraction_failed",
            certificate_id=str(work.certificate_id),
            extraction_run_id=str(run.id),
            error_code=code,
        )
        return ExtractionOutcome("FAILED", run.id)

    async def _preset_configs(self, options: dict) -> list:
        snapshot = options.get("presetSnapshot")
        if snapshot:
            return [preset_config_from_snapshot(snapshot)]
        return await ExtractionPresetService(self._session).configs()


def _failure_message(code: str) -> str:
    messages = {
        "DOCUMENT_PAGE_LIMIT_EXCEEDED": "В документе слишком много страниц",
        "SPREADSHEET_ROW_LIMIT_EXCEEDED": "В таблице слишком много строк",
        "DOCUMENT_FRAGMENT_LIMIT_EXCEEDED": "Фрагмент документа слишком большой",
        "UNSUPPORTED_DOCUMENT_FORMAT": "Формат документа не поддерживается",
    }
    return messages.get(code, "Не удалось прочитать содержимое сертификата")


def _exception_details(exc: Exception) -> dict[str, str]:
    message = str(exc).splitlines()[0] if str(exc) else repr(exc)
    return {
        "exceptionType": type(exc).__name__,
        "exceptionMessage": message[:800],
    }
