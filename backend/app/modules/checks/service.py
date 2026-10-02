"""Сценарии модулей `check-batches` и `check-items`.

Здесь живут инварианты, которые нельзя выразить в схеме или в роутере:
состав пакета (1..5 позиций), переходы состояний, предусловия запуска прогона
и optimistic locking по `version`.

Организационной изоляции («пакет другой организации недоступен», QA
check-batches) в схеме нет: `organization_id` отсутствует у всех агрегатов,
multi-tenant — предложение §2 IMPROVEMENT_PROPOSALS. Поэтому доступ ограничен
ролью: читают все аутентифицированные, изменяют и запускают — manager/admin/
sysadmin, viewer только читает.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import func, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.errors import ApiError, NotFoundError
from app.core.pagination import decode_cursor, encode_cursor
from app.modules.catalogs.models import CatalogVersion, EquipmentType
from app.modules.checks.models import (
    DB_BATCH_STATUSES,
    ITEM_ACTIVE_STATUSES,
    ITEM_RUNNABLE_STATUSES,
    MAX_ITEMS_PER_BATCH,
    RUN_ACTIVE_STATUSES,
    STATUS_FILTER_TO_DB,
    CheckBatch,
    CheckItem,
    PipelineDefinition,
    PipelineRun,
    StageRun,
)
from app.modules.checks.schemas import (
    COMPARISON_STATUSES,
    BatchCreate,
    BatchUpdate,
    EquipmentTypeConfirm,
    ItemCreate,
    ItemRunRequest,
    ItemUpdate,
    RunRequest,
)
from app.modules.documents.models import Document
from app.modules.identity.models import TenderRef, User
from app.modules.pipeline.queue import enqueue_pipeline_run
from app.modules.verdicts.batch import refresh_batch_verdict

#: Роли, которым разрешено изменять и запускать проверки.
WRITE_ROLES = frozenset({"manager", "admin", "sysadmin"})

#: Первый этап pipeline: создаётся вместе с прогоном, чтобы у активного прогона
#: сразу был осмысленный `stage`. Остальные этапы создаёт сам pipeline.
FIRST_STAGE = "INGESTING"


class CheckErrorCode(StrEnum):
    """Доменные коды ошибок (docs/api/check-batches, docs/api/check-items)."""

    CHECK_BATCH_NOT_FOUND = "CHECK_BATCH_NOT_FOUND"
    CHECK_ITEM_NOT_FOUND = "CHECK_ITEM_NOT_FOUND"
    CHECK_BATCH_ACCESS_DENIED = "CHECK_BATCH_ACCESS_DENIED"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    EDIT_CONFLICT = "EDIT_CONFLICT"
    ITEM_COUNT_LIMIT_EXCEEDED = "ITEM_COUNT_LIMIT_EXCEEDED"
    ITEM_NOT_READY = "ITEM_NOT_READY"
    CATALOG_VERSION_NOT_PUBLISHED = "CATALOG_VERSION_NOT_PUBLISHED"
    RUN_ALREADY_ACTIVE = "RUN_ALREADY_ACTIVE"
    MISSING_REQUIREMENT_DOCUMENT = "MISSING_REQUIREMENT_DOCUMENT"
    MISSING_FACT_DOCUMENT = "MISSING_FACT_DOCUMENT"
    EQUIPMENT_TYPE_CONFIRMATION_REQUIRED = "EQUIPMENT_TYPE_CONFIRMATION_REQUIRED"
    EQUIPMENT_TYPE_NOT_FOUND = "EQUIPMENT_TYPE_NOT_FOUND"
    PIPELINE_DEFINITION_MISSING = "PIPELINE_DEFINITION_MISSING"
    TENDER_ALREADY_LINKED = "TENDER_ALREADY_LINKED"


class ItemNotReady(Exception):
    """Позиция не прошла предусловия запуска.

    Отдельный тип нужен пакетному запуску: в режиме `READY_ITEMS_ONLY` такая
    позиция попадает в `rejected`, а не превращает весь запуск в ошибку.
    `status_code` хранится здесь, потому что контракт различает причины:
    `RUN_ALREADY_ACTIVE` — конфликт состояния (409), нехватка документов —
    непройденная валидация (422).
    """

    def __init__(
        self, code: CheckErrorCode, message: str, *, status_code: int = 422
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class StartedRun:
    check_item_id: uuid.UUID
    pipeline_run_id: uuid.UUID
    status: str


@dataclass(frozen=True)
class RejectedItem:
    check_item_id: uuid.UUID
    code: str
    message: str


@dataclass(frozen=True)
class BatchRunResult:
    started: list[StartedRun]
    rejected: list[RejectedItem]


class ChecksService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    # --- доступ --------------------------------------------------------

    @staticmethod
    def assert_can_write(user: User) -> None:
        if not WRITE_ROLES & set(user.role_names):
            raise ApiError(
                CheckErrorCode.CHECK_BATCH_ACCESS_DENIED,
                "Недостаточно прав для изменения проверок",
                status_code=403,
            )

    # --- пакеты: чтение -------------------------------------------------

    async def get_batch(self, batch_id: uuid.UUID) -> CheckBatch:
        batch = await self._session.get(CheckBatch, batch_id)
        if batch is None:
            raise NotFoundError(
                "Пакет проверки не найден",
                details={"batchId": str(batch_id)},
            )
        return batch

    async def list_batches(
        self,
        *,
        status: str | None,
        search: str | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[CheckBatch], str | None]:
        """Список пакетов для экрана Orders.

        Сортировка `created_at DESC, id DESC` и курсор по этой же паре: только
        по времени курсор терял бы пакеты, созданные в одну микросекунду.
        """
        query = select(CheckBatch).order_by(
            CheckBatch.created_at.desc(), CheckBatch.id.desc()
        )

        if status:
            query = query.where(CheckBatch.status.in_(self._status_filter(status)))
        if search:
            pattern = f"%{search.strip()}%"
            query = query.where(
                CheckBatch.title.ilike(pattern) | CheckBatch.customer.ilike(pattern)
            )
        if cursor:
            created_at, last_id = self._decode_batch_cursor(cursor)
            query = query.where(
                tuple_(CheckBatch.created_at, CheckBatch.id) < (created_at, last_id)
            )

        # Берём на одну запись больше запрошенного: так видно, есть ли следующая
        # страница, без отдельного COUNT.
        rows = (await self._session.scalars(query.limit(limit + 1))).all()
        page = list(rows[:limit])
        next_cursor = (
            encode_cursor(f"{page[-1].created_at.isoformat()}|{page[-1].id}")
            if len(rows) > limit and page
            else None
        )
        return page, next_cursor

    @staticmethod
    def _status_filter(status: str) -> tuple[str, ...]:
        """Значение фильтра → статусы БД.

        Экран Orders фильтрует по укрупнённым статусам (`ACTIVE`, `CLOSED`, …),
        но принимать точное значение enum тоже нужно — им пользуются интеграции.
        """
        key = status.strip().upper()
        if key == "ALL":
            return DB_BATCH_STATUSES
        if key in STATUS_FILTER_TO_DB:
            return STATUS_FILTER_TO_DB[key]
        if key in DB_BATCH_STATUSES:
            return (key,)
        raise ApiError(
            "VALIDATION_ERROR",
            f"Неизвестный статус фильтра: {status}",
            status_code=422,
            details={"allowed": ["all", *STATUS_FILTER_TO_DB, *DB_BATCH_STATUSES]},
        )

    @staticmethod
    def _decode_batch_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
        try:
            raw_created, raw_id = decode_cursor(cursor).split("|", 1)
            return datetime.fromisoformat(raw_created), uuid.UUID(raw_id)
        except ValueError as exc:
            raise ApiError(
                "VALIDATION_ERROR", "Некорректный курсор", status_code=422
            ) from exc

    # --- пакеты: запись -------------------------------------------------

    async def create_batch(self, author: User, payload: BatchCreate) -> CheckBatch:
        self.assert_can_write(author)

        tender_ref_id = await self._resolve_tender_ref(payload)
        await self._assert_tender_ref_available(tender_ref_id)
        batch = CheckBatch(
            title=payload.title,
            customer=payload.customer,
            supplier=payload.supplier,
            status="DRAFT",
            final_verdict="PENDING",
            author_id=author.id,
            version=1,
            tender_ref_id=tender_ref_id,
        )
        self._session.add(batch)
        await self._session.flush()

        for position, item in enumerate(payload.items, start=1):
            self._session.add(
                CheckItem(
                    batch_id=batch.id,
                    position_no=item.position_no or position,
                    name=item.name,
                    supplier=item.supplier,
                    status="DRAFT",
                    version=1,
                    equipment_type_id=await self._validated_type_id(
                        item.equipment_type_id
                    ),
                )
            )
        await self._session.flush()
        await self._session.refresh(batch)
        return batch

    async def update_batch(
        self,
        batch_id: uuid.UUID,
        expected_version: int,
        payload: BatchUpdate,
        user: User,
    ) -> CheckBatch:
        self.assert_can_write(user)
        batch = await self._locked_batch(batch_id, expected_version)
        if not batch.is_editable:
            raise ApiError(
                CheckErrorCode.INVALID_STATE_TRANSITION,
                f"Реквизиты можно менять только в черновике, статус пакета: {batch.status}",
                status_code=409,
            )

        changes = payload.model_dump(exclude_unset=True)
        if "tender_id" in changes:
            tender_ref_id = await self._resolve_tender_ref(payload)
            await self._assert_tender_ref_available(
                tender_ref_id, excluding_batch_id=batch.id
            )
            batch.tender_ref_id = tender_ref_id
        for field in ("title", "customer", "supplier"):
            if field in changes:
                setattr(batch, field, changes[field])

        batch.version += 1
        await self._session.flush()
        return batch

    async def delete_batch(self, batch_id: uuid.UUID, user: User) -> None:
        """Удаляет черновик без прогонов; остальное переводится в ARCHIVED.

        Контракт (check-batches §Маршруты) допускает оба поведения. Пакет с
        историей прогонов удалять нельзя: на него ссылаются evidence и отчёты.
        """
        self.assert_can_write(user)
        batch = await self.get_batch(batch_id)

        if batch.status == "DRAFT" and not await self._has_runs(batch_id):
            await self._session.delete(batch)
            await self._session.flush()
            return

        if batch.status == "ARCHIVED":
            return
        batch.status = "ARCHIVED"
        batch.version += 1
        await self._session.flush()

    async def add_item(
        self, batch_id: uuid.UUID, payload: ItemCreate, user: User
    ) -> CheckItem:
        self.assert_can_write(user)
        batch = await self.get_batch(batch_id)
        if not batch.is_editable:
            raise ApiError(
                CheckErrorCode.INVALID_STATE_TRANSITION,
                f"Позицию можно добавить только в черновик, статус пакета: {batch.status}",
                status_code=409,
            )

        occupied = {item.position_no for item in batch.items}
        if len(occupied) >= MAX_ITEMS_PER_BATCH:
            raise ApiError(
                CheckErrorCode.ITEM_COUNT_LIMIT_EXCEEDED,
                f"В пакете не может быть больше {MAX_ITEMS_PER_BATCH} позиций",
                status_code=422,
            )

        position = payload.position_no or next(
            number
            for number in range(1, MAX_ITEMS_PER_BATCH + 1)
            if number not in occupied
        )
        if position in occupied:
            raise ApiError(
                CheckErrorCode.INVALID_STATE_TRANSITION,
                f"Позиция {position} в пакете уже занята",
                status_code=409,
            )

        item = CheckItem(
            batch_id=batch.id,
            position_no=position,
            name=payload.name,
            supplier=payload.supplier,
            status="DRAFT",
            version=1,
            equipment_type_id=await self._validated_type_id(payload.equipment_type_id),
        )
        self._session.add(item)
        await self._session.flush()
        return item

    # --- позиции: чтение ------------------------------------------------

    async def get_item(self, item_id: uuid.UUID) -> CheckItem:
        item = await self._session.get(CheckItem, item_id)
        if item is None:
            raise ApiError(
                CheckErrorCode.CHECK_ITEM_NOT_FOUND,
                "Позиция проверки не найдена",
                status_code=404,
                details={"checkItemId": str(item_id)},
            )
        return item

    async def document_counts(self, item_id: uuid.UUID) -> dict[str, int]:
        """Сколько документов каждой роли имеют активную версию."""
        rows = await self._session.execute(
            select(Document.role, func.count())
            .where(
                Document.check_item_id == item_id,
                Document.active_version_id.is_not(None),
            )
            .group_by(Document.role)
        )
        counts = {"REQUIREMENT": 0, "FACT": 0}
        counts.update({role: count for role, count in rows.all()})
        return counts

    async def active_run(
        self, item_id: uuid.UUID
    ) -> tuple[PipelineRun, str | None] | None:
        """Активный прогон позиции и его текущий этап."""
        run = await self._session.scalar(
            select(PipelineRun)
            .where(
                PipelineRun.check_item_id == item_id,
                PipelineRun.status.in_(RUN_ACTIVE_STATUSES),
            )
            .order_by(PipelineRun.run_no.desc())
            .limit(1)
        )
        if run is None:
            return None
        stage = await self._session.scalar(
            select(StageRun.stage)
            .where(StageRun.pipeline_run_id == run.id)
            .order_by(StageRun.status == "RUNNING", StageRun.attempt.desc())
            .limit(1)
        )
        return run, stage

    async def proposed_types(self, item: CheckItem) -> list[EquipmentType]:
        """Предложенный классификатором тип. Список — на будущее (top-N)."""
        if item.proposed_type_id is None:
            return []
        proposed = await self._session.get(EquipmentType, item.proposed_type_id)
        return [proposed] if proposed else []

    async def equipment_type(self, item: CheckItem) -> EquipmentType | None:
        if item.equipment_type_id is None:
            return None
        return await self._session.get(EquipmentType, item.equipment_type_id)

    async def item_documents(self, item_id: uuid.UUID) -> dict[str, list[dict]]:
        """Документы позиции по ролям вместе с активной версией."""
        rows = await self._session.execute(
            text(
                "SELECT d.id, d.role, d.created_at, v.id AS version_id, v.file_name,"
                "       v.media_type, v.size_bytes, v.sha256, v.status"
                "  FROM documents d"
                "  LEFT JOIN document_versions v ON v.id = d.active_version_id"
                " WHERE d.check_item_id = :item_id"
                " ORDER BY d.created_at"
            ),
            {"item_id": item_id},
        )
        grouped: dict[str, list[dict]] = {"REQUIREMENT": [], "FACT": []}
        for row in rows.mappings():
            grouped[row["role"]].append(dict(row))
        return grouped

    async def item_runs(
        self, item_id: uuid.UUID
    ) -> list[tuple[PipelineRun, str | None]]:
        """История прогонов позиции, свежие первыми."""
        runs = (
            await self._session.scalars(
                select(PipelineRun)
                .where(PipelineRun.check_item_id == item_id)
                .order_by(PipelineRun.run_no.desc())
            )
        ).all()
        codes = await self._catalog_codes([r.catalog_version_id for r in runs])
        return [(run, codes.get(run.catalog_version_id)) for run in runs]

    # --- позиции: запись ------------------------------------------------

    async def update_item(
        self, item_id: uuid.UUID, expected_version: int, payload: ItemUpdate, user: User
    ) -> CheckItem:
        self.assert_can_write(user)
        item = await self._locked_item(item_id, expected_version)
        if not item.is_editable:
            raise ApiError(
                CheckErrorCode.INVALID_STATE_TRANSITION,
                f"Позицию нельзя менять в статусе {item.status}",
                status_code=409,
            )

        changes = payload.model_dump(exclude_unset=True)
        if "equipment_type_id" in changes:
            item.equipment_type_id = await self._validated_type_id(
                payload.equipment_type_id
            )
        for field in ("name", "supplier"):
            if field in changes:
                setattr(item, field, changes[field])

        item.version += 1
        await self._session.flush()
        return item

    async def confirm_equipment_type(
        self,
        item_id: uuid.UUID,
        expected_version: int,
        payload: EquipmentTypeConfirm,
        user: User,
    ) -> CheckItem:
        """Подтверждает предложенный тип либо заменяет его указанным."""
        self.assert_can_write(user)
        item = await self._locked_item(item_id, expected_version)

        chosen = payload.equipment_type_id or item.proposed_type_id
        if chosen is None:
            raise ApiError(
                CheckErrorCode.EQUIPMENT_TYPE_CONFIRMATION_REQUIRED,
                "Тип оборудования не предложен: укажите его явно",
                status_code=422,
            )

        item.equipment_type_id = await self._validated_type_id(chosen)
        # Ручное подтверждение снимает предложение: дальше источник истины —
        # подтверждённый тип, иначе карточка показывала бы оба.
        item.proposed_type_id = None
        item.type_confidence = None
        if item.status == "NEEDS_INPUT":
            item.status = "DRAFT"
        item.version += 1
        await self._session.flush()
        return item

    # --- запуск и отмена ------------------------------------------------

    async def run_item(
        self, item_id: uuid.UUID, payload: ItemRunRequest, user: User
    ) -> StartedRun:
        self.assert_can_write(user)
        item = await self.get_item(item_id)
        catalog = await self._published_catalog(payload.catalog_version)
        try:
            return await self._start_run(item, catalog, force=payload.force_reprocess)
        except ItemNotReady as not_ready:
            raise ApiError(
                not_ready.code, not_ready.message, status_code=not_ready.status_code
            ) from not_ready

    async def retry_item(
        self, item_id: uuid.UUID, payload: ItemRunRequest, user: User
    ) -> StartedRun:
        """Повтор FAILED/CANCELLED прогона: всегда новый `pipelineRunId`.

        Предыдущий прогон остаётся в истории — он нужен аудиту (check-items
        §Отмена и retry).
        """
        self.assert_can_write(user)
        item = await self.get_item(item_id)
        if item.status not in ("FAILED", "CANCELLED"):
            raise ApiError(
                CheckErrorCode.INVALID_STATE_TRANSITION,
                f"Повтор доступен для FAILED и CANCELLED, статус позиции: {item.status}",
                status_code=409,
            )
        catalog = await self._published_catalog(payload.catalog_version)
        try:
            return await self._start_run(item, catalog, force=True)
        except ItemNotReady as not_ready:
            raise ApiError(
                not_ready.code, not_ready.message, status_code=not_ready.status_code
            ) from not_ready

    async def run_batch(
        self, batch_id: uuid.UUID, payload: RunRequest, user: User
    ) -> BatchRunResult:
        """Пакетный запуск. Политика частичного запуска — из запроса."""
        self.assert_can_write(user)
        batch = await self.get_batch(batch_id)
        catalog = await self._published_catalog(payload.catalog_version)

        if payload.mode == "ALL_OR_NOTHING":
            # Сначала проверяем все позиции и только потом запускаем: так режим
            # «всё или ничего» не требует откатывать уже созданные прогоны.
            for item in batch.items:
                try:
                    await self._assert_runnable(item, force=False)
                except ItemNotReady as not_ready:
                    raise ApiError(
                        CheckErrorCode.ITEM_NOT_READY,
                        f"Позиция {item.position_no} не готова: {not_ready.message}",
                        status_code=422,
                        details={
                            "checkItemId": str(item.id),
                            "code": str(not_ready.code),
                        },
                    ) from not_ready

        started: list[StartedRun] = []
        rejected: list[RejectedItem] = []
        for item in batch.items:
            try:
                started.append(await self._start_run(item, catalog, force=False))
            except ItemNotReady as not_ready:
                rejected.append(
                    RejectedItem(
                        check_item_id=item.id,
                        code=str(not_ready.code),
                        message=not_ready.message,
                    )
                )

        if started and batch.status == "DRAFT":
            batch.status = "QUEUED"
            batch.version += 1
        await self._session.flush()
        return BatchRunResult(started=started, rejected=rejected)

    async def cancel_item(self, item_id: uuid.UUID, user: User) -> list[uuid.UUID]:
        self.assert_can_write(user)
        item = await self.get_item(item_id)
        return await self._cancel_items([item])

    async def cancel_batch(self, batch_id: uuid.UUID, user: User) -> list[uuid.UUID]:
        self.assert_can_write(user)
        batch = await self.get_batch(batch_id)
        cancelled = await self._cancel_items(list(batch.items))
        if cancelled and batch.status in ("QUEUED", "RUNNING"):
            batch.status = "CANCELLED"
            batch.version += 1
        await self._session.flush()
        return cancelled

    async def _cancel_items(self, items: list[CheckItem]) -> list[uuid.UUID]:
        """Запрашивает отмену незавершённых позиций.

        Контракт отвечает `CANCEL_REQUESTED` (check-items §Отмена и retry):
        отмена асинхронна, worker проверяет флаг между этапами. Поэтому у
        подхваченного прогона выставляется только `cancel_requested_at`, а в
        `CANCELLED` его переводит исполнитель — иначе два процесса писали бы
        в один прогон.

        Прогон, который никто не подхватил, отменяется сразу: ждать воркера
        незачем, а `worker_token` показывает, что работа не начиналась.
        """
        now = datetime.now(UTC)
        cancelled: list[uuid.UUID] = []
        for item in items:
            if item.status not in ITEM_ACTIVE_STATUSES:
                continue
            runs = (
                await self._session.scalars(
                    select(PipelineRun).where(
                        PipelineRun.check_item_id == item.id,
                        PipelineRun.status.in_(RUN_ACTIVE_STATUSES),
                    )
                )
            ).all()
            claimed = False
            for run in runs:
                run.cancel_requested_at = now
                if run.worker_token is None:
                    run.status = "CANCELLED"
                    run.finished_at = now
                else:
                    claimed = True
            if not claimed:
                item.status = "CANCELLED"
                item.version += 1
            cancelled.append(item.id)
        await self._session.flush()
        return cancelled

    async def confirm_parameters(self, item_id: uuid.UUID, user: User) -> StartedRun:
        """Подтверждение параметров менеджером запускает сравнение.

        Канон §16: `MATCHING` не стартует сам — только явной идемпотентной
        командой. Версия каталога берётся у прогона, который эти параметры
        извлёк: сравнивать по другой версии справочника значило бы сравнивать
        не то, что нормализовано.
        """
        self.assert_can_write(user)
        item = await self.get_item(item_id)
        if item.status != "AWAITING_PARAMETER_REVIEW":
            raise ApiError(
                CheckErrorCode.INVALID_STATE_TRANSITION,
                "Подтверждать параметры можно в статусе AWAITING_PARAMETER_REVIEW,"
                f" статус позиции: {item.status}",
                status_code=409,
            )
        return await self._start_stage_run(item, first_stage="MATCHING")

    async def start_reporting(self, item_id: uuid.UUID, user: User) -> StartedRun:
        """Сборка отчёта: только после подтверждённого вердикта (канон §17)."""
        self.assert_can_write(user)
        item = await self.get_item(item_id)
        if item.status != "VERDICT_APPROVED":
            raise ApiError(
                "VERDICT_NOT_APPROVED",
                "Отчёт формируется после подтверждения вердикта позиции,"
                f" статус позиции: {item.status}",
                status_code=409,
            )
        return await self._start_stage_run(item, first_stage="REPORTING")

    async def mark_verdict_approved(self, item: CheckItem) -> None:
        """Переводит позицию в `VERDICT_APPROVED` после решения менеджера.

        Статусами позиции распоряжается этот модуль — `verdicts` пишет свой
        вердикт и просит перевести позицию, а не меняет чужой агрегат сам.
        """
        item.status = "VERDICT_APPROVED"
        item.version += 1
        await self._session.flush()

    async def _start_run(
        self, item: CheckItem, catalog: CatalogVersion, *, force: bool
    ) -> StartedRun:
        """Проверяет предусловия, создаёт прогон и ставит его в очередь."""
        await self._assert_runnable(item, force=force)
        return await self._start_stage_run(
            item, first_stage=FIRST_STAGE, catalog_version_id=catalog.id
        )

    async def _start_stage_run(
        self,
        item: CheckItem,
        *,
        first_stage: str,
        catalog_version_id: uuid.UUID | None = None,
    ) -> StartedRun:
        """Создаёт прогон с заданным первым этапом и ставит его в очередь.

        Первый этап задаёт исполнителю план работы (`pipeline.service`).
        Задача уходит через транзакционный outbox в той же транзакции, что и сам
        прогон: воркер не может получить задачу раньше, чем прогон появится в
        базе, а откат запроса не оставит висящей задачи.
        """
        if await self.active_run(item.id) is not None:
            raise ApiError(
                CheckErrorCode.RUN_ALREADY_ACTIVE,
                "У позиции уже есть активный прогон",
                status_code=409,
            )

        definition = await self._pipeline_definition()
        run_no = (
            await self._session.scalar(
                select(func.coalesce(func.max(PipelineRun.run_no), 0)).where(
                    PipelineRun.check_item_id == item.id
                )
            )
        ) + 1
        if catalog_version_id is None:
            catalog_version_id = await self._catalog_of_previous_run(item)

        run = PipelineRun(
            check_item_id=item.id,
            run_no=run_no,
            status="QUEUED",
            pipeline_definition_id=definition.id,
            catalog_version_id=catalog_version_id,
        )
        self._session.add(run)
        await self._session.flush()
        self._session.add(
            StageRun(
                pipeline_run_id=run.id,
                stage=first_stage,
                attempt=1,
                status="QUEUED",
            )
        )

        item.status = "QUEUED"
        item.version += 1
        await enqueue_pipeline_run(
            self._session, check_item_id=item.id, pipeline_run_id=run.id
        )
        await self._session.flush()
        return StartedRun(
            check_item_id=item.id, pipeline_run_id=run.id, status=run.status
        )

    async def _catalog_of_previous_run(self, item: CheckItem) -> uuid.UUID | None:
        """Версия каталога предыдущего прогона позиции."""
        return await self._session.scalar(
            select(PipelineRun.catalog_version_id)
            .where(
                PipelineRun.check_item_id == item.id,
                PipelineRun.catalog_version_id.is_not(None),
            )
            .order_by(PipelineRun.run_no.desc())
            .limit(1)
        )

    async def _assert_runnable(self, item: CheckItem, *, force: bool) -> None:
        """Предусловия запуска (check-items §Условия запуска)."""
        # Активный прогон проверяется первым: иначе повторный запуск сообщал бы
        # про «неподходящий статус», хотя настоящая причина конкретнее.
        if await self.active_run(item.id) is not None:
            raise ItemNotReady(
                CheckErrorCode.RUN_ALREADY_ACTIVE,
                "у позиции уже есть активный прогон",
                status_code=409,
            )
        if not force and item.status not in ITEM_RUNNABLE_STATUSES:
            raise ItemNotReady(
                CheckErrorCode.ITEM_NOT_READY,
                f"позицию нельзя запустить в статусе {item.status}",
                status_code=409,
            )

        counts = await self.document_counts(item.id)
        if not counts["REQUIREMENT"]:
            raise ItemNotReady(
                CheckErrorCode.MISSING_REQUIREMENT_DOCUMENT,
                "не загружен документ требований (ТЗ)",
            )
        if not counts["FACT"]:
            raise ItemNotReady(
                CheckErrorCode.MISSING_FACT_DOCUMENT,
                "не загружен документ факта (паспорт)",
            )

        # Предложенный, но не подтверждённый тип — это ожидание решения
        # менеджера. Если предложения нет вовсе, классификация — работа
        # pipeline, и он остановится на NEEDS_INPUT сам.
        if item.equipment_type_id is None and item.proposed_type_id is not None:
            raise ItemNotReady(
                CheckErrorCode.EQUIPMENT_TYPE_CONFIRMATION_REQUIRED,
                "предложенный тип оборудования не подтверждён",
            )

    # --- summary --------------------------------------------------------

    async def batch_summary(self, batch_id: uuid.UUID) -> dict:
        batch = await self.get_batch(batch_id)
        item_ids = [item.id for item in batch.items]

        by_status: dict[str, int] = {}
        for item in batch.items:
            by_status[item.status] = by_status.get(item.status, 0) + 1

        # Расчёт подтверждается на чтение: вердикты позиций меняются и вне
        # этого модуля, а сводка обязана показывать текущее состояние.
        await refresh_batch_verdict(self._session, batch.id)
        return {
            "batch_id": batch.id,
            "items_total": len(item_ids),
            "items_by_status": by_status,
            "comparisons": await self._comparison_counts(item_ids),
            "computed_verdict": batch.final_verdict,
            "manager_verdict": batch.manager_verdict,
        }

    async def _comparison_counts(self, item_ids: list[uuid.UUID]) -> dict[str, int]:
        """Распределение сравнений по статусам; все ключи присутствуют всегда."""
        counts = dict.fromkeys(COMPARISON_STATUSES, 0)
        if not item_ids:
            return counts
        rows = await self._session.execute(
            text(
                "SELECT effective_status, count(*) FROM comparisons"
                " WHERE check_item_id = ANY(:item_ids) GROUP BY effective_status"
            ),
            {"item_ids": item_ids},
        )
        counts.update({status: count for status, count in rows.all()})
        return counts

    # --- данные для ответов ---------------------------------------------

    async def authors_of(self, batches: list[CheckBatch]) -> dict[uuid.UUID, User]:
        """Авторы пакетов одним запросом: ответ обязан содержать displayName."""
        ids = {batch.author_id for batch in batches}
        if not ids:
            return {}
        users = (
            await self._session.scalars(select(User).where(User.id.in_(ids)))
        ).all()
        return {user.id: user for user in users}

    async def tender_ids_of(self, batches: list[CheckBatch]) -> dict[uuid.UUID, str]:
        """Внешние идентификаторы сделок по ссылкам пакетов."""
        ids = {b.tender_ref_id for b in batches if b.tender_ref_id is not None}
        if not ids:
            return {}
        rows = await self._session.execute(
            select(TenderRef.id, TenderRef.tender_id).where(TenderRef.id.in_(ids))
        )
        return {ref_id: tender_id for ref_id, tender_id in rows.all()}

    # --- вспомогательное ------------------------------------------------

    async def _locked_batch(self, batch_id: uuid.UUID, expected: int) -> CheckBatch:
        batch = await self.get_batch(batch_id)
        self._assert_version(batch.version, expected, "пакет")
        return batch

    async def _locked_item(self, item_id: uuid.UUID, expected: int) -> CheckItem:
        item = await self.get_item(item_id)
        self._assert_version(item.version, expected, "позиция")
        return item

    @staticmethod
    def _assert_version(actual: int, expected: int, entity: str) -> None:
        if actual != expected:
            raise ApiError(
                CheckErrorCode.EDIT_CONFLICT,
                f"{entity.capitalize()} изменена другим пользователем",
                status_code=409,
                details={"currentVersion": actual, "expectedVersion": expected},
            )

    async def _has_runs(self, batch_id: uuid.UUID) -> bool:
        return bool(
            await self._session.scalar(
                select(func.count())
                .select_from(PipelineRun)
                .join(CheckItem, CheckItem.id == PipelineRun.check_item_id)
                .where(CheckItem.batch_id == batch_id)
            )
        )

    async def _published_catalog(self, code: str | None) -> CatalogVersion:
        """Опубликованная версия каталога по коду либо версия по умолчанию."""
        query = select(CatalogVersion).where(CatalogVersion.status == "PUBLISHED")
        query = (
            query.where(CatalogVersion.code == code)
            if code
            else query.where(CatalogVersion.is_default.is_(True))
        )
        catalog = await self._session.scalar(query.limit(1))
        if catalog is None:
            raise ApiError(
                CheckErrorCode.CATALOG_VERSION_NOT_PUBLISHED,
                (
                    f"Версия каталога {code} не опубликована"
                    if code
                    else "Нет опубликованной версии каталога по умолчанию"
                ),
                status_code=422,
            )
        return catalog

    async def _pipeline_definition(self) -> PipelineDefinition:
        definition = await self._session.scalar(
            select(PipelineDefinition)
            .order_by(PipelineDefinition.created_at.desc())
            .limit(1)
        )
        if definition is None:
            raise ApiError(
                CheckErrorCode.PIPELINE_DEFINITION_MISSING,
                "Версия pipeline не зарегистрирована: запуск невозможен",
                status_code=503,
            )
        return definition

    async def _catalog_codes(self, ids: list[uuid.UUID | None]) -> dict[uuid.UUID, str]:
        present = [value for value in ids if value is not None]
        if not present:
            return {}
        rows = await self._session.execute(
            select(CatalogVersion.id, CatalogVersion.code).where(
                CatalogVersion.id.in_(present)
            )
        )
        return {row_id: code for row_id, code in rows.all()}

    async def _validated_type_id(self, type_id: uuid.UUID | None) -> uuid.UUID | None:
        """Проверяет, что тип существует и активен: FK этого не гарантирует."""
        if type_id is None:
            return None
        equipment_type = await self._session.get(EquipmentType, type_id)
        if equipment_type is None or not equipment_type.active:
            raise ApiError(
                CheckErrorCode.EQUIPMENT_TYPE_NOT_FOUND,
                "Тип оборудования не найден или неактивен",
                status_code=422,
                details={"equipmentTypeId": str(type_id)},
            )
        return type_id

    async def _resolve_tender_ref(
        self, payload: BatchCreate | BatchUpdate
    ) -> uuid.UUID | None:
        """Находит или создаёт ссылку на сделку по внешнему `tenderId`.

        `tender_refs.organization` обязателен, а понятия организации в схеме
        ещё нет (multi-tenant — предложение §2): подставляется название из
        настроек. При появлении организаций здесь будет организация автора.
        """
        if not payload.tender_id:
            return None

        existing = await self._session.scalar(
            select(TenderRef).where(TenderRef.tender_id == payload.tender_id).limit(1)
        )
        if existing is not None:
            return existing.id

        tender = TenderRef(
            organization=self._settings.organization_name,
            tender_id=payload.tender_id,
            title=payload.title,
            customer=payload.customer,
        )
        self._session.add(tender)
        await self._session.flush()
        return tender.id

    async def _assert_tender_ref_available(
        self,
        tender_ref_id: uuid.UUID | None,
        *,
        excluding_batch_id: uuid.UUID | None = None,
    ) -> None:
        """Не допускает две локальные заявки на один внешний тендер."""
        if tender_ref_id is None:
            return

        query = select(CheckBatch.id).where(
            CheckBatch.tender_ref_id == tender_ref_id
        )
        if excluding_batch_id is not None:
            query = query.where(CheckBatch.id != excluding_batch_id)
        existing_id = await self._session.scalar(query.limit(1))
        if existing_id is not None:
            raise ApiError(
                CheckErrorCode.TENDER_ALREADY_LINKED,
                "Тендер уже связан с другой заявкой",
                status_code=409,
                details={
                    "tenderRefId": str(tender_ref_id),
                    "checkBatchId": str(existing_id),
                },
            )
