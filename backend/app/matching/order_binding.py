# -*- coding: utf-8 -*-
"""Связка канонической заявки (check-batch) с расчётным листом P2.

Задача №7 (docs/merge-remote/09-task7-kp-contour.md): заявка в UI — это
check-batch фазы 3, а контур подбора цен и КП живёт в P2-сущностях
(`price_requests` + `calculations`). Связь 1:1: `PriceRequest` создаётся
из позиций заявки при первом обращении контура Ф2 и своей страницы не
имеет. Идемпотентность — по уникальному `check_batch_id`: повторный вызов
возвращает существующий лист, а не заводит второй.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OrderEvent, PriceRequest, PriceRequestLine
from app.modules.checks.models import CheckBatch, CheckItem

#: События журнала заявки (решение №1 открытых вопросов задачи №7).
ORDER_EVENT_SENT = "sent"
ORDER_EVENT_SEND_CANCELLED = "send_cancelled"
ORDER_EVENT_CHECK_COMPLETED = "check_completed"
ORDER_EVENT_QUOTE_CONFIRMED = "quote_confirmed"
ORDER_EVENT_ARCHIVED = "archived"

ORDER_EVENT_TYPES = frozenset(
    {
        ORDER_EVENT_SENT,
        ORDER_EVENT_SEND_CANCELLED,
        ORDER_EVENT_CHECK_COMPLETED,
        ORDER_EVENT_QUOTE_CONFIRMED,
        ORDER_EVENT_ARCHIVED,
    }
)


def order_number(request: PriceRequest) -> str:
    """Детерминированный номер из id: З-<8 hex>. Как у ручных заявок P2."""
    return f"З-{request.id.hex[:8]}"


def record_order_event(
    batch_id: uuid.UUID,
    event_type: str,
    actor_id: uuid.UUID | None = None,
    details: dict | None = None,
) -> OrderEvent:
    """Строка журнала событий заявки; добавляется в сессию вызывающим."""
    if event_type not in ORDER_EVENT_TYPES:
        raise ValueError(f"неизвестный тип события заявки: {event_type}")
    return OrderEvent(
        check_batch_id=batch_id,
        event_type=event_type,
        actor_id=actor_id,
        details=details,
    )


class BatchNotFound(LookupError):
    """Check-batch не существует — вызывающий роутер отдаёт 404."""


async def ensure_request_for_batch(
    session: AsyncSession, batch_id: uuid.UUID
) -> PriceRequest:
    """Расчётный лист заявки; создаётся при первом обращении, идемпотентно.

    Строки листа — позиции заявки в порядке `position_no`: наименование
    позиции становится и исходной строкой, и поисковым запросом. Дальше их
    правит менеджер обычными ручками строк заявки.
    """
    existing = await session.scalar(
        select(PriceRequest).where(PriceRequest.check_batch_id == batch_id)
    )
    if existing is not None:
        return existing

    batch = await session.get(CheckBatch, batch_id)
    if batch is None:
        raise BatchNotFound(str(batch_id))
    items = (
        await session.scalars(
            select(CheckItem)
            .where(CheckItem.batch_id == batch_id)
            .order_by(CheckItem.position_no)
        )
    ).all()

    request = PriceRequest(
        title=batch.title or "",
        client=batch.customer or "",
        source="check_batch",
        status="draft",
        check_batch_id=batch_id,
    )
    session.add(request)
    await session.flush()  # id для номера появляется здесь
    request.number = order_number(request)
    for item in items:
        session.add(
            PriceRequestLine(
                request_id=request.id,
                ordinal=item.position_no,
                source_line=item.name,
                query_name=item.name,
                match_stage="none",
            )
        )
    await session.flush()
    return request
