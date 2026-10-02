"""Исполнение прогона позиции: этапы FSM, прогресс, отмена и завершение.

Прогон исполняет не весь FSM разом, а один из трёх планов — потому что между
ними по канону стоит человек:

* `INGESTING → EXTRACTING → VERIFYING_SOURCES → NORMALIZING` — до разбора
  параметров менеджером (§16: `MATCHING` не стартует без `parameters/confirm`);
* `MATCHING` — сопоставление и вердикт, запускается подтверждением параметров;
* `REPORTING` — сборка отчёта, запускается только после `VERDICT_APPROVED`
  (§17).

Отдельный прогон на каждый план, а не пауза внутри одного: у `pipeline_runs`
нет статуса «ждём человека», а `RUNNING` без воркера сломал бы и лизинг, и
повторную доставку. Каждый прогон остаётся тем, чем был, — единицей работы с
началом, концом и своим следом в истории.

Свойства, ради которых написан именно такой исполнитель:

* **идемпотентность** — повторная доставка той же задачи не создаёт второго
  прогона и не переписывает результат завершённого;
* **лизинг** — параллельный воркер не подхватит прогон, пока лизинг жив, а
  «повисший» воркер не сможет записать результат после его истечения;
* **устаревшая доставка** — если у позиции уже есть более новый прогон, старая
  задача завершается без единой записи;
* **отмена** — проверяется между этапами и внутри длинного этапа, как требует
  контракт (check-items §Отмена и retry).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.modules.checks.models import CheckItem, PipelineRun, StageRun
from app.modules.documents.models import Document, DocumentVersion
from app.modules.documents.storage import StoragePort
from app.modules.extraction.engine import (
    DocumentAnalysis,
    DocumentAnalyzer,
    normalize_parameters,
    persist_analysis,
)
from app.modules.extraction.models import ExtractionRun
from app.modules.extraction.parsers import PARSER_VERSION, NativeParseError
from app.modules.extraction.providers import (
    LlmExtractionProvider,
    OcrProvider,
    PdfPageRenderer,
    ProviderError,
    ProviderTransientError,
)
from app.modules.pipeline.events import (
    RUN_CANCELLED,
    RUN_COMPLETED,
    RUN_FAILED,
    STAGE_CHANGED,
    Progress,
    record_event,
)
from app.modules.pipeline.queue import EXECUTOR_VERSION
from app.modules.reporting.service import ReportingError, ReportingService
from app.modules.rules.service import MatchingError, MatchingService

logger = structlog.get_logger(__name__)

#: Сколько воркер держит прогон за собой. Дольше самого долгого этапа (OCR
#: многостраничного скана) и достаточно коротко, чтобы упавший воркер не
#: блокировал позицию надолго.
LEASE_TIME = timedelta(minutes=15)

#: Планы исполнения: первый этап прогона → все его этапы по порядку FSM
#: (`pipeline.bootstrap.PIPELINE_FSM`). Первый этап создаётся вместе с прогоном
#: тем, кто его запускает, и однозначно определяет план.
STAGE_PLANS: dict[str, tuple[str, ...]] = {
    "INGESTING": ("INGESTING", "EXTRACTING", "VERIFYING_SOURCES", "NORMALIZING"),
    "MATCHING": ("MATCHING",),
    "REPORTING": ("REPORTING",),
}

#: Статус позиции после извлечения: параметры ждут подтверждения менеджером.
STATUS_AFTER_EXTRACTION = "AWAITING_PARAMETER_REVIEW"
#: Если из паспорта не подтвердилось ни одного значения, сравнивать нечего, но и
#: ронять прогон незачем: разбор документа состоялся, дальше нужен человек.
STATUS_NEEDS_INPUT = "NEEDS_INPUT"
#: Причина остановки на ручном вводе — тот же код, что раньше валил прогон.
NO_VERIFIED_PARAMETERS = "NO_VERIFIED_PARAMETERS"
#: После сравнения — либо разбор расхождений, либо готовность к вердикту.
STATUS_AFTER_MATCHING_CLEAN = "READY_FOR_MANAGER_REVIEW"
STATUS_AFTER_MATCHING_ISSUES = "REVIEW_REQUIRED"
#: После отчёта позиция закрыта.
STATUS_AFTER_REPORTING = "COMPLETED"


def _extraction_key(run_id: uuid.UUID, role: str) -> str:
    """Ключ идемпотентности извлечения: свой на прогон и роль документа."""
    return f"pipeline-run:{run_id}:{role}:{EXECUTOR_VERSION}"


class StageFailed(Exception):
    """Этап не может быть выполнен; прогон завершается как FAILED."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class RunCancelled(Exception):
    """Между этапами обнаружен запрос отмены."""


class LeaseLost(Exception):
    """Прогон перешёл другому воркеру.

    Отдельно от `StageFailed`: этот воркер больше не имеет права писать в
    прогон — ни результат, ни отказ, иначе он затрёт работу того, кто его
    перехватил.
    """


@dataclass(frozen=True)
class RunOutcome:
    """Чем закончилась попытка исполнения задачи."""

    status: str
    run_id: uuid.UUID | None = None
    parameters: int = 0


@dataclass(frozen=True)
class _VerifiedParameters:
    """Что осталось после hard gate и по каким ролям не осталось ничего."""

    parameters: list
    empty_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class _SourceDocument:
    """Документ позиции, который разбирает прогон."""

    role: str
    document_version_id: uuid.UUID
    media_type: str
    storage_key: str


class PipelineExecutor:
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
        #: Этап, который сейчас исполняется: на нём отмечается отказ или отмена.
        #: Исполнитель обслуживает один прогон, поэтому поле однозначно.
        self._stage: StageRun | None = None

    async def execute(
        self, pipeline_run_id: uuid.UUID, *, worker_token: str
    ) -> RunOutcome:
        """Исполняет прогон целиком; безопасна к повторной доставке."""
        claimed = await self._claim(pipeline_run_id, worker_token)
        if isinstance(claimed, RunOutcome):
            return claimed
        run, item = claimed

        started = time.monotonic()
        run_id = run.id
        plan = await self._plan(run)
        try:
            if plan == "MATCHING":
                return await self._execute_matching(
                    run, item, worker_token, started=started
                )
            if plan == "REPORTING":
                return await self._execute_reporting(
                    run, item, worker_token, started=started
                )
            documents = await self._stage_ingesting(run, item, worker_token)
            analyses = await self._stage_extracting(run, item, documents, worker_token)
            verified = await self._stage_verifying(
                run, item, documents, analyses, worker_token
            )
            normalized = await self._stage_normalizing(
                run, item, verified.parameters, worker_token
            )
        except LeaseLost:
            await self._session.rollback()
            return RunOutcome("SUPERSEDED", run_id)
        except RunCancelled:
            return await self._finish_cancelled(run, item, worker_token)
        except StageFailed as failure:
            return await self._finish_failed(run, item, failure, worker_token)
        except ProviderTransientError:
            # Временная недоступность провайдера — не приговор прогону: этап
            # остаётся RUNNING и задача повторяется. Лизинг при этом снимается,
            # иначе повтор ждал бы его истечения, хотя работать уже некому.
            await self._session.rollback()
            await self._release_lease(run_id)
            raise
        except Exception:
            await self._session.rollback()
            failure = StageFailed("PIPELINE_FAILED", "Не удалось выполнить прогон")
            await self._finish_failed(run, item, failure, worker_token)
            raise

        needs_input = bool(verified.empty_roles)
        return await self._finish_completed(
            run,
            item,
            worker_token,
            started=started,
            item_status=(
                STATUS_NEEDS_INPUT if needs_input else STATUS_AFTER_EXTRACTION
            ),
            last_stage="NORMALIZING",
            parameters=normalized.normalized,
            payload={
                "normalized": normalized.normalized,
                # Явно: сравнение — отдельный прогон по команде менеджера.
                "pendingStages": ["MATCHING", "REPORTING"],
                **(
                    {
                        "needsInput": {
                            "reason": NO_VERIFIED_PARAMETERS,
                            "roles": list(verified.empty_roles),
                        }
                    }
                    if needs_input
                    else {}
                ),
            },
        )

    async def _execute_matching(
        self, run: PipelineRun, item: CheckItem, worker_token: str, *, started: float
    ) -> RunOutcome:
        """План MATCHING: сопоставление, правила и вердикт позиции."""
        stats = await self._stage_matching(run, item, worker_token)
        clean = stats.review_required == 0 and stats.not_found == 0
        return await self._finish_completed(
            run,
            item,
            worker_token,
            started=started,
            item_status=(
                STATUS_AFTER_MATCHING_CLEAN if clean else STATUS_AFTER_MATCHING_ISSUES
            ),
            last_stage="MATCHING",
            parameters=stats.comparisons,
            payload={
                "comparisons": stats.comparisons,
                "computedVerdict": stats.verdict,
                "reviewRequired": stats.review_required,
                "notFoundInFact": stats.not_found,
                "pendingStages": ["REPORTING"],
            },
        )

    async def _execute_reporting(
        self, run: PipelineRun, item: CheckItem, worker_token: str, *, started: float
    ) -> RunOutcome:
        """План REPORTING: неизменяемый snapshot и файлы отчёта."""
        stats = await self._stage_reporting(run, item, worker_token)
        return await self._finish_completed(
            run,
            item,
            worker_token,
            started=started,
            item_status=STATUS_AFTER_REPORTING,
            last_stage="REPORTING",
            parameters=stats.rows,
            payload={
                "reportId": str(stats.report_snapshot_id),
                "formats": stats.formats,
                "rows": stats.rows,
                "pendingStages": [],
            },
        )

    # --- захват прогона -------------------------------------------------

    async def _claim(
        self, pipeline_run_id: uuid.UUID, worker_token: str
    ) -> tuple[PipelineRun, CheckItem] | RunOutcome:
        """Берёт прогон в работу, если он ещё актуален и не занят."""
        run = await self._session.scalar(
            select(PipelineRun)
            .where(PipelineRun.id == pipeline_run_id)
            .with_for_update()
        )
        if run is None:
            await self._session.rollback()
            return RunOutcome("MISSING")

        # Идентификатор читается до любого отката: rollback истекает объект, и
        # обращение к `run.id` после него уходит в ленивую загрузку вне greenlet.
        run_id = run.id

        item = await self._session.get(CheckItem, run.check_item_id)
        if item is None:
            await self._session.rollback()
            return RunOutcome("MISSING", run_id)

        if run.status in ("COMPLETED", "FAILED", "CANCELLED"):
            await self._session.rollback()
            return RunOutcome("ALREADY_FINISHED", run_id)

        # Более новый прогон делает эту доставку устаревшей: retry всегда создаёт
        # новый `pipelineRunId`, и писать результат старого уже нельзя.
        newest_run_no = await self._session.scalar(
            select(PipelineRun.run_no)
            .where(PipelineRun.check_item_id == run.check_item_id)
            .order_by(PipelineRun.run_no.desc())
            .limit(1)
        )
        if newest_run_no is not None and newest_run_no > run.run_no:
            await self._session.rollback()
            return RunOutcome("SUPERSEDED", run_id)

        now = datetime.now(UTC)
        if run.cancel_requested_at is not None:
            outcome = await self._finish_cancelled(run, item, worker_token, claim=True)
            return outcome

        lease_alive = (
            run.worker_token is not None
            and run.worker_token != worker_token
            and run.lease_expires_at is not None
            and run.lease_expires_at > now
        )
        if lease_alive:
            await self._session.rollback()
            return RunOutcome("IN_PROGRESS", run_id)

        run.status = "RUNNING"
        run.worker_token = worker_token
        run.lease_expires_at = now + LEASE_TIME
        if run.started_at is None:
            run.started_at = now
        item.status = "INGESTING"
        item.version += 1
        await self._session.commit()
        return run, item

    # --- этапы ----------------------------------------------------------

    async def _stage_ingesting(
        self, run: PipelineRun, item: CheckItem, worker_token: str
    ) -> list[_SourceDocument]:
        """Проверяет наличие документов и фиксирует, что именно обрабатываем."""
        stage = await self._begin_stage(run, item, "INGESTING", worker_token)

        documents = (
            await self._session.execute(
                select(Document.role, DocumentVersion)
                .join(DocumentVersion, DocumentVersion.id == Document.active_version_id)
                .where(Document.check_item_id == item.id)
                .order_by(Document.created_at)
            )
        ).all()
        by_role: dict[str, DocumentVersion] = {}
        for role, version in documents:
            by_role.setdefault(role, version)

        if "REQUIREMENT" not in by_role:
            raise StageFailed(
                "MISSING_REQUIREMENT_DOCUMENT",
                "Не загружен документ требований (ТЗ)",
            )
        if "FACT" not in by_role:
            raise StageFailed(
                "MISSING_FACT_DOCUMENT",
                "Не загружен документ факта (паспорт)",
            )

        # Требование разбирается первым: без него сравнивать будет нечего, и
        # узнать об этом лучше до дорогого OCR паспорта.
        documents = [
            _SourceDocument(
                role=role,
                document_version_id=by_role[role].id,
                media_type=by_role[role].media_type,
                storage_key=by_role[role].storage_key,
            )
            for role in ("REQUIREMENT", "FACT")
        ]
        await self._complete_stage(
            run,
            item,
            stage,
            metrics={
                "documents": len(by_role),
                "factDocumentVersionId": str(by_role["FACT"].id),
                "requirementDocumentVersionId": str(by_role["REQUIREMENT"].id),
            },
        )
        return documents

    async def _stage_extracting(
        self,
        run: PipelineRun,
        item: CheckItem,
        documents: list[_SourceDocument],
        worker_token: str,
    ) -> dict[str, tuple[DocumentAnalysis, ExtractionRun]]:
        """Разбирает документы позиции тем же движком, что и сертификаты.

        Каждой роли — свой `extraction_run`: у ТЗ и паспорта разные источники,
        и след извлечения должен показывать, откуда взялось конкретное
        значение, а не сваливать оба документа в один прогон.
        """
        stage = await self._begin_stage(run, item, "EXTRACTING", worker_token)
        analyses: dict[str, tuple[DocumentAnalysis, ExtractionRun]] = {}
        total = len(documents)

        for index, document in enumerate(documents):
            extraction_run = await self._extraction_run(
                run, item, document, worker_token
            )
            # Прогресс общий на этап: каждый документ занимает свою долю шкалы.
            base = round(index * 100 / total)
            span = round(100 / total)

            async def report(name: str, percent: int, base=base, span=span) -> None:
                await self._report_progress(
                    run, item, stage, base + round(percent * span / 100), unit="PERCENT"
                )

            try:
                data = await self._storage.get(
                    self._settings.upload_bucket, document.storage_key
                )
                analysis = await self._analyzer.analyze(
                    data,
                    document.media_type,
                    correlation_id=str(run.id),
                    on_progress=report,
                )
            except NativeParseError as exc:
                raise StageFailed(
                    str(exc) or "DOCUMENT_PARSE_FAILED",
                    "Не удалось прочитать содержимое документа",
                ) from exc
            except ProviderTransientError:
                raise
            except ProviderError as exc:
                raise StageFailed(
                    str(exc) or "PROVIDER_FAILED",
                    "Сервис распознавания не смог обработать документ",
                ) from exc
            analyses[document.role] = (analysis, extraction_run)

        await self._complete_stage(
            run,
            item,
            stage,
            metrics={
                "pages": sum(
                    len(analysis.document.pages) for analysis, _ in analyses.values()
                ),
                "candidates": sum(
                    len(analysis.candidates) for analysis, _ in analyses.values()
                ),
                "ocrPages": sum(
                    analysis.ocr_stats.recognized_pages
                    for analysis, _ in analyses.values()
                ),
                "llmCandidates": sum(
                    analysis.llm_stats.candidates for analysis, _ in analyses.values()
                ),
                "documents": len(analyses),
            },
        )
        return analyses

    async def _stage_verifying(
        self,
        run: PipelineRun,
        item: CheckItem,
        documents: list[_SourceDocument],
        analyses: dict[str, tuple[DocumentAnalysis, ExtractionRun]],
        worker_token: str,
    ) -> _VerifiedParameters:
        """Hard gate: наружу проходят только подтверждённые исходником значения.

        Пустой паспорт прогон не роняет: разбор состоялся, просто подтвердить
        нечего — позиция уходит в `NEEDS_INPUT`, и менеджер вводит значения
        руками. Отказ здесь закрывал бы этот путь: правка параметров возможна
        только после успешного извлечения. Пустой ТЗ-документ тоже не роняет
        прогон — отказ на этапе MATCHING скажет об этом точнее.
        """
        stage = await self._begin_stage(run, item, "VERIFYING_SOURCES", worker_token)
        parameters: list = []
        empty_roles: list[str] = []
        metrics: dict = {"byRole": {}}
        for document in documents:
            analysis, extraction_run = analyses[document.role]
            persisted = await persist_analysis(
                self._session,
                document_version_id=document.document_version_id,
                run=extraction_run,
                analysis=analysis,
                min_confidence=self._settings.extraction_min_confidence,
                check_item_id=item.id,
                role=document.role,
            )
            if document.role == "FACT" and not persisted.parameters:
                empty_roles.append(document.role)
            parameters.extend(persisted.parameters)
            metrics["byRole"][document.role] = {
                "fragments": persisted.fragments,
                "candidates": persisted.candidates,
                "verified": persisted.verified,
                "rejected": persisted.candidates - persisted.verified,
            }

        totals = metrics["byRole"].values()
        metrics.update(
            fragments=sum(item["fragments"] for item in totals),
            candidates=sum(item["candidates"] for item in totals),
            verified=sum(item["verified"] for item in totals),
            rejected=sum(item["rejected"] for item in totals),
        )
        metrics["emptyRoles"] = empty_roles
        await self._complete_stage(run, item, stage, metrics=metrics)
        return _VerifiedParameters(parameters, tuple(empty_roles))

    async def _stage_normalizing(
        self, run: PipelineRun, item: CheckItem, parameters: list, worker_token: str
    ):
        """Приводит подтверждённые значения к каталогу, зафиксированному прогоном."""
        stage = await self._begin_stage(run, item, "NORMALIZING", worker_token)
        stats = await normalize_parameters(
            self._session,
            parameters,
            catalog_version_id=run.catalog_version_id,
        )
        await self._complete_stage(
            run,
            item,
            stage,
            metrics={
                "normalized": stats.normalized,
                "rejected": stats.rejected,
                "catalogVersionId": (
                    str(stats.catalog_version_id) if stats.catalog_version_id else None
                ),
            },
        )
        return stats

    async def _stage_matching(
        self, run: PipelineRun, item: CheckItem, worker_token: str
    ):
        """Сопоставление требований с фактами и вердикт позиции."""
        stage = await self._begin_stage(run, item, "MATCHING", worker_token)
        try:
            stats = await MatchingService(self._session).run(run, item)
        except MatchingError as exc:
            raise StageFailed(exc.code, exc.message) from exc

        await self._complete_stage(
            run,
            item,
            stage,
            metrics={
                "comparisons": stats.comparisons,
                "candidates": stats.candidates,
                "compliant": stats.compliant,
                "nonCompliant": stats.non_compliant,
                "reviewRequired": stats.review_required,
                "notFoundInFact": stats.not_found,
                "extraFacts": stats.extra_facts,
                "verdict": stats.verdict,
                "ruleSnapshotId": str(stats.rule_snapshot_id),
            },
        )
        return stats

    async def _stage_reporting(
        self, run: PipelineRun, item: CheckItem, worker_token: str
    ):
        """Сборка неизменяемого snapshot отчёта и файлов по нему."""
        stage = await self._begin_stage(run, item, "REPORTING", worker_token)
        service = ReportingService(self._session, self._settings, self._storage)
        try:
            stats = await service.build(run, item)
        except ReportingError as exc:
            raise StageFailed(exc.code, exc.message) from exc

        await self._complete_stage(
            run,
            item,
            stage,
            metrics={
                "reportSnapshotId": str(stats.report_snapshot_id),
                "rows": stats.rows,
                "formats": stats.formats,
                "failedFormats": stats.failed_formats,
                "bytes": stats.total_bytes,
            },
        )
        return stats

    # --- механика этапов ------------------------------------------------

    async def _begin_stage(
        self, run: PipelineRun, item: CheckItem, stage_name: str, worker_token: str
    ) -> StageRun:
        """Открывает этап: своя попытка, статус позиции и событие — атомарно."""
        await self._assert_owned(run, worker_token)

        attempt = (
            await self._session.scalar(
                select(StageRun.attempt)
                .where(
                    StageRun.pipeline_run_id == run.id,
                    StageRun.stage == stage_name,
                )
                .order_by(StageRun.attempt.desc())
                .limit(1)
            )
        ) or 0
        existing = await self._session.scalar(
            select(StageRun).where(
                StageRun.pipeline_run_id == run.id,
                StageRun.stage == stage_name,
                StageRun.attempt == attempt,
            )
        )
        now = datetime.now(UTC)
        if existing is not None and existing.status == "QUEUED":
            # Первый этап создаётся вместе с прогоном (checks.FIRST_STAGE) —
            # используем его, а не заводим вторую попытку того же этапа.
            stage = existing
            stage.status = "RUNNING"
            stage.started_at = now
        else:
            stage = StageRun(
                pipeline_run_id=run.id,
                stage=stage_name,
                attempt=attempt + 1,
                status="RUNNING",
                started_at=now,
            )
            self._session.add(stage)
        self._stage = stage

        item.status = stage_name
        item.version += 1
        run.lease_expires_at = now + LEASE_TIME
        await self._session.flush()
        await record_event(
            self._session,
            check_item_id=item.id,
            pipeline_run_id=run.id,
            event_type=STAGE_CHANGED,
            stage=stage_name,
            status="RUNNING",
        )
        await self._session.commit()
        return stage

    async def _complete_stage(
        self, run: PipelineRun, item: CheckItem, stage: StageRun, *, metrics: dict
    ) -> None:
        stage.status = "COMPLETED"
        stage.finished_at = datetime.now(UTC)
        stage.metrics = metrics
        await record_event(
            self._session,
            check_item_id=item.id,
            pipeline_run_id=run.id,
            event_type=STAGE_CHANGED,
            stage=stage.stage,
            status="COMPLETED",
            payload={"metrics": metrics},
        )
        await self._session.commit()
        await self._assert_not_cancelled(run)

    async def _report_progress(
        self,
        run: PipelineRun,
        item: CheckItem,
        stage: StageRun,
        current: int,
        *,
        total: int = 100,
        unit: str = "PERCENT",
    ) -> None:
        """Прогресс внутри длинного этапа; здесь же проверяется отмена."""
        stage.progress_current = current
        stage.progress_total = total
        run.lease_expires_at = datetime.now(UTC) + LEASE_TIME
        await record_event(
            self._session,
            check_item_id=item.id,
            pipeline_run_id=run.id,
            event_type=STAGE_CHANGED,
            stage=stage.stage,
            status="RUNNING",
            progress=Progress(current=current, total=total, unit=unit),
        )
        await self._session.commit()
        await self._assert_not_cancelled(run)

    async def _extraction_run(
        self,
        run: PipelineRun,
        item: CheckItem,
        document: _SourceDocument,
        worker_token: str,
    ) -> ExtractionRun:
        """Прогон извлечения, к которому привязываются параметры и evidence.

        Свой на каждый прогон pipeline и каждую роль документа: повторная
        проверка позиции обязана дать отдельный неизменяемый след, а не
        переписать предыдущий.
        """
        key = _extraction_key(run.id, document.role)
        existing = await self._session.scalar(
            select(ExtractionRun).where(ExtractionRun.idempotency_key == key)
        )
        if existing is not None:
            return existing
        extraction_run = ExtractionRun(
            id=uuid.uuid4(),
            check_item_id=item.id,
            document_version_id=document.document_version_id,
            method="REGEX",
            provider="native",
            model=PARSER_VERSION,
            status="RUNNING",
            pipeline_version=EXECUTOR_VERSION,
            idempotency_key=key,
            worker_token=worker_token,
            started_at=datetime.now(UTC),
            usage={"stage": "EXTRACTING", "role": document.role, "progress": 10},
        )
        self._session.add(extraction_run)
        await self._session.flush()
        return extraction_run

    # --- завершение -----------------------------------------------------

    async def _finish_completed(
        self,
        run: PipelineRun,
        item: CheckItem,
        worker_token: str,
        *,
        started: float,
        item_status: str,
        last_stage: str,
        parameters: int,
        payload: dict,
    ) -> RunOutcome:
        now = datetime.now(UTC)
        run.status = "COMPLETED"
        run.finished_at = now
        run.worker_token = None
        run.lease_expires_at = None
        item.status = item_status
        item.version += 1
        await self._close_extraction_run(run, "COMPLETED")
        await record_event(
            self._session,
            check_item_id=item.id,
            pipeline_run_id=run.id,
            event_type=RUN_COMPLETED,
            stage=last_stage,
            status="COMPLETED",
            payload={
                "itemStatus": item_status,
                "durationMs": round((time.monotonic() - started) * 1000),
                **payload,
            },
        )
        await self._session.commit()
        logger.info(
            "pipeline_run_completed",
            check_item_id=str(item.id),
            pipeline_run_id=str(run.id),
            stage=last_stage,
            item_status=item_status,
        )
        return RunOutcome("COMPLETED", run.id, parameters)

    async def _finish_failed(
        self,
        run: PipelineRun,
        item: CheckItem,
        failure: StageFailed,
        worker_token: str,
    ) -> RunOutcome:
        stage = await self._current_stage(run)
        now = datetime.now(UTC)
        if stage is not None:
            stage.status = "FAILED"
            stage.finished_at = now
            stage.error_code = failure.code
            stage.error = {
                "code": failure.code,
                "message": failure.message,
                "retryable": failure.retryable,
            }
        run.status = "FAILED"
        run.finished_at = now
        run.worker_token = None
        run.lease_expires_at = None
        item.status = "FAILED"
        item.version += 1
        await self._close_extraction_run(run, "FAILED", error=failure)
        await record_event(
            self._session,
            check_item_id=item.id,
            pipeline_run_id=run.id,
            event_type=RUN_FAILED,
            stage=stage.stage if stage is not None else None,
            status="FAILED",
            payload={
                "error": {
                    "code": failure.code,
                    "message": failure.message,
                    "retryable": failure.retryable,
                }
            },
        )
        await self._session.commit()
        logger.warning(
            "pipeline_run_failed",
            check_item_id=str(item.id),
            pipeline_run_id=str(run.id),
            error_code=failure.code,
        )
        return RunOutcome("FAILED", run.id)

    async def _finish_cancelled(
        self,
        run: PipelineRun,
        item: CheckItem,
        worker_token: str,
        *,
        claim: bool = False,
    ) -> RunOutcome:
        stage = await self._current_stage(run)
        now = datetime.now(UTC)
        if stage is not None and stage.status in ("QUEUED", "RUNNING"):
            stage.status = "CANCELLED"
            stage.finished_at = now
        run.status = "CANCELLED"
        run.finished_at = now
        run.worker_token = None
        run.lease_expires_at = None
        item.status = "CANCELLED"
        item.version += 1
        await self._close_extraction_run(run, "CANCELLED")
        await record_event(
            self._session,
            check_item_id=item.id,
            pipeline_run_id=run.id,
            event_type=RUN_CANCELLED,
            stage=stage.stage if stage is not None else None,
            status="CANCELLED",
            payload={"claimedByWorker": not claim},
        )
        await self._session.commit()
        logger.info(
            "pipeline_run_cancelled",
            check_item_id=str(item.id),
            pipeline_run_id=str(run.id),
        )
        return RunOutcome("CANCELLED", run.id)

    async def _close_extraction_run(
        self, run: PipelineRun, status: str, *, error: StageFailed | None = None
    ) -> None:
        """Закрывает извлечения прогона — по одному на роль документа."""
        keys = [_extraction_key(run.id, role) for role in ("REQUIREMENT", "FACT")]
        extraction_runs = (
            await self._session.scalars(
                select(ExtractionRun).where(ExtractionRun.idempotency_key.in_(keys))
            )
        ).all()
        for extraction_run in extraction_runs:
            extraction_run.status = status
            extraction_run.finished_at = datetime.now(UTC)
            extraction_run.worker_token = None
            if error is not None:
                extraction_run.error_code = error.code
                extraction_run.error_message = error.message

    # --- вспомогательное ------------------------------------------------

    async def _plan(self, run: PipelineRun) -> str:
        """План прогона по его этапам.

        Первый этап создаёт тот, кто запускает прогон, и он же однозначно
        задаёт план: `MATCHING` и `REPORTING` не встречаются в плане извлечения.
        """
        stages = set(
            (
                await self._session.scalars(
                    select(StageRun.stage).where(StageRun.pipeline_run_id == run.id)
                )
            ).all()
        )
        if "REPORTING" in stages:
            return "REPORTING"
        if "MATCHING" in stages:
            return "MATCHING"
        return "INGESTING"

    async def _current_stage(self, run: PipelineRun) -> StageRun | None:
        """Этап текущей попытки; при захвате отменённого прогона — первый."""
        if self._stage is not None:
            return self._stage
        return await self._session.scalar(
            select(StageRun)
            .where(StageRun.pipeline_run_id == run.id)
            .order_by(StageRun.attempt.desc())
            .limit(1)
        )

    async def _release_lease(self, run_id: uuid.UUID) -> None:
        """Снимает лизинг, не трогая статус: прогон ждёт следующей попытки.

        Через UPDATE, а не через объект: после отката он истёк, и чтение его
        полей ушло бы в ленивую загрузку вне greenlet.
        """
        await self._session.execute(
            update(PipelineRun)
            .where(PipelineRun.id == run_id, PipelineRun.status == "RUNNING")
            .values(worker_token=None, lease_expires_at=None)
        )
        await self._session.commit()

    async def _assert_owned(self, run: PipelineRun, worker_token: str) -> None:
        """Прогон мог уйти другому воркеру, пока шёл длинный этап."""
        await self._session.refresh(run, ["worker_token", "status"])
        if run.worker_token != worker_token or run.status != "RUNNING":
            raise LeaseLost()

    async def _assert_not_cancelled(self, run: PipelineRun) -> None:
        await self._session.refresh(run, ["cancel_requested_at"])
        if run.cancel_requested_at is not None:
            raise RunCancelled()
