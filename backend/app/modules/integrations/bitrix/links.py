"""Работа со связями `external_links`.

Вынесено из сервиса: связями пользуются и HTTP-слой (показать, что файл уже
импортирован), и исполнитель задания (не загрузить отчёт дважды), а правило
уникальности у них общее — `uq_external_link`.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PriceFile
from app.modules.documents.models import Document
from app.modules.identity.models import ExternalLink

PROVIDER = "bitrix"

#: Локальные сущности, у которых бывает внешняя связь.
ENTITY_CHECK_BATCH = "check_batch"
ENTITY_DOCUMENT = "document"
ENTITY_PRICE_FILE = "price_file"
ENTITY_REPORT_ARTIFACT = "report_artifact"

#: Типы сущностей на стороне портала. Различают одинаковые external_id.
EXTERNAL_DEAL = "deal"
EXTERNAL_FILE = "file"
EXTERNAL_TIMELINE_COMMENT = "timeline_comment"


async def link(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    external_entity_type: str,
    external_id: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Создаёт связь, если её ещё нет.

    `ON CONFLICT DO NOTHING` вместо предварительного SELECT: повторная доставка
    задания выполняется параллельно с первой, и проверка «нет ли уже» между
    запросами ничего не гарантирует.
    """
    await session.execute(
        insert(ExternalLink)
        .values(
            entity_type=entity_type,
            entity_id=entity_id,
            provider=PROVIDER,
            external_entity_type=external_entity_type,
            external_id=external_id,
            # Именно `link_metadata`: ключ `metadata` в `values()` попадает в
            # declarative-`MetaData` класса, а не в колонку, и падает.
            link_metadata=metadata,
        )
        .on_conflict_do_nothing(constraint="uq_external_link")
    )


async def record_files_available(
    session: AsyncSession,
    *,
    batch_id: uuid.UUID,
    deal_id: str,
    files_count: int,
    external_url: str | None = None,
) -> None:
    """Отмечает в связи «заявка ↔ сделка», что комплект файлов показан.

    Нужно статусу комплекта (`status.compute_batch_kit_status`): по одному
    факту связи нельзя отличить «сделка найдена, файлы ещё не смотрели» от
    «файлы доступны менеджеру». Связь создаётся здесь же, если её ещё нет, —
    вызов идёт из `prepare_deal` уже после основного `link`.
    """
    row = await session.scalar(
        select(ExternalLink).where(
            ExternalLink.provider == PROVIDER,
            ExternalLink.entity_type == ENTITY_CHECK_BATCH,
            ExternalLink.entity_id == batch_id,
            ExternalLink.external_entity_type == EXTERNAL_DEAL,
            ExternalLink.external_id == deal_id,
        )
    )
    if row is None:
        await link(
            session,
            entity_type=ENTITY_CHECK_BATCH,
            entity_id=batch_id,
            external_entity_type=EXTERNAL_DEAL,
            external_id=deal_id,
            metadata={"externalUrl": external_url},
        )
        row = await session.scalar(
            select(ExternalLink).where(
                ExternalLink.provider == PROVIDER,
                ExternalLink.entity_type == ENTITY_CHECK_BATCH,
                ExternalLink.entity_id == batch_id,
                ExternalLink.external_entity_type == EXTERNAL_DEAL,
                ExternalLink.external_id == deal_id,
            )
        )
    if row is None:  # pragma: no cover — защита от рассинхронизации
        return
    metadata = dict(row.link_metadata or {})
    metadata["filesAvailable"] = files_count > 0
    metadata["filesCount"] = files_count
    if external_url:
        metadata["externalUrl"] = external_url
    row.link_metadata = metadata


async def find_external_id(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    external_entity_type: str,
) -> str | None:
    """Внешний идентификатор, уже связанный с сущностью."""
    return await session.scalar(
        select(ExternalLink.external_id).where(
            ExternalLink.provider == PROVIDER,
            ExternalLink.entity_type == entity_type,
            ExternalLink.entity_id == entity_id,
            ExternalLink.external_entity_type == external_entity_type,
        )
    )


async def imported_documents(
    session: AsyncSession, external_ids: list[str]
) -> dict[str, uuid.UUID]:
    """Внешний id файла → документ, в который он уже импортирован.

    `external_links.entity_id` не является внешним ключом (одна таблица
    ссылается на разные сущности), поэтому удалённый документ оставляет висячую
    связь. Соединение с `documents` отсекает такие связи: иначе однажды
    удалённый документ навсегда запрещал бы повторный импорт файла.
    """
    if not external_ids:
        return {}
    rows = await session.execute(
        select(ExternalLink.external_id, ExternalLink.entity_id)
        .join(Document, Document.id == ExternalLink.entity_id)
        .where(
            ExternalLink.provider == PROVIDER,
            ExternalLink.entity_type == ENTITY_DOCUMENT,
            ExternalLink.external_entity_type == EXTERNAL_FILE,
            ExternalLink.external_id.in_(external_ids),
        )
    )
    return {external_id: entity_id for external_id, entity_id in rows.all()}


async def imported_price_files(
    session: AsyncSession, external_ids: list[str]
) -> dict[str, uuid.UUID]:
    """Внешний id файла → `price_files`, в который он уже импортирован (Ф2).

    Аналог `imported_documents`, но для ценового контура: повторный импорт
    того же файла сделки не должен создавать новую версию прайса.
    """
    if not external_ids:
        return {}
    rows = await session.execute(
        select(ExternalLink.external_id, ExternalLink.entity_id)
        .join(PriceFile, PriceFile.id == ExternalLink.entity_id)
        .where(
            ExternalLink.provider == PROVIDER,
            ExternalLink.entity_type == ENTITY_PRICE_FILE,
            ExternalLink.external_entity_type == EXTERNAL_FILE,
            ExternalLink.external_id.in_(external_ids),
        )
    )
    return {external_id: entity_id for external_id, entity_id in rows.all()}
