"""Исполнение задания интеграции воркером.

Три свойства исполнителя обязательны по контракту и определяют его устройство:

  * повторная доставка безопасна — перед работой проверяется терминальный
    статус, а каждый уже выполненный шаг опознаётся по `external_links`;
  * повтор не создаёт второй файл и второй комментарий — публикуются только те
    артефакты, у которых связи ещё нет, а комментарий не пишется, если он уже
    записан;
  * провал публикации не трогает локальный отчёт — состояние живёт в
    `integration_jobs`, а не в `report_artifacts`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.modules.documents.service import (
    UploadRequest,
    UploadsService,
    content_sha256,
)
from app.modules.documents.storage import StoragePort
from app.modules.identity.models import User
from app.modules.integrations.bitrix import links
from app.modules.integrations.bitrix.errors import (
    BitrixTransportCode,
    BitrixTransportError,
)
from app.modules.integrations.bitrix.models import TERMINAL_STATUSES, IntegrationJob
from app.modules.integrations.bitrix.port import BitrixPort, OutgoingFile
from app.modules.integrations.bitrix.pricing import register_price_document
from app.modules.integrations.bitrix.retry import plan_retry
from app.modules.integrations.bitrix.summary import build_comment
from app.models import PriceFile, PriceImport, PriceImportStatus, PriceVersion, Supplier
from app.modules.reporting.models import ReportArtifact, ReportSnapshot


@dataclass(frozen=True)
class JobOutcome:
    """Чем закончилась попытка. `skipped` — повторная доставка завершённого."""

    status: str
    attempt: int
    result: dict | None = None
    error: dict | None = None
    skipped: bool = False


class BitrixJobExecutor:
    """Выполняет одно задание и переводит его в следующее состояние."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        storage: StoragePort,
        client: BitrixPort | None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._storage = storage
        self._client = client
        #: Разборы прайсов, созданных этой попыткой. Публикуются после
        #: коммита задания: иначе воркер разбора не увидит строки импорта.
        self._pending_price_imports: list[uuid.UUID] = []

    async def execute(self, job_id: uuid.UUID) -> JobOutcome:
        job = await self._session.get(IntegrationJob, job_id)
        if job is None:
            raise LookupError(f"Задание {job_id} не найдено")
        if job.status in TERMINAL_STATUSES:
            return JobOutcome(job.status, job.attempt, skipped=True)

        if self._client is None:
            return await self._fail(
                job,
                BitrixTransportError(
                    BitrixTransportCode.CONTRACT_ERROR,
                    "Интеграция с Bitrix24 не настроена",
                    retryable=False,
                ),
            )

        self._pending_price_imports = []
        job.status = "RUNNING"
        job.attempt += 1
        # Коммит до работы, а не после: счётчик попыток обязан пережить откат
        # неудачной попытки, иначе повторы шли бы по кругу с attempt = 1.
        await self._session.commit()

        try:
            result = await self._dispatch(job)
        except BitrixTransportError as error:
            return await self._fail(job, error)
        except Exception as error:  # noqa: BLE001 — воркер не должен падать
            # Всё, что не сбой портала, — отказ нашей стороны: отвергнутый
            # проверками файл, исчезнувшая позиция, недоступное хранилище.
            # Повторять такое нельзя: результат не изменится, а задание
            # осталось бы в RUNNING навсегда.
            return await self._fail(
                job,
                BitrixTransportError(
                    BitrixTransportCode.CONTRACT_ERROR,
                    _reason(error),
                    retryable=False,
                ),
            )

        job.status = "COMPLETED"
        job.result = result
        job.error = None
        job.next_retry_at = None
        job.completed_at = datetime.now(UTC)
        await self._session.commit()
        await self._publish_price_imports()
        return JobOutcome(job.status, job.attempt, result=result)

    # --- виды заданий ----------------------------------------------------

    async def _dispatch(self, job: IntegrationJob) -> dict:
        if job.type == "IMPORT_FILE":
            return await self._import_files(job)
        if job.type == "PUBLISH_REPORT":
            return await self._publish_report(job)
        raise BitrixTransportError(
            BitrixTransportCode.CONTRACT_ERROR,
            f"Неизвестный тип задания {job.type}",
            retryable=False,
        )

    async def _import_files(self, job: IntegrationJob) -> dict:
        meta = job.request_meta or {}
        user = await self._requester(meta)
        uploads = UploadsService(self._session, self._settings, self._storage)

        documents: dict[str, str] = {}
        prices: dict[str, dict] = {}
        for spec in meta.get("files", []):
            external_id = spec["externalFileId"]
            if spec.get("kind") == "PRICE":
                prices[external_id] = await self._import_price(job, spec)
                continue

            existing = await links.imported_documents(self._session, [external_id])
            if external_id in existing:
                # Повторная доставка: файл этого задания уже импортирован.
                documents[external_id] = str(existing[external_id])
                continue

            downloaded = await self._client.download_file(external_id)
            result = await uploads.upload(
                user,
                UploadRequest(
                    check_item_id=uuid.UUID(spec["checkItemId"]),
                    role=spec["role"],
                    file_name=downloaded.name,
                    data=downloaded.data,
                    sha256=content_sha256(downloaded.data),
                    source="BITRIX",
                ),
            )
            await links.link(
                self._session,
                entity_type=links.ENTITY_DOCUMENT,
                entity_id=result.document.id,
                external_entity_type=links.EXTERNAL_FILE,
                external_id=external_id,
                metadata={"fileName": downloaded.name},
            )
            documents[external_id] = str(result.document.id)
        return {"documents": documents, "prices": prices}

    async def _import_price(self, job: IntegrationJob, spec: dict) -> dict:
        """Скачивает ценовой файл сделки в контур Phase 2."""
        external_id = spec["externalFileId"]
        existing = await links.imported_price_files(self._session, [external_id])
        if external_id in existing:
            return await self._price_result(existing[external_id])

        downloaded = await self._client.download_file(external_id)
        document_type = spec["priceDocumentType"]
        price_import = await register_price_document(
            self._session,
            self._settings,
            self._storage,
            supplier_id=uuid.UUID(spec["supplierId"]),
            document_type=document_type,
            check_batch_id=(
                job.check_batch_id if document_type == "SUPPLIER_QUOTE" else None
            ),
            external_file_id=external_id,
            file_name=downloaded.name,
            media_type=downloaded.media_type,
            data=downloaded.data,
        )
        await links.link(
            self._session,
            entity_type=links.ENTITY_PRICE_FILE,
            entity_id=price_import.price_file_id,
            external_entity_type=links.EXTERNAL_FILE,
            external_id=external_id,
            metadata={"fileName": downloaded.name, "documentType": document_type},
        )
        self._pending_price_imports.append(price_import.id)
        return await self._price_result(price_import.price_file_id, price_import)

    async def _price_result(
        self, price_file_id: uuid.UUID, price_import: PriceImport | None = None
    ) -> dict:
        """Снимок ценового импорта для `integration_jobs.result`."""
        if price_import is None:
            price_import = await self._session.scalar(
                select(PriceImport).where(PriceImport.price_file_id == price_file_id)
            )
        if price_import is None:
            return {"priceFileId": str(price_file_id)}
        version = await self._session.get(PriceVersion, price_import.price_version_id)
        supplier = await self._session.get(Supplier, price_import.supplier_id)
        price_file = await self._session.get(PriceFile, price_import.price_file_id)
        return {
            "priceFileId": str(price_import.price_file_id),
            "priceVersionId": str(price_import.price_version_id),
            "priceImportId": str(price_import.id),
            "status": price_import.status,
            "supplierId": str(price_import.supplier_id),
            "supplierName": supplier.name if supplier is not None else None,
            "fileName": price_file.original_filename if price_file is not None else None,
            "documentType": version.document_type if version is not None else None,
            "checkBatchId": (
                str(version.check_batch_id)
                if version is not None and version.check_batch_id is not None
                else None
            ),
            "statusUrl": f"/api/v1/price-imports/{price_import.id}",
        }

    async def _publish_price_imports(self) -> None:
        """Ставит разбор созданных прайсов в очередь после коммита задания."""
        pending, self._pending_price_imports = self._pending_price_imports, []
        if not pending:
            return
        from app.tasks import run_price_import

        failed = False
        for import_id in pending:
            try:
                run_price_import.delay(str(import_id))
            except Exception:  # noqa: BLE001 — сбой постановки терминален для импорта
                row = await self._session.get(PriceImport, import_id)
                if row is not None:
                    row.status = PriceImportStatus.failed.value
                    row.stage = PriceImportStatus.failed.value
                    row.progress_percent = 100
                    row.error_code = "QUEUE_PUBLISH_FAILED"
                    row.error_message = "Не удалось поставить разбор в очередь"
                    failed = True
        if failed:
            await self._session.commit()

    async def _publish_report(self, job: IntegrationJob) -> dict:
        meta = job.request_meta or {}
        deal_id = meta["dealId"]
        artifact_ids = [uuid.UUID(value) for value in meta.get("reportArtifactIds", [])]

        pending: dict[str, OutgoingFile] = {}
        published: dict[str, str] = {}
        snapshot: ReportSnapshot | None = None
        for artifact_id in artifact_ids:
            artifact = await self._session.get(ReportArtifact, artifact_id)
            if artifact is None:
                raise BitrixTransportError(
                    BitrixTransportCode.CONTRACT_ERROR,
                    f"Артефакт отчёта {artifact_id} исчез до публикации",
                    retryable=False,
                )
            snapshot = snapshot or await self._session.get(
                ReportSnapshot, artifact.report_snapshot_id
            )
            already = await links.find_external_id(
                self._session,
                entity_type=links.ENTITY_REPORT_ARTIFACT,
                entity_id=artifact_id,
                external_entity_type=links.EXTERNAL_FILE,
            )
            if already is not None:
                published[str(artifact_id)] = already
                continue
            data = await self._storage.get(
                self._settings.upload_bucket, artifact.storage_key
            )
            pending[str(artifact_id)] = OutgoingFile(
                file_name=artifact.file_name,
                media_type=_media_type(artifact.format),
                data=data,
            )

        comment = await self._comment_text(job, meta, snapshot)
        result = await self._client.publish_report(
            deal_id=deal_id, files=pending, comment=comment
        )

        for artifact_id, external_id in result.file_ids.items():
            await links.link(
                self._session,
                entity_type=links.ENTITY_REPORT_ARTIFACT,
                entity_id=uuid.UUID(artifact_id),
                external_entity_type=links.EXTERNAL_FILE,
                external_id=external_id,
            )
            published[artifact_id] = external_id
        if (
            comment is not None
            and result.comment_id is not None
            and job.check_batch_id is not None
        ):
            await links.link(
                self._session,
                entity_type=links.ENTITY_CHECK_BATCH,
                entity_id=job.check_batch_id,
                external_entity_type=links.EXTERNAL_TIMELINE_COMMENT,
                external_id=result.comment_id,
            )
        return {
            "dealId": result.deal_id,
            "files": published,
            "commentId": result.comment_id,
        }

    # --- внутреннее ------------------------------------------------------

    async def _comment_text(
        self,
        job: IntegrationJob,
        meta: dict,
        snapshot: ReportSnapshot | None,
    ) -> str | None:
        """Текст комментария или `None`, если писать его не нужно.

        Повторная публикация не пишет второй комментарий: наличие связи и есть
        признак того, что сводка в сделке уже стоит.
        """
        if (
            not meta.get("addTimelineComment", True)
            or snapshot is None
            or job.check_batch_id is None
        ):
            return None
        existing = await links.find_external_id(
            self._session,
            entity_type=links.ENTITY_CHECK_BATCH,
            entity_id=job.check_batch_id,
            external_entity_type=links.EXTERNAL_TIMELINE_COMMENT,
        )
        if existing is not None:
            return None
        return build_comment(
            snapshot.payload or {},
            include_key_mismatches=bool(meta.get("includeKeyMismatches", True)),
        )

    async def _requester(self, meta: dict) -> User:
        user = await self._session.get(User, uuid.UUID(meta["requestedBy"]))
        if user is None or not user.is_active:
            raise BitrixTransportError(
                BitrixTransportCode.CONTRACT_ERROR,
                "Автор задания недоступен",
                retryable=False,
            )
        return user

    async def _fail(
        self, job: IntegrationJob, error: BitrixTransportError
    ) -> JobOutcome:
        """Применяет политику повторов и фиксирует состояние задания.

        Откат обязателен: часть импорта могла записаться до сбоя, и эти строки
        не должны сохраниться вместе с отметкой об ошибке.
        """
        # Идентификатор снимается до отката: откат помечает объект устаревшим,
        # и чтение любого его атрибута после этого — скрытый запрос в БД из
        # синхронного контекста, то есть `MissingGreenlet`.
        job_id = job.id
        await self._session.rollback()
        self._pending_price_imports = []
        job = await self._session.get(IntegrationJob, job_id)
        decision = plan_retry(error, attempt=job.attempt, settings=self._settings)
        payload = error.as_json()

        if decision.retry:
            job.status = "RETRY_SCHEDULED"
            job.next_retry_at = datetime.now(UTC) + timedelta(
                seconds=decision.delay_seconds
            )
        else:
            job.status = "FAILED"
            job.next_retry_at = None
            job.completed_at = datetime.now(UTC)
            payload["reason"] = decision.reason
        job.error = payload
        await self._session.commit()
        return JobOutcome(job.status, job.attempt, error=payload)


async def due_jobs(
    session: AsyncSession, *, limit: int = 50
) -> list[tuple[uuid.UUID, int]]:
    """Задания, у которых наступило время повтора: пары «id, номер попытки».

    Отдельная выборка нужна потому, что запланированный повтор не лежит в
    outbox: строка там уже опубликована, и вернуть задание в очередь должен
    диспетчер по `next_retry_at`. Номер попытки возвращается вместе с id — из
    него собирается ключ идемпотентности новой доставки.
    """
    rows = await session.execute(
        select(IntegrationJob.id, IntegrationJob.attempt)
        .where(
            IntegrationJob.status == "RETRY_SCHEDULED",
            IntegrationJob.next_retry_at <= datetime.now(UTC),
        )
        .order_by(IntegrationJob.next_retry_at)
        .limit(limit)
    )
    return [(job_id, attempt) for job_id, attempt in rows.all()]


def _reason(error: Exception) -> str:
    """Сообщение об отказе для `integration_jobs.error`.

    У прикладных ошибок API уже есть текст для пользователя; у остальных
    наружу идёт только имя класса — исходное сообщение может содержать пути и
    внутренние идентификаторы.
    """
    message = getattr(error, "message", None)
    return str(message) if message else f"Внутренняя ошибка: {type(error).__name__}"


def _media_type(report_format: str) -> str:
    return {
        "XLSX": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        "PDF": "application/pdf",
    }.get(report_format, "application/octet-stream")
