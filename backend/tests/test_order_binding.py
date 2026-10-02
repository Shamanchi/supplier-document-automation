# -*- coding: utf-8 -*-
"""Задача №7, шаг 1: связка check-batch → расчётный лист (order_binding).

Требует PostgreSQL (TEST_DATABASE_URL): таблицы фазы 3 используют enum-типы.
Без переменной тесты пропускаются — общий паттерн conftest.

Сервис вызывается не через пул приложения, а через отдельный движок:
`SessionLocal` привязан к event loop'у TestClient, а `asyncio.run` в тесте
создаёт свой — соединения asyncpg нельзя переносить между loop'ами.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from tests.conftest import TEST_DATABASE_URL, requires_db

ADMIN = {"email": "admin@docprice.internal", "password": "admin"}
BATCHES = "/api/v1/check-batches"

T = TypeVar("T")


def _auth(client) -> dict[str, str]:
    token = client.post("/api/v1/auth/login", json=ADMIN).json()["accessToken"]
    return {"Authorization": f"Bearer {token}"}


def _batch(client, headers: dict[str, str]) -> dict:
    payload = {
        "title": "Насосы для котельной",
        "customer": "АО Заказчик",
        "items": [{"name": "Насос Н1"}, {"name": "Насос Н2"}],
    }
    response = client.post(
        BATCHES,
        json=payload,
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _run(scenario: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Прогоняет сценарий в своём loop'е со своим движком."""

    async def wrapper() -> T:
        engine = create_async_engine(TEST_DATABASE_URL)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as session:
                return await scenario(session)
        finally:
            await engine.dispose()

    return asyncio.run(wrapper())


@requires_db
def test_ensure_request_creates_lines_from_items(client):
    from app.matching.order_binding import ensure_request_for_batch

    headers = _auth(client)
    batch = _batch(client, headers)
    batch_id = uuid.UUID(batch["id"])

    async def scenario(session: AsyncSession):
        request = await ensure_request_for_batch(session, batch_id)
        await session.commit()
        await session.refresh(request)
        return request

    request = _run(scenario)
    assert request.source == "check_batch"
    assert request.status == "draft"
    assert request.title == "Насосы для котельной"
    assert request.client == "АО Заказчик"
    assert request.number.startswith("З-")
    assert [line.query_name for line in request.lines] == ["Насос Н1", "Насос Н2"]


@requires_db
def test_ensure_request_is_idempotent(client):
    from app.matching.order_binding import ensure_request_for_batch
    from app.models import PriceRequest
    from sqlalchemy import select

    headers = _auth(client)
    batch = _batch(client, headers)
    batch_id = uuid.UUID(batch["id"])

    async def first_call(session: AsyncSession):
        request = await ensure_request_for_batch(session, batch_id)
        await session.commit()
        return request.id

    async def second_call(session: AsyncSession):
        request = await ensure_request_for_batch(session, batch_id)
        await session.commit()
        rows = (
            await session.scalars(
                select(PriceRequest).where(PriceRequest.check_batch_id == batch_id)
            )
        ).all()
        return request.id, len(rows)

    first_id = _run(first_call)
    second_id, rows = _run(second_call)
    assert first_id == second_id
    assert rows == 1


@requires_db
def test_ensure_request_missing_batch(client):
    from app.matching.order_binding import BatchNotFound, ensure_request_for_batch

    async def scenario(session: AsyncSession):
        await ensure_request_for_batch(session, uuid.uuid4())

    try:
        _run(scenario)
    except BatchNotFound:
        return
    raise AssertionError("ожидали BatchNotFound для несуществующей заявки")


@requires_db
def test_order_event_roundtrip(client):
    from sqlalchemy import select

    from app.matching.order_binding import ORDER_EVENT_SENT, record_order_event
    from app.models import OrderEvent

    headers = _auth(client)
    batch = _batch(client, headers)
    batch_id = uuid.UUID(batch["id"])

    async def scenario(session: AsyncSession):
        session.add(record_order_event(batch_id, ORDER_EVENT_SENT))
        await session.commit()
        return (
            await session.scalars(
                select(OrderEvent).where(OrderEvent.check_batch_id == batch_id)
            )
        ).all()

    events = _run(scenario)
    assert [event.event_type for event in events] == [ORDER_EVENT_SENT]
    assert events[0].actor_id is None

    # неизвестный тип события отвергается на записи, а не молча теряется
    try:
        record_order_event(batch_id, "something_else")
    except ValueError:
        pass
    else:
        raise AssertionError("ожидали ValueError для неизвестного типа события")
