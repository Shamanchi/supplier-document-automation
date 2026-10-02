"""Прикладной слой интеграции с Bitrix24.

Разделение с исполнителем (`executor.py`) проходит по границе «синхронно в
запросе» / «в воркере»: здесь проверяются права и предусловия и создаётся
задание, там задание выполняется. Контракт требует именно этого — операции,
зависящие от сети и rate limits, отвечают `202` и продолжаются асинхронно.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.errors import ApiError
from app.modules.checks.models import CheckBatch, CheckItem
from app.models import Supplier
from app.modules.checks.service import ChecksService
from app.modules.identity.models import TenderRef, User
from app.modules.integrations.bitrix import links, status
from app.modules.integrations.bitrix.errors import (
    BitrixErrorCode,
    BitrixTransportCode,
    BitrixTransportError,
)
from app.modules.integrations.bitrix.models import (
    RETRYABLE_STATUSES,
    IntegrationJob,
)
from app.modules.integrations.bitrix.port import (
    BitrixDeal,
    BitrixFile,
    BitrixPort,
)
from app.modules.integrations.bitrix.queue import enqueue_bitrix_job
from app.modules.reporting.models import ReportArtifact, ReportSnapshot

#: Подсказка типа файла по имени. Порядок важен: «прайс-каталог» — это прайс, а
#: не каталог, поэтому ценовые подсказки идут первыми; «паспорт качества»
#: содержит и «паспорт», и «качеств», а тип определяет первое совпадение.
KIND_HINTS: tuple[tuple[str, str, str], ...] = (
    ("PRICE", "прайс", "85"),
    ("PRICE", "price", "80"),
    ("PRICE", "коммерческое предложение", "85"),
    ("PRICE", "счёт", "80"),
    ("PRICE", "счет", "80"),
    ("PRICE", "invoice", "75"),
    ("PRICE", "quote", "70"),
    ("PRICE", "кп", "70"),
    ("REQUIREMENT", "техническое задание", "95"),
    ("REQUIREMENT", "тз", "80"),
    ("REQUIREMENT", "спецификац", "85"),
    ("REQUIREMENT", "требован", "85"),
    ("REQUIREMENT", "опросн", "75"),
    ("FACT", "паспорт", "90"),
    ("FACT", "сертификат", "90"),
    ("FACT", "деклараци", "85"),
    ("FACT", "руководств", "75"),
    ("FACT", "инструкц", "75"),
    ("FACT", "каталог", "70"),
    ("FACT", "опросный лист поставщика", "70"),
)

#: Типы, которые одновременно являются ролью файла в заявке Ф3.
ROLE_KINDS: frozenset[str] = frozenset({"REQUIREMENT", "FACT"})


@dataclass(frozen=True)
class SuggestedKind:
    kind: str | None
    confidence: Decimal | None

    @property
    def role(self) -> str | None:
        """Роль Ф3, если тип укладывается в `CheckItem` (иначе `None`)."""
        return self.kind if self.kind in ROLE_KINDS else None

    @property
    def role_confidence(self) -> Decimal | None:
        return self.confidence if self.role is not None else None


@dataclass(frozen=True)
class SuggestedRole:
    role: str | None
    confidence: Decimal | None


def suggest_kind(file_name: str) -> SuggestedKind:
    """Предложение типа файла по имени.

    Это подсказка, а не решение: контракт прямо требует подтверждения
    менеджером, поэтому неуверенный ответ лучше отсутствующего — он не
    навязывает выбор. Автоматически считать файл прайсом по расширению нельзя,
    поэтому сопоставляется только имя.
    """
    lowered = file_name.casefold()
    for kind, hint, confidence in KIND_HINTS:
        if hint in lowered:
            return SuggestedKind(kind=kind, confidence=Decimal(confidence))
    return SuggestedKind(kind=None, confidence=None)


def suggest_role(file_name: str) -> SuggestedRole:
    """Предложение роли Ф3 по имени файла (совместимая обёртка)."""
    kind = suggest_kind(file_name)
    return SuggestedRole(role=kind.role, confidence=kind.role_confidence)


@dataclass(frozen=True)
class DealFileInfo:
    """Файл сделки с подсказкой типа и признаком уже выполненного импорта.

    Импорт бывает двух видов — документ Ф3 (`document_id`) и ценовой документ
    Ф2 (`price_file_id`), поэтому признак «уже импортирован» общий.
    """

    file: BitrixFile
    suggestion: SuggestedKind
    document_id: uuid.UUID | None = None
    price_file_id: uuid.UUID | None = None

    @property
    def already_imported(self) -> bool:
        return self.document_id is not None or self.price_file_id is not None


@dataclass(frozen=True)
class PreparedDeal:
    deal: BitrixDeal
    batch: CheckBatch
    tender: TenderRef
    files: list[DealFileInfo]
    notes: list[str]
    created: bool


#: Допустимые назначения ценового документа Ф2.
PRICE_DOCUMENT_KINDS: frozenset[str] = frozenset(
    {"PRICE_LIST", "SUPPLIER_QUOTE"}
)


@dataclass(frozen=True)
class ImportSpec:
    """Один выбранный менеджером файл: тип и назначение подтверждены.

    Для Ф3 (`REQUIREMENT`/`FACT`) заполнены `check_item_id` и `role`; для
    ценового документа Ф2 — `price_document_type` и `supplier_id`.
    `kind` может отсутствовать: старые клиенты присылают только роль, и
    тогда тип выводится из неё.
    """

    external_file_id: str
    kind: str | None = None
    check_item_id: uuid.UUID | None = None
    role: str | None = None
    price_document_type: str | None = None
    supplier_id: uuid.UUID | None = None

    @property
    def resolved_kind(self) -> str | None:
        """Тип файла: явный `kind` либо выведенный из роли Ф3."""
        if self.kind:
            return self.kind
        if self.role in ROLE_KINDS:
            return self.role
        return None


def _file_meta(spec: ImportSpec) -> dict:
    """Полезная нагрузка файла в `integration_jobs.request_meta`."""
    kind = spec.resolved_kind
    entry: dict = {"externalFileId": spec.external_file_id, "kind": kind}
    if kind in ROLE_KINDS:
        entry["checkItemId"] = str(spec.check_item_id)
        entry["role"] = spec.role or kind
    else:
        entry["priceDocumentType"] = spec.price_document_type
        entry["supplierId"] = str(spec.supplier_id)
    return entry


class BitrixService:
    """Карточка сделки, файлы, задания импорта и публикации."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        client: BitrixPort | None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._client = client
        self._checks = ChecksService(session, settings)

    # --- состояние -------------------------------------------------------

    async def health(self) -> tuple[str, str | None]:
        """Статус связи и код сбоя. Ни URL, ни токен наружу не выходят."""
        if self._client is None:
            return "DISABLED", None
        try:
            await self._client.ping()
        except BitrixTransportError as error:
            return "ERROR", str(error.code)
        return "OK", None

    # --- сделка и файлы --------------------------------------------------

    async def get_deal(self, tender_id: str) -> BitrixDeal:
        return await self._call(lambda client: client.get_deal(tender_id))

    async def prepare_deal(
        self,
        user: User,
        tender_id: str,
        *,
        check_batch_id: uuid.UUID | None,
        items: list[tuple[int | None, str, str | None]],
    ) -> PreparedDeal:
        """Создаёт или находит локальную заявку для сделки без импорта файлов.

        Блокировка строки `TenderRef` сериализует два параллельных запроса с
        разными ключами идемпотентности: оба увидят один и тот же пакет, а не
        создадут два `CheckBatch`. Файлы только перечисляются — назначение
        ролей и скачивание остаются следующим действием менеджера.
        """
        ChecksService.assert_can_write(user)
        deal = await self.get_deal(tender_id)
        tender = await self._ensure_tender_ref(deal)

        batch: CheckBatch | None = None
        created = False
        if check_batch_id is not None:
            batch = await self._locked_batch(check_batch_id)
            if batch.tender_ref_id is not None and batch.tender_ref_id != tender.id:
                raise ApiError(
                    BitrixErrorCode.BATCH_LINK_CONFLICT,
                    "Заявка уже связана с другим тендером",
                    status_code=409,
                    details={
                        "checkBatchId": str(batch.id),
                        "tenderRefId": str(batch.tender_ref_id),
                    },
                )
        else:
            batch = await self._batch_for_tender(tender.id)

        if batch is None:
            if not items:
                raise ApiError(
                    BitrixErrorCode.BATCH_ITEMS_REQUIRED,
                    "Для создания новой заявки передайте хотя бы одну позицию",
                    status_code=422,
                    details={"tenderId": tender.tender_id},
                )
            batch = CheckBatch(
                title=deal.title,
                customer=deal.customer,
                status="DRAFT",
                final_verdict="PENDING",
                author_id=user.id,
                version=1,
                tender_ref_id=tender.id,
            )
            self._session.add(batch)
            await self._session.flush()
            seen_positions: set[int] = set()
            for ordinal, (position_no, name, supplier) in enumerate(items, start=1):
                position = position_no or ordinal
                if position in seen_positions:
                    raise ApiError(
                        "VALIDATION_ERROR",
                        "Позиции заявки должны иметь разные номера",
                        status_code=422,
                        details={"positionNo": position},
                    )
                seen_positions.add(position)
                self._session.add(
                    CheckItem(
                        batch_id=batch.id,
                        position_no=position,
                        name=name,
                        supplier=supplier,
                        status="DRAFT",
                        version=1,
                    )
                )
            await self._session.flush()
            created = True

        await self._assert_batch_deal_compatibility(batch, deal.deal_id)
        await self._link_batch_to_deal(batch, deal)
        rows, notes = await self.list_files(tender_id)
        # Файлы показаны менеджеру: статус комплекта отличается от простого
        # факта связи «сделка найдена».
        await links.record_files_available(
            self._session,
            batch_id=batch.id,
            deal_id=deal.deal_id,
            files_count=len(rows),
            external_url=deal.external_url,
        )
        return PreparedDeal(
            deal=deal,
            batch=batch,
            tender=tender,
            files=rows,
            notes=notes,
            created=created,
        )

    async def batch_kit_status(self, batch_id: uuid.UUID) -> status.BatchKitStatus:
        """Статус комплекта заявки из сделки (этап 4 плана адаптации)."""
        batch = await self._checks.get_batch(batch_id)
        return await status.compute_batch_kit_status(self._session, batch)

    async def list_files(
        self, tender_id: str
    ) -> tuple[list[DealFileInfo], list[str]]:
        """Файлы сделки с подсказкой типа, признаком импорта и текстом ленты."""
        page = await self._call(lambda client: client.list_files(tender_id))
        external_ids = [file.external_file_id for file in page.files]
        imported_documents = await links.imported_documents(self._session, external_ids)
        imported_price_files = await links.imported_price_files(
            self._session, external_ids
        )
        rows = [
            DealFileInfo(
                file=file,
                suggestion=suggest_kind(file.name),
                document_id=imported_documents.get(file.external_file_id),
                price_file_id=imported_price_files.get(file.external_file_id),
            )
            for file in page.files
        ]
        return rows, page.notes

    # --- задания ---------------------------------------------------------

    async def create_import(
        self,
        user: User,
        tender_id: str,
        *,
        check_batch_id: uuid.UUID,
        files: list[ImportSpec],
        idempotency_key: str,
    ) -> IntegrationJob:
        """Ставит импорт выбранных файлов в очередь.

        Проверки выполняются здесь, а не в воркере: менеджер должен узнать о
        неверном сопоставлении сразу, а не из состояния задания. Один запрос
        может нести и документы Ф3, и ценовые документы Ф2.
        """
        ChecksService.assert_can_write(user)
        batch = await self._checks.get_batch(check_batch_id)
        deal = await self.get_deal(tender_id)

        available = {
            file.external_file_id
            for file in (
                await self._call(lambda client: client.list_files(tender_id))
            ).files
        }
        external_ids = [spec.external_file_id for spec in files]
        already_documents = await links.imported_documents(
            self._session, external_ids
        )
        already_prices = await links.imported_price_files(
            self._session, external_ids
        )
        await self._validate_import(
            batch.id,
            files,
            available=available,
            already_documents=already_documents,
            already_prices=already_prices,
        )
        await self._link_batch_to_deal(batch, deal)

        job = IntegrationJob(
            id=uuid.uuid4(),
            type="IMPORT_FILE",
            status="QUEUED",
            tender_id=tender_id,
            check_batch_id=batch.id,
            request_meta={
                "requestedBy": str(user.id),
                "dealId": deal.deal_id,
                "files": [_file_meta(spec) for spec in files],
            },
            idempotency_key=idempotency_key,
        )
        self._session.add(job)
        await self._session.flush()
        await enqueue_bitrix_job(self._session, job_id=job.id)
        return job

    async def create_publication(
        self,
        user: User,
        check_batch_id: uuid.UUID,
        *,
        artifact_ids: list[uuid.UUID],
        add_timeline_comment: bool,
        include_key_mismatches: bool,
        idempotency_key: str,
    ) -> IntegrationJob:
        """Ставит публикацию отчёта в очередь после проверки предусловий."""
        ChecksService.assert_can_write(user)
        batch = await self._checks.get_batch(check_batch_id)

        if batch.manager_verdict == "PENDING" or batch.verdict_approved_at is None:
            raise ApiError(
                BitrixErrorCode.REPORT_NOT_READY,
                "Вердикт заявки не утверждён",
                status_code=422,
                details={"batchId": str(batch.id)},
            )

        await self._assert_artifacts_ready(batch.id, artifact_ids)
        tender_id, deal_id = await self._linked_deal(batch.id, batch.tender_ref_id)

        job = IntegrationJob(
            id=uuid.uuid4(),
            type="PUBLISH_REPORT",
            status="QUEUED",
            tender_id=tender_id,
            check_batch_id=batch.id,
            request_meta={
                "requestedBy": str(user.id),
                "dealId": deal_id,
                "reportArtifactIds": [str(artifact_id) for artifact_id in artifact_ids],
                "addTimelineComment": add_timeline_comment,
                "includeKeyMismatches": include_key_mismatches,
            },
            idempotency_key=idempotency_key,
        )
        self._session.add(job)
        await self._session.flush()
        await enqueue_bitrix_job(self._session, job_id=job.id)
        return job

    async def get_job(self, job_id: uuid.UUID) -> IntegrationJob:
        job = await self._session.get(IntegrationJob, job_id)
        if job is None:
            raise ApiError(
                BitrixErrorCode.JOB_NOT_FOUND,
                "Задание интеграции не найдено",
                status_code=404,
                details={"jobId": str(job_id)},
            )
        return job

    async def retry_job(self, user: User, job_id: uuid.UUID) -> IntegrationJob:
        """Ручной повтор задания.

        Повторить можно только то, что упало по повторяемой причине: отказ прав
        и отсутствующая сделка от повтора не исправятся, а очередь займут.
        """
        ChecksService.assert_can_write(user)
        job = await self.get_job(job_id)
        if job.status not in RETRYABLE_STATUSES:
            raise ApiError(
                BitrixErrorCode.JOB_NOT_RETRYABLE,
                f"Задание в состоянии {job.status} не повторяется",
                status_code=409,
                details={"jobId": str(job.id), "status": job.status},
            )
        if job.error and job.error.get("retryable") is False:
            raise ApiError(
                BitrixErrorCode.JOB_NOT_RETRYABLE,
                "Ошибка задания не устраняется повтором",
                status_code=409,
                details={"jobId": str(job.id), "code": job.error.get("code")},
            )

        job.status = "QUEUED"
        job.next_retry_at = None
        await self._session.flush()
        await enqueue_bitrix_job(self._session, job_id=job.id, attempt=job.attempt)
        return job

    # --- внутреннее ------------------------------------------------------

    async def _call(self, operation):
        """Вызов портала с переводом сбоя в ответ API.

        Синхронные маршруты (карточка, список файлов) не имеют задания, в
        котором можно было бы хранить состояние повтора, поэтому сбой портала
        превращается здесь в ошибку HTTP.
        """
        if self._client is None:
            raise ApiError(
                BitrixErrorCode.NOT_CONFIGURED,
                "Интеграция с Bitrix24 не настроена",
                status_code=503,
            )
        try:
            return await operation(self._client)
        except BitrixTransportError as error:
            raise _as_api_error(error) from error

    async def _validate_import(
        self,
        batch_id: uuid.UUID,
        files: list[ImportSpec],
        *,
        available: set[str],
        already_documents: dict[str, uuid.UUID],
        already_prices: dict[str, uuid.UUID],
    ) -> None:
        items = await self._items_of_batch(batch_id)
        for spec in files:
            external_id = spec.external_file_id
            if external_id not in available:
                raise ApiError(
                    BitrixErrorCode.FILE_NOT_FOUND,
                    "Файл не найден в сделке",
                    status_code=404,
                    details={"externalFileId": external_id},
                )
            imported_document = already_documents.get(external_id)
            imported_price = already_prices.get(external_id)
            if imported_document is not None or imported_price is not None:
                raise ApiError(
                    BitrixErrorCode.FILE_ALREADY_IMPORTED,
                    "Файл уже импортирован",
                    status_code=409,
                    details={
                        "externalFileId": external_id,
                        "documentId": (
                            str(imported_document)
                            if imported_document is not None
                            else None
                        ),
                        "priceFileId": (
                            str(imported_price)
                            if imported_price is not None
                            else None
                        ),
                    },
                )
            kind = spec.resolved_kind
            if kind in ROLE_KINDS:
                if spec.check_item_id is None or spec.check_item_id not in items:
                    raise ApiError(
                        BitrixErrorCode.FILE_MAPPING_REQUIRED,
                        "Позиция не принадлежит указанной заявке",
                        status_code=422,
                        details={
                            "externalFileId": external_id,
                            "checkItemId": (
                                str(spec.check_item_id)
                                if spec.check_item_id is not None
                                else None
                            ),
                        },
                    )
                continue
            if kind == "PRICE":
                await self._validate_price_spec(spec)
                continue
            raise ApiError(
                BitrixErrorCode.FILE_MAPPING_REQUIRED,
                "Не удалось определить тип файла",
                status_code=422,
                details={"externalFileId": external_id},
            )

    async def _validate_price_spec(self, spec: ImportSpec) -> None:
        """Проверяет ценовой файл: поставщик, назначение и его существование."""
        if spec.supplier_id is None:
            raise ApiError(
                BitrixErrorCode.SUPPLIER_REQUIRED,
                "Для ценового файла нужно указать поставщика",
                status_code=422,
                details={"externalFileId": spec.external_file_id},
            )
        if spec.price_document_type not in PRICE_DOCUMENT_KINDS:
            raise ApiError(
                BitrixErrorCode.PRICE_DOCUMENT_TYPE_REQUIRED,
                "Не указано назначение ценового документа",
                status_code=422,
                details={"externalFileId": spec.external_file_id},
            )
        supplier = await self._session.get(Supplier, spec.supplier_id)
        if supplier is None:
            raise ApiError(
                BitrixErrorCode.SUPPLIER_NOT_FOUND,
                "Поставщик не найден",
                status_code=404,
                details={"supplierId": str(spec.supplier_id)},
            )

    async def _items_of_batch(self, batch_id: uuid.UUID) -> set[uuid.UUID]:
        rows = await self._session.execute(
            select(CheckItem.id).where(CheckItem.batch_id == batch_id)
        )
        return set(rows.scalars().all())

    async def _locked_batch(self, batch_id: uuid.UUID) -> CheckBatch:
        batch = await self._session.scalar(
            select(CheckBatch)
            .where(CheckBatch.id == batch_id)
            .with_for_update()
        )
        if batch is None:
            raise ApiError(
                "CHECK_BATCH_NOT_FOUND",
                "Пакет проверки не найден",
                status_code=404,
                details={"batchId": str(batch_id)},
            )
        return batch

    async def _batch_for_tender(self, tender_ref_id: uuid.UUID) -> CheckBatch | None:
        rows = (
            await self._session.scalars(
                select(CheckBatch)
            .where(CheckBatch.tender_ref_id == tender_ref_id)
            .order_by(CheckBatch.created_at.desc(), CheckBatch.id.desc())
            .with_for_update()
            )
        ).all()
        if len(rows) > 1:
            raise ApiError(
                BitrixErrorCode.BATCH_LINK_CONFLICT,
                "Для сделки найдено несколько локальных заявок",
                status_code=409,
                details={
                    "tenderRefId": str(tender_ref_id),
                    "checkBatchIds": [str(batch.id) for batch in rows],
                },
            )
        return rows[0] if rows else None

    async def _assert_batch_deal_compatibility(
        self, batch: CheckBatch, deal_id: str
    ) -> None:
        linked = await links.find_external_id(
            self._session,
            entity_type=links.ENTITY_CHECK_BATCH,
            entity_id=batch.id,
            external_entity_type=links.EXTERNAL_DEAL,
        )
        if linked is not None and linked != deal_id:
            raise ApiError(
                BitrixErrorCode.BATCH_LINK_CONFLICT,
                "Заявка уже связана с другой сделкой Bitrix24",
                status_code=409,
                details={"checkBatchId": str(batch.id), "dealId": linked},
            )

    async def _assert_artifacts_ready(
        self, batch_id: uuid.UUID, artifact_ids: list[uuid.UUID]
    ) -> None:
        """Артефакты должны существовать, принадлежать заявке и не быть опубликованы."""
        rows = await self._session.execute(
            select(ReportArtifact.id)
            .join(ReportSnapshot, ReportSnapshot.id == ReportArtifact.report_snapshot_id)
            .where(
                ReportArtifact.id.in_(artifact_ids),
                ReportSnapshot.check_batch_id == batch_id,
                ReportSnapshot.status.in_(("COMPLETED", "PARTIALLY_COMPLETED")),
            )
        )
        found = set(rows.scalars().all())
        missing = [str(a) for a in artifact_ids if a not in found]
        if missing:
            raise ApiError(
                BitrixErrorCode.REPORT_NOT_READY,
                "Отчёт заявки не готов к публикации",
                status_code=422,
                details={"reportArtifactIds": missing},
            )

        published = [
            str(artifact_id)
            for artifact_id in artifact_ids
            if await links.find_external_id(
                self._session,
                entity_type=links.ENTITY_REPORT_ARTIFACT,
                entity_id=artifact_id,
                external_entity_type=links.EXTERNAL_FILE,
            )
            is not None
        ]
        if published:
            raise ApiError(
                BitrixErrorCode.REPORT_ALREADY_PUBLISHED,
                "Отчёт уже опубликован в сделке",
                status_code=409,
                details={"reportArtifactIds": published},
            )

    async def _linked_deal(
        self, batch_id: uuid.UUID, tender_ref_id: uuid.UUID | None
    ) -> tuple[str, str]:
        """Номер тендера и id сделки для заявки.

        Связь появляется при импорте файлов; заявка, заведённая вручную и не
        связанная со сделкой, публиковаться не может — публиковать некуда.
        """
        deal_id = await links.find_external_id(
            self._session,
            entity_type=links.ENTITY_CHECK_BATCH,
            entity_id=batch_id,
            external_entity_type=links.EXTERNAL_DEAL,
        )
        tender = (
            await self._session.get(TenderRef, tender_ref_id)
            if tender_ref_id is not None
            else None
        )
        if deal_id is None or tender is None:
            raise ApiError(
                BitrixErrorCode.BATCH_NOT_LINKED,
                "Заявка не связана со сделкой Bitrix24",
                status_code=422,
                details={"batchId": str(batch_id)},
            )
        return tender.tender_id, deal_id

    async def _link_batch_to_deal(self, batch: CheckBatch, deal: BitrixDeal) -> None:
        """Фиксирует связь заявки со сделкой и дополняет карточку тендера."""
        await links.link(
            self._session,
            entity_type=links.ENTITY_CHECK_BATCH,
            entity_id=batch.id,
            external_entity_type=links.EXTERNAL_DEAL,
            external_id=deal.deal_id,
            metadata={"externalUrl": deal.external_url},
        )
        tender = await self._ensure_tender_ref(deal)
        batch.tender_ref_id = tender.id

    async def _ensure_tender_ref(self, deal: BitrixDeal) -> TenderRef:
        """Возвращает заблокированную локальную ссылку на сделку.

        Уникальность `tender_refs` — `(organization, tender_id)`. Сначала
        выполняется безопасный no-op insert, затем строка блокируется до конца
        транзакции, чтобы подготовка заявки была атомарной и идемпотентной.
        """
        await self._session.execute(
            pg_insert(TenderRef)
            .values(
                organization=self._settings.organization_name,
                tender_id=deal.tender_id,
                bitrix_deal_id=deal.deal_id,
                title=deal.title,
                customer=deal.customer,
            )
            .on_conflict_do_nothing(
                index_elements=[TenderRef.organization, TenderRef.tender_id]
            )
        )
        tender = await self._session.scalar(
            select(TenderRef)
            .where(
                TenderRef.organization == self._settings.organization_name,
                TenderRef.tender_id == deal.tender_id,
            )
            .with_for_update()
        )
        if tender is None:
            raise RuntimeError("TenderRef не создан и не найден после insert")

        # Ручные реквизиты не затираются: менеджер мог уточнить заказчика, а
        # стадия сделки в Bitrix могла отстать. Дописывается только пустое.
        tender.bitrix_deal_id = tender.bitrix_deal_id or deal.deal_id
        tender.title = tender.title or deal.title
        tender.customer = tender.customer or deal.customer
        return tender


def _as_api_error(error: BitrixTransportError) -> ApiError:
    """Сбой портала → ответ синхронного маршрута."""
    mapping: dict[BitrixTransportCode, tuple[str, int]] = {
        BitrixTransportCode.DEAL_NOT_FOUND: (BitrixErrorCode.DEAL_NOT_FOUND, 404),
        BitrixTransportCode.FILE_NOT_FOUND: (BitrixErrorCode.FILE_NOT_FOUND, 404),
        BitrixTransportCode.FAILED_AUTH: (str(BitrixTransportCode.FAILED_AUTH), 502),
        BitrixTransportCode.RATE_LIMITED: (str(BitrixTransportCode.RATE_LIMITED), 503),
    }
    code, status_code = mapping.get(
        error.code, (str(BitrixTransportCode.SERVER_ERROR), 502)
    )
    return ApiError(code, error.message, status_code=status_code)
