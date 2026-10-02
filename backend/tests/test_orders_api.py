# -*- coding: utf-8 -*-
"""Задача №7, шаги 2-3: ручки контракта frontend/API.md на /orders.

PG-контрактные тесты (TEST_DATABASE_URL): события заявки и связка с
check-batch живут в таблицах фазы 3. Сервис связки вызывается через
отдельный движок — пул приложения привязан к loop'у TestClient.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from typing import TypeVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from tests.conftest import TEST_DATABASE_URL, requires_db

MANAGER = {"email": "manager@docprice.internal", "password": "manager"}
# email развёртываемого аккаунта (APP_VIEWER_USERNAME) может совпадать с
# dev-аккаунтом — тогда действует пароль из APP_VIEWER_PASSWORD, а не из
# BOOTSTRAP_VIEWER_PASSWORD; берём тот, что задан в окружении.
VIEWER = {
    "email": "viewer@docprice.internal",
    "password": os.getenv("APP_VIEWER_PASSWORD")
    or os.getenv("BOOTSTRAP_VIEWER_PASSWORD")
    or "viewer",
}

ORDERS = "/api/v1/orders"
BATCHES = "/api/v1/check-batches"
RAW_TEXT = "\n".join(["Огнетушитель ОП-5(з) — 2 шт", "Огнетушитель ОП-4 — 3 шт"])

T = TypeVar("T")


def _auth(client, credentials: dict = MANAGER) -> dict[str, str]:
    token = client.post("/api/v1/auth/login", json=credentials).json()["accessToken"]
    return {"Authorization": f"Bearer {token}"}


def _create(client, headers: dict[str, str]) -> dict:
    response = client.post(
        ORDERS, json={"client": "АО Заказчик", "raw_text": RAW_TEXT}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


def _run(scenario: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Прогон в своём loop'е со своим движком (см. test_order_binding)."""

    async def wrapper() -> T:
        engine = create_async_engine(TEST_DATABASE_URL)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as session:
                return await scenario(session)
        finally:
            await engine.dispose()

    return asyncio.run(wrapper())


def _batch(client, headers: dict[str, str]) -> dict:
    payload = {
        "title": "Насосы для котельной",
        "customer": "АО Заказчик",
        "items": [{"name": "Насос Н1"}],
    }
    response = client.post(
        BATCHES, json=payload, headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- RBAC (решение №4) ------------------------------------------------------


@requires_db
def test_manager_creates_order_but_viewer_cannot(client):
    headers = _auth(client)
    order = _create(client, headers)
    assert order["status"] == "draft"
    assert order["source"] == "manual"
    assert order["lines_count"] == 2

    viewer = _auth(client, VIEWER)
    denied = client.post(
        ORDERS, json={"client": "АО Заказчик", "raw_text": RAW_TEXT}, headers=viewer
    )
    assert denied.status_code == 403, denied.text

    # чтение доступно всем аутентифицированным
    listed = client.get(ORDERS, headers=viewer)
    assert listed.status_code == 200, listed.text


# --- статусная машина контракта ---------------------------------------------


@requires_db
def test_action_endpoints_lifecycle(client):
    headers = _auth(client)
    order = _create(client, headers)
    order_id = order["id"]

    # confirm-quote из draft невозможен
    assert (
        client.post(f"{ORDERS}/{order_id}/confirm-quote", headers=headers).status_code
        == 409
    )

    # send -> active; cancel-send без проверки -> обратно draft
    sent = client.post(f"{ORDERS}/{order_id}/send", headers=headers)
    assert sent.status_code == 200 and sent.json()["status"] == "active"
    cancelled = client.post(f"{ORDERS}/{order_id}/cancel-send", headers=headers)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "draft"

    # complete-check у ручной заявки бессмыслен
    assert (
        client.post(f"{ORDERS}/{order_id}/complete-check", headers=headers).status_code
        == 400
    )

    # полный путь: send -> confirm-quote -> close -> archive
    client.post(f"{ORDERS}/{order_id}/send", headers=headers)
    confirmed = client.post(f"{ORDERS}/{order_id}/confirm-quote", headers=headers)
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmation"
    closed = client.post(f"{ORDERS}/{order_id}/close", headers=headers)
    assert closed.status_code == 200 and closed.json()["status"] == "closed"
    archived = client.post(f"{ORDERS}/{order_id}/archive", headers=headers)
    assert archived.status_code == 200 and archived.json()["status"] == "archived"

    # закрытая/архивная заявка только читается
    patched = client.patch(
        f"{ORDERS}/{order_id}/quote",
        json={"vatRate": 20, "reason": "тест"},
        headers=headers,
    )
    assert patched.status_code == 409


@requires_db
def test_quote_patch_requires_reason_and_journals(client):
    headers = _auth(client)
    order = _create(client, headers)

    no_reason = client.patch(
        f"{ORDERS}/{order['id']}/quote", json={"vatRate": 20}, headers=headers
    )
    assert no_reason.status_code == 400, no_reason.text

    patched = client.patch(
        f"{ORDERS}/{order['id']}/quote",
        json={"vatRate": 20, "specialDiscount": 5, "reason": "согласовано с клиентом"},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    card = patched.json()
    assert card["vatRate"] == 20
    assert card["specialDiscount"] == 5
    actions = [entry["action"] for entry in card["auditLog"]]
    assert any("vat_rate" in action for action in actions)
    assert any("special_discount" in action for action in actions)

    # пустой PATCH отвергается
    empty = client.patch(f"{ORDERS}/{order['id']}/quote", json={}, headers=headers)
    assert empty.status_code == 400


@requires_db
def test_order_files_registration(client):
    headers = _auth(client)
    order = _create(client, headers)

    bad_kind = client.post(
        f"{ORDERS}/{order['id']}/files",
        json={"fileName": "tz.pdf", "kind": "unknown"},
        headers=headers,
    )
    assert bad_kind.status_code == 400, bad_kind.text

    added = client.post(
        f"{ORDERS}/{order['id']}/files",
        json={"fileName": "tz.pdf", "kind": "specification"},
        headers=headers,
    )
    assert added.status_code == 200, added.text
    files = added.json()["files"]
    assert [entry["name"] for entry in files] == ["tz.pdf"]
    assert files[0]["kind"] == "specification"


# --- резолв по check-batch id и события -------------------------------------


@requires_db
def test_viewer_cannot_mutate_order_actions(client):
    """Решение №4: чтение — всем, действия контура — только менеджерскому кругу."""

    headers = _auth(client)
    order = _create(client, headers)
    viewer = _auth(client, VIEWER)

    # мутации действий и правка КП для viewer закрыты
    assert (
        client.post(f"{ORDERS}/{order['id']}/send", headers=viewer).status_code == 403
    )
    assert (
        client.patch(
            f"{ORDERS}/{order['id']}/quote",
            json={"vatRate": 20, "reason": "тест"},
            headers=viewer,
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{ORDERS}/{order['id']}/files",
            json={"fileName": "tz.pdf", "kind": "specification"},
            headers=viewer,
        ).status_code
        == 403
    )
    # карточка при этом читается
    assert client.get(f"{ORDERS}/{order['id']}", headers=viewer).status_code == 200


@requires_db
def test_complete_check_for_batch_order_logs_event(client):
    """complete-check: фиксация завершения проверки ТХ заявки из пакета."""

    headers = _auth(client)
    batch = _batch(client, headers)
    assert client.post(f"{ORDERS}/{batch['id']}/send", headers=headers).status_code == 200

    completed = client.post(f"{ORDERS}/{batch['id']}/complete-check", headers=headers)
    assert completed.status_code == 200, completed.text
    actions = [entry["action"] for entry in completed.json()["auditLog"]]
    assert "Завершена проверка ТХ" in actions


@requires_db
def test_quote_patch_supplier_journaled_without_reason(client, db):
    """Смена поставщика: причина не обязательна (не НДС/скидка), журнал ведётся."""

    headers = _auth(client)
    order = _create(client, headers)

    unknown = client.patch(
        f"{ORDERS}/{order['id']}/quote",
        json={"supplierId": str(uuid.uuid4())},
        headers=headers,
    )
    assert unknown.status_code == 404, unknown.text

    db.execute(
        text(
            "INSERT INTO suppliers (id, code, name, created_at) "
            "VALUES (:id, :code, :name, now()) ON CONFLICT (code) DO NOTHING"
        ),
        {
            "id": str(uuid.uuid4()),
            "code": "test-supplier-task7",
            "name": "ООО «Поставщик №7»",
        },
    )
    db.commit()
    supplier_id = db.execute(
        text("SELECT id FROM suppliers WHERE code = 'test-supplier-task7'")
    ).scalar_one()

    patched = client.patch(
        f"{ORDERS}/{order['id']}/quote",
        json={"supplierId": str(supplier_id)},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    audit = [
        entry
        for entry in patched.json()["auditLog"]
        if entry["action"] == "Правка КП: supplier"
    ]
    assert len(audit) == 1
    assert audit[0]["value"].endswith("ООО «Поставщик №7»")


@requires_db
def test_line_offers_readable_by_viewer(client):
    """Дополнение задачи №7: offers — чтение контура, доступно всем аутентифицированным."""

    headers = _auth(client)
    order = _create(client, headers)
    line_id = order["lines"][0]["id"]

    viewer = _auth(client, VIEWER)
    response = client.get(
        f"{ORDERS}/{order['id']}/lines/{line_id}/offers", headers=viewer
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["lineId"] == line_id
    assert body["offers"] == []  # в пустой базе предложений нет — честный пустой список

    # строка чужой заявки — 404
    assert (
        client.get(
            f"{ORDERS}/{order['id']}/lines/{uuid.uuid4()}/offers", headers=viewer
        ).status_code
        == 404
    )


@requires_db
def test_order_resolved_by_check_batch_id_and_events_logged(client):
    from app.matching.order_binding import ensure_request_for_batch

    headers = _auth(client)
    batch = _batch(client, headers)
    batch_id = batch["id"]

    async def scenario(session: AsyncSession):
        await ensure_request_for_batch(session, uuid.UUID(batch_id))
        await session.commit()

    _run(scenario)

    card = client.get(f"{ORDERS}/{batch_id}", headers=headers)
    assert card.status_code == 200, card.text
    body = card.json()
    assert body["checkBatchId"] == batch_id
    assert body["source"] == "check_batch"

    sent = client.post(f"{ORDERS}/{batch_id}/send", headers=headers)
    assert sent.status_code == 200
    confirmed = client.post(f"{ORDERS}/{batch_id}/confirm-quote", headers=headers)
    assert confirmed.status_code == 200

    card = client.get(f"{ORDERS}/{batch_id}", headers=headers).json()
    actions = [entry["action"] for entry in card["auditLog"]]
    assert "Отправлена на обработку" in actions
    assert "Подтверждено КП" in actions


@requires_db
def test_check_batch_creation_binds_order_sheet(client):
    """Шаг 5: расчётный лист создаётся вместе с заявкой (решение №1)."""

    headers = _auth(client)
    batch = _batch(client, headers)
    batch_id = batch["id"]

    card = client.get(f"{ORDERS}/{batch_id}", headers=headers)
    assert card.status_code == 200, card.text
    body = card.json()
    assert body["checkBatchId"] == batch_id
    assert body["source"] == "check_batch"
    # строки = позиции заявки (одна позиция в _batch)
    assert body["lines_count"] == 1


@requires_db
def test_order_card_lazy_binding_for_legacy_batches(client, db):
    """Шаг 5: заявки, созданные до связки, получают лист при чтении карточки."""

    headers = _auth(client)
    batch = _batch(client, headers)
    batch_id = batch["id"]

    # легаси-состояние: лист отвязан/отсутствует
    db.execute(
        text("DELETE FROM price_requests WHERE check_batch_id = :batch_id"),
        {"batch_id": batch_id},
    )
    db.commit()

    card = client.get(f"{ORDERS}/{batch_id}", headers=headers)
    assert card.status_code == 200, card.text
    body = card.json()
    assert body["checkBatchId"] == batch_id
    assert body["source"] == "check_batch"

    # воссозданный лист один: повторное чтение не плодит записи
    again = client.get(f"{ORDERS}/{batch_id}", headers=headers).json()
    assert again["id"] == body["id"]


@requires_db
def test_cancel_send_blocked_while_check_running(client, db):
    from app.matching.order_binding import ensure_request_for_batch

    headers = _auth(client)
    batch = _batch(client, headers)
    batch_id = batch["id"]
    item_id = batch["items"][0]["id"]

    async def scenario(session: AsyncSession):
        await ensure_request_for_batch(session, uuid.UUID(batch_id))
        await session.commit()

    _run(scenario)
    assert client.post(f"{ORDERS}/{batch_id}/send", headers=headers).status_code == 200

    # решение №2: прогон в очереди/исполнении блокирует отмену отправки
    db.execute(
        text(
            "INSERT INTO pipeline_runs (check_item_id, run_no, status, "
            " pipeline_definition_id) VALUES "
            "(:item_id, 1, 'RUNNING', "
            " (SELECT id FROM pipeline_definitions LIMIT 1))"
        ),
        {"item_id": item_id},
    )
    db.commit()

    blocked = client.post(f"{ORDERS}/{batch_id}/cancel-send", headers=headers)
    assert blocked.status_code == 409, blocked.text

    db.execute(
        text("UPDATE pipeline_runs SET status = 'COMPLETED' WHERE check_item_id = :item_id"),
        {"item_id": item_id},
    )
    db.commit()
    allowed = client.post(f"{ORDERS}/{batch_id}/cancel-send", headers=headers)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "draft"
