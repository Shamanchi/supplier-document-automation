"""Статус комплекта заявки (этап 4 плана адаптации).

Статус показывает, насколько заявка (`CheckBatch`) укомплектована данными из
сделки Bitrix24: найдена ли сделка, показан ли комплект файлов, импортированы
ли документы Ф3, что происходит с ценовыми документами Ф2 и можно ли запускать
расчёт.

Значение вычисляется только по локальным данным (`external_links`, документы,
`price_versions`, `price_imports`, `integration_jobs`) и не обращается к
порталу: экран заявки не должен зависеть от доступности Bitrix24.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PriceImport, PriceVersion
from app.modules.checks.models import CheckBatch, CheckItem
from app.modules.documents.models import Document
from app.modules.identity.models import ExternalLink, TenderRef
from app.modules.integrations.bitrix import links
from app.modules.integrations.bitrix.models import IntegrationJob

#: Статусы комплекта: перечислены в docs/PHASE2_PHASE3_ADAPTATION_PLAN.md §7
#: и идут по возрастанию готовности заявки.
NOT_LINKED = "BITRIX_NOT_LINKED"
DEAL_FOUND = "BITRIX_DEAL_FOUND"
FILES_AVAILABLE = "FILES_AVAILABLE"
DOCUMENTS_IMPORTED = "DOCUMENTS_IMPORTED"
PRICE_IMPORT_RUNNING = "PRICE_IMPORT_RUNNING"
PRICE_REVIEW_REQUIRED = "PRICE_REVIEW_REQUIRED"
PRICE_READY = "PRICE_READY"
READY_FOR_CALCULATION = "READY_FOR_CALCULATION"

#: `PriceImport` в работе: разбор ещё не дошёл до терминального состояния.
PRICE_RUNNING_STATUSES = frozenset(
    {
        "queued",
        "parsing",
        "generating_rule",
        "compiling_rule",
        "reparsing",
        "quality_check",
    }
)
#: `PriceImport`, требующий человека: review, OCR или ошибка без автоповтора.
PRICE_ATTENTION_STATUSES = frozenset(
    {"review_required", "needs_ocr", "failed"}
)
#: Версия прайса, пригодная для использования в расчёте.
PRICE_READY_VERSION_STATUSES = frozenset({"validated", "active"})


@dataclass(frozen=True)
class BatchKitStatus:
    """Снимок состояния комплекта заявки."""

    check_batch_id: uuid.UUID
    status: str
    deal_linked: bool
    deal_id: str | None
    tender_id: str | None
    files_available: bool
    files_count: int | None
    documents_imported: int
    price_documents: int
    price_ready: int
    price_running: int
    price_attention: int


async def compute_batch_kit_status(
    session: AsyncSession, batch: CheckBatch
) -> BatchKitStatus:
    """Считает статус комплекта по локальным данным заявки."""
    deal_link = await session.scalar(
        select(ExternalLink).where(
            ExternalLink.provider == links.PROVIDER,
            ExternalLink.entity_type == links.ENTITY_CHECK_BATCH,
            ExternalLink.entity_id == batch.id,
            ExternalLink.external_entity_type == links.EXTERNAL_DEAL,
        )
    )
    tender_id = await _tender_id(session, batch)
    documents_imported = await _documents_count(session, batch.id)
    versions = await _batch_price_versions(session, batch.id)
    import_statuses = await _price_import_statuses(session, list(versions))

    price_running = sum(
        1 for status in import_statuses if status in PRICE_RUNNING_STATUSES
    )
    price_attention = sum(
        1 for status in import_statuses if status in PRICE_ATTENTION_STATUSES
    )
    price_ready = sum(
        1
        for version_status in versions.values()
        if version_status in PRICE_READY_VERSION_STATUSES
    )

    metadata = (deal_link.link_metadata or {}) if deal_link is not None else {}
    files_available = bool(metadata.get("filesAvailable"))
    files_count = metadata.get("filesCount")

    computed = decide_kit_status(
        deal_linked=deal_link is not None,
        files_available=files_available,
        documents_imported=documents_imported,
        price_documents=len(versions),
        price_ready=price_ready,
        price_running=price_running,
        price_attention=price_attention,
    )
    return BatchKitStatus(
        check_batch_id=batch.id,
        status=computed,
        deal_linked=deal_link is not None,
        deal_id=deal_link.external_id if deal_link is not None else None,
        tender_id=tender_id,
        files_available=files_available,
        files_count=files_count,
        documents_imported=documents_imported,
        price_documents=len(versions),
        price_ready=price_ready,
        price_running=price_running,
        price_attention=price_attention,
    )


def decide_kit_status(
    *,
    deal_linked: bool,
    files_available: bool,
    documents_imported: int,
    price_documents: int,
    price_ready: int,
    price_running: int,
    price_attention: int,
) -> str:
    """Чистое правило перехода между статусами комплекта.

    Порядок проверок — от блокирующих состояний к готовности: незавершённый
    разбор важнее уже готовых документов, а ошибка ценового документа — важнее
    частичной готовности комплекта.
    """
    if not deal_linked:
        return NOT_LINKED
    if price_running:
        return PRICE_IMPORT_RUNNING
    if price_attention:
        return PRICE_REVIEW_REQUIRED
    if documents_imported and (price_ready or not price_documents):
        # Документы Ф3 на месте, ценовых документов в комплекте нет либо они
        # готовы: расчёт можно запускать (локальный каталог тоже подходит).
        return READY_FOR_CALCULATION
    if price_ready:
        return PRICE_READY
    if documents_imported:
        return DOCUMENTS_IMPORTED
    if files_available:
        return FILES_AVAILABLE
    return DEAL_FOUND


async def _tender_id(
    session: AsyncSession, batch: CheckBatch
) -> str | None:
    if batch.tender_ref_id is None:
        return None
    tender = await session.get(TenderRef, batch.tender_ref_id)
    return tender.tender_id if tender is not None else None


async def _documents_count(session: AsyncSession, batch_id: uuid.UUID) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(Document)
        .join(CheckItem, CheckItem.id == Document.check_item_id)
        .where(CheckItem.batch_id == batch_id)
    )
    return int(count or 0)


async def _batch_price_versions(
    session: AsyncSession, batch_id: uuid.UUID
) -> dict[uuid.UUID, str]:
    """Ценовые версии, относящиеся к заявке: «id версии → статус».

    Источников два. `SUPPLIER_QUOTE` привязан к заявке напрямую
    (`check_batch_id`). `PRICE_LIST` глобален, поэтому его принадлежность
    заявке определяется заданием импорта этого комплекта: связь ищется через
    `integration_jobs` (файл, выбранный менеджером как `PRICE`) и
    `external_links` (файл сделки → `price_file`).
    """
    versions: dict[uuid.UUID, str] = {}
    rows = await session.execute(
        select(PriceVersion.id, PriceVersion.status).where(
            PriceVersion.check_batch_id == batch_id
        )
    )
    versions.update({version_id: status for version_id, status in rows.all()})

    external_ids = await _batch_price_external_ids(session, batch_id)
    if external_ids:
        file_ids = set(
            (await links.imported_price_files(session, external_ids)).values()
        )
        if file_ids:
            rows = await session.execute(
                select(PriceVersion.id, PriceVersion.status).where(
                    PriceVersion.price_file_id.in_(file_ids)
                )
            )
            versions.update(
                {version_id: status for version_id, status in rows.all()}
            )
    return versions


async def _batch_price_external_ids(
    session: AsyncSession, batch_id: uuid.UUID
) -> list[str]:
    """Внешние id ценовых файлов, выбранных менеджером для этой заявки."""
    rows = await session.execute(
        select(IntegrationJob.request_meta).where(
            IntegrationJob.type == "IMPORT_FILE",
            IntegrationJob.check_batch_id == batch_id,
        )
    )
    external_ids: list[str] = []
    for metadata in rows.scalars().all():
        for spec in (metadata or {}).get("files", []) or []:
            if spec.get("kind") == "PRICE" and spec.get("externalFileId"):
                external_ids.append(str(spec["externalFileId"]))
    return external_ids


async def _price_import_statuses(
    session: AsyncSession, version_ids: list[uuid.UUID]
) -> list[str]:
    if not version_ids:
        return []
    rows = await session.execute(
        select(PriceImport.status).where(
            PriceImport.price_version_id.in_(version_ids)
        )
    )
    return list(rows.scalars().all())
