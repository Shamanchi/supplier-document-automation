"""Заявки клиента: создание из текста, карточка, правка строк, статусы.

Жизненный цикл: `draft` -> `active` (send) -> `confirmation`
(confirm-quote) -> `closed` (close); `archived` — терминальное хранилище.

Задача №7 (docs/merge-remote/09-task7-kp-contour.md), шаги 2-3:
- аутентификация — канон identity (мутации: manager/admin/sysadmin, чтение —
  любой аутентифицированный), легаси-зависимости import_auth убраны;
- `{order_id}` понимает и id расчётного листа (ручные заявки P2), и id
  связанной заявки-check-batch — фронт оперирует именно check-batch id;
- недостающие ручки контракта `frontend/API.md`: cancel-send, archive,
  complete-check, confirm-quote, PATCH quote (журнал правок с причиной),
  POST files;
- события заявки пишутся в `order_events`, правки КП — в
  `quote_adjustments`; карточка дополнена полями вкладок «Подбор цен» и
  «КП» (файлы, позиции, итоги по поставщикам, версии, журнал).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.project_context import get_current_project
from app.api.calculations import _calculation_dict
from app.matching.calculations import CalculationService, LineInput
from app.matching.cascade import MatchService
from app.matching.order_binding import (
    ORDER_EVENT_ARCHIVED,
    ORDER_EVENT_CHECK_COMPLETED,
    ORDER_EVENT_QUOTE_CONFIRMED,
    ORDER_EVENT_SEND_CANCELLED,
    ORDER_EVENT_SENT,
    ensure_request_for_batch,
    record_order_event,
)
from app.matching.requests import parse_text
from app.matching.variants import VariantService, price_version_is_searchable
from app.models import (
    Calculation,
    CalculationItem,
    OrderEvent,
    OrderFile,
    PriceRequest,
    PriceRequestLine,
    PriceVersion,
    Project,
    QuoteAdjustment,
    Supplier,
    SupplierPriceItem,
    SupplierPriceValue,
)
from app.modules.checks.models import RUN_ACTIVE_STATUSES, CheckBatch, CheckItem, PipelineRun
from app.modules.identity.dependencies import CurrentUser, require_roles
from app.modules.identity.models import User as IdentityUser

router = APIRouter(prefix="/orders", tags=["orders"])

#: Мутации заявки — контур менеджера (решение №4 задачи №7).
OrderWriter = Annotated[IdentityUser, Depends(require_roles("manager", "admin", "sysadmin"))]

_STATUS_LABELS = {
    "draft": "Черновик",
    "active": "В работе",
    "confirmation": "На подтверждении",
    "closed": "Закрыта",
    "archived": "В архиве",
}
_STATUS_PROGRESS = {
    "draft": 0,
    "active": 20,
    "confirmation": 80,
    "closed": 100,
    "archived": 100,
}
#: Порядок жизненного цикла для степпера карточки.
_LIFECYCLE = ("draft", "active", "confirmation", "closed")
_READ_ONLY_STATUSES = ("closed", "archived")
_FILE_KINDS = ("specification", "passport", "price", "other")


class CreateOrderIn(BaseModel):
    """Тело заявки: свободный текст и/или реквизиты шапки.

    `raw_text` разбирается построчно; `title`/`client` могут заполнить шапку
    даже без текста (строки появятся позже, например из файла).
    """

    title: str = Field(default="", max_length=2000)
    client: str = Field(default="", max_length=2000)
    raw_text: str | None = Field(default=None, max_length=200_000)


class UpdateLineIn(BaseModel):
    """Ручная правка разобранной строки до запуска подбора."""

    query_name: str | None = Field(default=None, max_length=2000)
    quantity: float | None = Field(default=None, gt=0)
    unit_code: str | None = Field(default=None, max_length=32)


class UpdateQuoteIn(BaseModel):
    """Ручная правка КП: НДС, скидка, поставщик и причина.

    При изменении `vatRate`/`specialDiscount` причина обязательна
    (контракт `frontend/API.md`).
    """

    vatRate: float | None = Field(default=None, ge=0, le=100)
    specialDiscount: float | None = Field(default=None, ge=0, le=100)
    supplierId: uuid.UUID | None = None
    reason: str | None = Field(default=None, max_length=2000)


class OrderFileIn(BaseModel):
    """Регистрация файла заявки: имя и роль, бинарные данные — через /uploads."""

    fileName: str = Field(min_length=1, max_length=500)
    kind: str = Field(default="other", max_length=20)


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _money(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _order_number(order: PriceRequest) -> str:
    """Детерминированный номер из id: З-<8 hex>."""
    return f"З-{order.id.hex[:8]}"


def _line_dict(line: PriceRequestLine) -> dict[str, object]:
    return {
        "id": str(line.id),
        "ordinal": line.ordinal,
        "source_line": line.source_line,
        "query_name": line.query_name,
        "quantity": float(line.quantity) if line.quantity is not None else None,
        "unit_code": line.unit_code,
        "matched_canonical_id": (
            str(line.matched_canonical_id) if line.matched_canonical_id is not None else None
        ),
        "match_stage": line.match_stage,
        "match_score": float(line.match_score) if line.match_score is not None else None,
    }


async def _find_order(
    session: AsyncSession, order_id: uuid.UUID
) -> PriceRequest | None:
    """Заявка по id расчётного листа либо по id связанного check-batch.

    Фронт оперирует id заявки-пакета (check-batch), P2-сервис — id самого
    `price_requests`; роутер понимает оба.
    """
    order = await session.get(PriceRequest, order_id)
    if order is None:
        order = await session.scalar(
            select(PriceRequest).where(PriceRequest.check_batch_id == order_id)
        )
    return order


async def _require_order(
    session: AsyncSession, order_id: uuid.UUID
) -> PriceRequest:
    order = await _find_order(session, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="заявка не найдена")
    return order


async def _load_card(
    session: AsyncSession, order_id: uuid.UUID
) -> tuple[PriceRequest, list[PriceRequestLine]] | None:
    order = await _find_order(session, order_id)
    if order is None:
        return None
    lines = list(
        await session.scalars(
            select(PriceRequestLine)
            .where(PriceRequestLine.request_id == order.id)
            .order_by(PriceRequestLine.ordinal, PriceRequestLine.id)
        )
    )
    return order, lines


# --- данные вкладок «Подбор цен» / «КП» -------------------------------------


async def _quote_state(session: AsyncSession, order: PriceRequest) -> dict[str, str]:
    """Актуальные значения правок КП: последняя запись по каждому полю."""
    rows = await session.scalars(
        select(QuoteAdjustment)
        .where(QuoteAdjustment.request_id == order.id)
        .order_by(QuoteAdjustment.created_at, QuoteAdjustment.id)
    )
    state: dict[str, str] = {}
    for row in rows:
        state[row.field] = row.new_value or ""
    return state


async def _latest_calculation(session: AsyncSession, order: PriceRequest) -> Calculation | None:
    return await session.scalar(
        select(Calculation)
        .where(Calculation.request_id == order.id)
        .order_by(Calculation.created_at.desc(), Calculation.id.desc())
        .limit(1)
    )


def _calculation_item_map(calculation: Calculation | None) -> dict[str, CalculationItem]:
    """Строки расчёта по исходной строке заявки: `source_line` — общий ключ."""
    if calculation is None:
        return {}
    return {item.source_line: item for item in calculation.items if item.source_line}


async def _batch_suppliers(
    session: AsyncSession, order: PriceRequest
) -> dict[int, str]:
    """Заявленный поставщик по номеру позиции заявки.

    У заявки из Bitrix24 позиция может быть привязана к конкретному
    поставщику; тогда подбор цен ограничивается им. `ordinal` строки
    расчётного листа совпадает с `position_no` позиции заявки.
    """
    if order.check_batch_id is None:
        return {}
    rows = await session.execute(
        select(CheckItem.position_no, CheckItem.supplier).where(
            CheckItem.batch_id == order.check_batch_id
        )
    )
    return {
        int(position_no): (supplier or "").strip() for position_no, supplier in rows
    }


async def _price_positions(
    session: AsyncSession,
    order: PriceRequest,
    lines: list[PriceRequestLine],
    calculation: Calculation | None,
) -> list[dict[str, object]]:
    """Позиции вкладки «Подбор цен»: строка заявки -> цена активного прайса.

    Подбор идёт в контексте заявки: разовый счёт/КП другой заявки в выборку
    не попадает, а если у позиции указан поставщик — цены ограничиваются им.
    Данные не выдумываются: строка без канона и без цены остаётся со
    статусом `missing`, чтобы менеджер видел, что подбор не сделан.
    """
    item_by_line = _calculation_item_map(calculation)
    suppliers_by_position = await _batch_suppliers(session, order)
    variant_service: VariantService | None = None
    positions: list[dict[str, object]] = []
    for line in lines:
        item = item_by_line.get(line.source_line)
        if item is not None:
            positions.append(
                {
                    "id": str(item.id),
                    "product": item.name,
                    "unit": item.unit_code,
                    "supplier": item.supplier_name,
                    "price": _money(item.base_price),
                    "discount": _money(item.discount_percent),
                    "method": "manual" if item.manual_price is not None else "auto",
                    "status": "success",
                    "source": str(item.price_item_id) if item.price_item_id else "",
                    "selected": True,
                }
            )
            continue
        query = (line.query_name or "").strip()
        if not query:
            positions.append(
                {
                    "id": str(line.id),
                    "product": line.source_line,
                    "unit": line.unit_code,
                    "supplier": "",
                    "price": None,
                    "discount": 0.0,
                    "method": "auto",
                    "status": "missing",
                    "source": "",
                    "selected": False,
                }
            )
            continue
        if variant_service is None:
            variant_service = VariantService(session)
        _, variants = await variant_service.by_query(
            query,
            supplier=suppliers_by_position.get(line.ordinal, ""),
            check_batch_id=order.check_batch_id,
            limit=100,
        )
        offer = None
        product = query
        for variant in variants:
            product = variant.title or query
            offer = next((o for o in variant.offers if o.price is not None), None)
            if offer is not None:
                break
        if offer is None:
            positions.append(
                {
                    "id": str(line.id),
                    "product": product,
                    "unit": line.unit_code,
                    "supplier": "",
                    "price": None,
                    "discount": 0.0,
                    "method": "auto",
                    "status": "missing",
                    "source": "",
                    "selected": False,
                }
            )
            continue
        positions.append(
            {
                "id": offer.price_item_id,
                "product": offer.name or product,
                "unit": offer.unit,
                "supplier": offer.supplier,
                "price": _money(offer.price),
                "discount": 0.0,
                "method": "auto",
                "status": "success",
                "source": offer.price_version_id,
                "selected": False,
            }
        )
    return positions


def _quote_positions(calculation: Calculation | None) -> list[dict[str, object]]:
    if calculation is None:
        return []
    return [
        {
            "id": str(item.id),
            "product": item.name,
            "quantity": float(item.quantity),
            "unit": item.unit_code,
            "supplier": item.supplier_name,
            "price": _money(item.base_price),
            "discount": _money(item.discount_percent),
            "subtotal": _money(item.line_total_without_vat),
            "vat": _money(item.line_vat_amount),
            "total": _money(item.line_total_with_vat),
        }
        for item in calculation.items
    ]


def _supplier_totals(calculation: Calculation | None) -> list[dict[str, object]]:
    if calculation is None:
        return []
    amounts: dict[str, Decimal] = {}
    weight: dict[str, Decimal] = {}
    volume: dict[str, Decimal] = {}
    for item in calculation.items:
        name = item.supplier_name or "—"
        amounts[name] = amounts.get(name, Decimal(0)) + (
            item.line_total_without_vat
            if item.line_total_without_vat is not None
            else Decimal(0)
        )
        weight[name] = weight.get(name, Decimal(0)) + (
            item.weight_kg if item.weight_kg is not None else Decimal(0)
        )
        volume[name] = volume.get(name, Decimal(0)) + (
            item.volume_m3 if item.volume_m3 is not None else Decimal(0)
        )
    return [
        {
            "supplier": name,
            "amount": _money(amounts.get(name)),
            "weight": f"{weight.get(name, Decimal(0)):,.3f}",
            "volume": f"{volume.get(name, Decimal(0)):,.3f}",
        }
        for name in amounts
    ]


async def _order_files(session: AsyncSession, order: PriceRequest) -> list[dict[str, object]]:
    rows = await session.scalars(
        select(OrderFile)
        .where(OrderFile.request_id == order.id)
        .order_by(OrderFile.created_at, OrderFile.id)
    )
    return [
        {
            "id": str(row.id),
            "name": row.file_name,
            "kind": row.kind,
            "version": "1",
            "date": _iso(row.created_at),
            "progress": 0,
            "status": "pending",
        }
        for row in rows
    ]


async def _audit_log(session: AsyncSession, order: PriceRequest) -> list[dict[str, object]]:
    """Журнал: события заявки и правки КП в одной ленте, свежие сверху.

    Имена актёров разрешаются одним запросом: журнал без имён бесполезен
    менеджеру, а JOIN на каждую строку — лишние запросы.
    """
    entries: list[dict[str, object]] = []
    adjustments = list(
        await session.scalars(
            select(QuoteAdjustment)
            .where(QuoteAdjustment.request_id == order.id)
            .order_by(QuoteAdjustment.created_at, QuoteAdjustment.id)
        )
    )
    events: list[OrderEvent] = []
    if order.check_batch_id is not None:
        events = list(
            await session.scalars(
                select(OrderEvent)
                .where(OrderEvent.check_batch_id == order.check_batch_id)
                .order_by(OrderEvent.created_at, OrderEvent.id)
            )
        )
    actor_ids = {
        row.actor_id for row in adjustments if row.actor_id is not None
    } | {row.actor_id for row in events if row.actor_id is not None}
    names = {
        row.id: row.display_name
        for row in (
            await session.scalars(
                select(IdentityUser).where(IdentityUser.id.in_(actor_ids))
            )
        )
    } if actor_ids else {}

    _EVENT_ACTIONS = {
        ORDER_EVENT_SENT: "Отправлена на обработку",
        ORDER_EVENT_SEND_CANCELLED: "Отменена отправка",
        ORDER_EVENT_CHECK_COMPLETED: "Завершена проверка ТХ",
        ORDER_EVENT_QUOTE_CONFIRMED: "Подтверждено КП",
        ORDER_EVENT_ARCHIVED: "Перемещена в архив",
    }
    for row in adjustments:
        entries.append(
            {
                "time": _iso(row.created_at),
                "user": names.get(row.actor_id, "") if row.actor_id else "",
                "action": f"Правка КП: {row.field}",
                "value": f"{row.old_value or '—'} → {row.new_value or '—'}",
                "reason": row.reason,
            }
        )
    for row in events:
        entries.append(
            {
                "time": _iso(row.created_at),
                "user": names.get(row.actor_id, "") if row.actor_id else "",
                "action": _EVENT_ACTIONS.get(row.event_type, row.event_type),
                "value": "",
                "reason": (row.details or {}).get("comment", "") if row.details else "",
            }
        )
    entries.sort(key=lambda entry: entry["time"] or "", reverse=True)
    return entries


async def _card_dict(
    session: AsyncSession,
    order: PriceRequest,
    lines: list[PriceRequestLine],
) -> dict[str, object]:
    """Карточка заявки: шапка, строки и данные вкладок Фазы 2."""
    calculation = await _latest_calculation(session, order)
    quote_state = await _quote_state(session, order)
    calculations = list(
        await session.scalars(
            select(Calculation)
            .where(Calculation.request_id == order.id)
            .order_by(Calculation.created_at, Calculation.id)
        )
    )

    # Автоскидка — доля скидки в базовой сумме последнего расчёта.
    automatic_discount = 0.0
    if calculation is not None:
        base = sum(
            (item.base_price or Decimal(0)) * item.quantity for item in calculation.items
        )
        saved = sum(
            item.discount_amount or Decimal(0) for item in calculation.items
        )
        if base:
            automatic_discount = float(saved / base * 100)

    vat_rate = 0.0
    if "vat_rate" in quote_state:
        try:
            vat_rate = float(quote_state["vat_rate"])
        except ValueError:
            vat_rate = 0.0
    elif calculation is not None:
        for item in calculation.items:
            if item.vat_rate_percent is not None:
                vat_rate = float(item.vat_rate_percent)
                break

    special_discount = 0.0
    if "special_discount" in quote_state:
        try:
            special_discount = float(quote_state["special_discount"])
        except ValueError:
            special_discount = 0.0

    versions = [
        {
            "version": f"Р-{row.id.hex[:8]}",
            "date": _iso(row.created_at),
            "author": "",
            "amount": _money(row.total_amount),
            "status": "confirmed"
            if row.id == calculation.id and order.status == "confirmation"
            else "draft",
            "final": calculation is not None and row.id == calculation.id,
        }
        for row in calculations
    ]

    return {
        "id": str(order.id),
        "number": _order_number(order),
        "client": order.client,
        "source": order.source,
        "status": order.status,
        "statusLabel": _STATUS_LABELS.get(order.status, order.status),
        "progress": _STATUS_PROGRESS.get(order.status, 0),
        "date": _iso(order.created_at),
        "title": order.title,
        "lines_count": len(lines),
        "lines": [_line_dict(line) for line in lines],
        "checkBatchId": str(order.check_batch_id) if order.check_batch_id else None,
        # Поля вкладок Фазы 2 (контракт OrderDetail из frontend/API.md).
        "createdBy": "",
        "importedAt": _iso(order.created_at),
        "calculationVersion": (
            f"Р-{calculation.id.hex[:8]}" if calculation is not None else "—"
        ),
        "lifecycle": [_STATUS_LABELS[status] for status in _LIFECYCLE],
        "currentLifecycle": (
            _LIFECYCLE.index(order.status)
            if order.status in _LIFECYCLE
            else len(_LIFECYCLE) - 1
        ),
        "sent": order.status in ("active", "confirmation"),
        "readOnly": order.status in _READ_ONLY_STATUSES,
        "files": await _order_files(session, order),
        "pricePositions": await _price_positions(session, order, lines, calculation),
        "quotePositions": _quote_positions(calculation),
        "vatRate": vat_rate,
        "automaticDiscount": automatic_discount,
        "specialDiscount": special_discount,
        "discountApproval": "pending",
        "supplierTotals": _supplier_totals(calculation),
        "versions": versions,
        "auditLog": await _audit_log(session, order),
    }


def _summary_dict(order: PriceRequest, lines_count: int) -> dict[str, object]:
    return {
        "id": str(order.id),
        "number": _order_number(order),
        "client": order.client,
        "source": order.source,
        "status": order.status,
        "date": _iso(order.created_at),
        "lines_count": lines_count,
    }


def _event(
    session: AsyncSession, order: PriceRequest, event_type: str, user: IdentityUser
) -> None:
    """Событие заявки — только для заявок, связанных с check-batch."""
    if order.check_batch_id is not None:
        session.add(
            record_order_event(order.check_batch_id, event_type, actor_id=user.id)
        )


async def _transition(
    session: AsyncSession,
    order_id: uuid.UUID,
    *,
    allowed: tuple[str, ...],
    target: str,
    user: IdentityUser,
    event_type: str | None = None,
) -> dict[str, object]:
    order = await _require_order(session, order_id)
    if order.status not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"переход в статус «{target}» из статуса «{order.status}» невозможен",
        )
    order.status = target
    if event_type is not None:
        _event(session, order, event_type, user)
    await session.commit()
    card = await _load_card(session, order.id)
    assert card is not None
    return await _card_dict(session, *card)


@router.post("", status_code=201)
async def create_order(
    payload: CreateOrderIn,
    user: OrderWriter,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Создать заявку-черновик и разобрать `raw_text` на строки."""
    raw_text = (payload.raw_text or "").strip()
    parsed = parse_text(raw_text)
    has_lines = bool(parsed)
    has_header = bool((payload.title or "").strip() or (payload.client or "").strip())
    if not has_lines and not has_header:
        raise HTTPException(
            status_code=400,
            detail="пустая заявка: передайте raw_text, title или client",
        )
    order = PriceRequest(
        title=(payload.title or "").strip(),
        client=(payload.client or "").strip(),
        raw_text=raw_text or None,
        source="manual",
        status="draft",
    )
    session.add(order)
    await session.flush()
    order.number = _order_number(order)
    for parsed_line in parsed:
        session.add(
            PriceRequestLine(
                request_id=order.id,
                ordinal=parsed_line.ordinal,
                source_line=parsed_line.source_line,
                query_name=parsed_line.query_name,
                quantity=parsed_line.quantity,
                unit_code=parsed_line.unit_code,
                match_stage="none",
            )
        )
    await session.commit()
    card = await _load_card(session, order.id)
    assert card is not None
    return await _card_dict(session, *card)


@router.get("")
async def list_orders(
    user: CurrentUser,
    status: str | None = Query(default=None, description="all | draft | active | closed | archived"),
    search: str | None = Query(default=None, description="поиск по номеру или клиенту"),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, object]]:
    """Список заявок с количеством строк."""
    conditions = []
    status_value = (status or "").strip().lower()
    if status_value == "active":
        # «Активные» в контракте UI = черновики + в работе (+ confirmation).
        conditions.append(PriceRequest.status.in_(("draft", "active")))
    elif status_value in ("draft", "active", "confirmation", "closed", "archived"):
        conditions.append(PriceRequest.status == status_value)
    elif status_value not in ("", "all"):
        raise HTTPException(status_code=400, detail=f"неизвестный статус: {status_value}")
    if search and (needle := search.strip()):
        pattern = f"%{needle}%"
        conditions.append(
            or_(PriceRequest.number.ilike(pattern), PriceRequest.client.ilike(pattern))
        )
    lines_count = (
        select(func.count(PriceRequestLine.id))
        .where(PriceRequestLine.request_id == PriceRequest.id)
        .correlate(PriceRequest)
        .scalar_subquery()
    )
    rows = (
        await session.execute(
            select(PriceRequest, lines_count.label("lines_count"))
            .where(*conditions)
            .order_by(PriceRequest.created_at.desc(), PriceRequest.id.desc())
        )
    ).all()
    return [_summary_dict(order, count) for order, count in rows]


@router.get("/{order_id}")
async def get_order(
    order_id: uuid.UUID,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Полная карточка заявки со строками и данными вкладок Фазы 2."""
    card = await _load_card(session, order_id)
    if card is None and await session.get(CheckBatch, order_id) is not None:
        # Ленивая связка (решение №1): расчётный лист — производная заявки,
        # поэтому заводится при первом обращении к контуру Фазы 2. Так
        # оживают и заявки, созданные до задачи №7. Гонку параллельных
        # запросов (React StrictMode) снимает уникальность check_batch_id.
        try:
            await ensure_request_for_batch(session, order_id)
            await session.commit()
        except IntegrityError:
            await session.rollback()
        card = await _load_card(session, order_id)
    if card is None:
        raise HTTPException(status_code=404, detail="заявка не найдена")
    return await _card_dict(session, *card)


@router.patch("/{order_id}/lines/{line_id}")
async def update_line(
    order_id: uuid.UUID,
    line_id: uuid.UUID,
    payload: UpdateLineIn,
    user: OrderWriter,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Ручной маппинг строки: поправить наименование, количество или единицу."""
    order = await _require_order(session, order_id)
    if order.status != "draft":
        raise HTTPException(
            status_code=409,
            detail="правка строк доступна только в статусе «черновик»",
        )
    if payload.query_name is None and payload.quantity is None and payload.unit_code is None:
        raise HTTPException(
            status_code=400,
            detail="передайте хотя бы одно поле: query_name, quantity или unit_code",
        )
    line = await session.get(PriceRequestLine, line_id)
    if line is None or line.request_id != order.id:
        raise HTTPException(status_code=404, detail="строка не найдена")
    if payload.query_name is not None:
        query_name = payload.query_name.strip()
        if not query_name:
            raise HTTPException(status_code=400, detail="query_name не может быть пустым")
        line.query_name = query_name
    if payload.quantity is not None:
        line.quantity = Decimal(str(payload.quantity))
    if payload.unit_code is not None:
        unit_code = payload.unit_code.strip()
        line.unit_code = unit_code or None
    await session.commit()
    card = await _load_card(session, order.id)
    assert card is not None
    return await _card_dict(session, *card)


@router.get("/{order_id}/lines/{line_id}/offers")
async def get_line_offers(
    order_id: uuid.UUID,
    line_id: uuid.UUID,
    user: CurrentUser,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Предложения прайсов по строке заявки: поиск по имени позиции.

    Задача №7, дополнение (docs/merge-remote/10-task7-ui-price-picker.md):
    автоподбор вкладки «Подбор цен» ищет по дефисным обозначениям, которых в
    прайсах может не быть, а менеджеру нужен выбор по наименованию. Кандидаты —
    каскад `MatchService` (синоним → артикул → вектор → обозначение → fuzzy),
    дополненные полным рядом по обозначению; предложения поставщиков — по
    канонам из активных прайсов. Порядок — от лучшего совпадения к остальным.
    """
    order = await _require_order(session, order_id)
    line = await session.get(PriceRequestLine, line_id)
    if line is None or line.request_id != order.id:
        raise HTTPException(status_code=404, detail="строка не найдена")
    query = (line.query_name or line.source_line or "").strip()

    offers: list[dict[str, object]] = []
    if query:
        variant_service = VariantService(session)
        suppliers_by_position = await _batch_suppliers(session, order)
        supplier = suppliers_by_position.get(line.ordinal, "")
        match = await MatchService(session).match(
            query,
            limit=10,
            supplier=supplier,
            check_batch_id=order.check_batch_id,
        )
        _, variants = await variant_service.by_query(
            query,
            supplier=supplier,
            check_batch_id=order.check_batch_id,
        )
        variant_by_id = {variant.canonical_id: variant for variant in variants}

        # Ранг канона: сначала кандидаты каскада (стадия + балл), затем ряд по
        # обозначению, которого каскад мог не увидеть.
        ranked: dict[str, tuple[str, float]] = {}
        for candidate in match.candidates:
            ranked.setdefault(candidate.canonical_id, (candidate.stage, candidate.score))
        for variant in variants:
            ranked.setdefault(variant.canonical_id, ("designation", variant.relevance))

        missing_ids = [
            uuid.UUID(canonical_id)
            for canonical_id in ranked
            if canonical_id not in variant_by_id
        ]
        for variant in await variant_service.by_ids(
            missing_ids,
            supplier=supplier,
            check_batch_id=order.check_batch_id,
        ):
            variant_by_id[variant.canonical_id] = variant

        for canonical_id, (stage, score) in ranked.items():
            variant = variant_by_id.get(canonical_id)
            if variant is None:
                continue  # канон без предложений в активных прайсах
            for offer in variant.offers:
                offers.append(
                    {
                        "canonicalId": canonical_id,
                        "canonicalTitle": variant.title,
                        "priceItemId": offer.price_item_id,
                        "supplier": offer.supplier,
                        "name": offer.name,
                        "unit": offer.unit,
                        "price": _money(offer.price),
                        "currency": offer.currency,
                        "stage": stage,
                        "score": score,
                    }
                )

    return {"lineId": str(line.id), "query": query, "offers": offers}


@router.post("/{order_id}/send")
async def send_order(
    order_id: uuid.UUID,
    user: OrderWriter,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Отправить черновик на обработку: draft -> active."""
    return await _transition(
        session,
        order_id,
        allowed=("draft",),
        target="active",
        user=user,
        event_type=ORDER_EVENT_SENT,
    )


@router.post("/{order_id}/cancel-send")
async def cancel_send_order(
    order_id: uuid.UUID,
    user: OrderWriter,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Отменить отправку до начала обработки: active -> draft.

    Решение №2 открытых вопросов: если хотя бы один прогон позиций заявки
    уже поставлен в очередь или исполняется, отмена запрещена — она не
    должна молча ломать идущую проверку.
    """
    order = await _require_order(session, order_id)
    if order.status != "active":
        raise HTTPException(
            status_code=409,
            detail=f"отмена отправки из статуса «{order.status}» невозможна",
        )
    if order.check_batch_id is not None:
        active_runs = await session.scalar(
            select(func.count())
            .select_from(PipelineRun)
            .join(CheckItem, CheckItem.id == PipelineRun.check_item_id)
            .where(
                CheckItem.batch_id == order.check_batch_id,
                PipelineRun.status.in_(RUN_ACTIVE_STATUSES),
            )
        )
        if active_runs:
            raise HTTPException(
                status_code=409,
                detail="проверка уже запущена: отмените прогоны позиций или дождитесь завершения",
            )
    order.status = "draft"
    _event(session, order, ORDER_EVENT_SEND_CANCELLED, user)
    await session.commit()
    card = await _load_card(session, order.id)
    assert card is not None
    return await _card_dict(session, *card)


@router.post("/{order_id}/complete-check")
async def complete_check_order(
    order_id: uuid.UUID,
    user: OrderWriter,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Зафиксировать завершение проверки ТХ (событие в журнале заявки).

    Проверка живёт в контуре Фазы 3 на позициях check-batch, поэтому для
    ручной заявки P2 без связки действие бессмысленно.
    """
    order = await _require_order(session, order_id)
    if order.check_batch_id is None:
        raise HTTPException(
            status_code=400,
            detail="у ручной заявки нет проверки ТХ: событие доступно заявкам из пакета",
        )
    if order.status != "active":
        raise HTTPException(
            status_code=409,
            detail=f"завершение проверки из статуса «{order.status}» невозможно",
        )
    _event(session, order, ORDER_EVENT_CHECK_COMPLETED, user)
    await session.commit()
    card = await _load_card(session, order.id)
    assert card is not None
    return await _card_dict(session, *card)


@router.post("/{order_id}/confirm-quote")
async def confirm_quote_order(
    order_id: uuid.UUID,
    user: OrderWriter,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Подтвердить КП: active -> confirmation."""
    return await _transition(
        session,
        order_id,
        allowed=("active",),
        target="confirmation",
        user=user,
        event_type=ORDER_EVENT_QUOTE_CONFIRMED,
    )


@router.post("/{order_id}/close")
async def close_order(
    order_id: uuid.UUID,
    user: OrderWriter,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Закрыть заявку: draft/active/confirmation -> closed."""
    return await _transition(
        session, order_id, allowed=("draft", "active", "confirmation"), target="closed", user=user
    )


@router.post("/{order_id}/archive")
async def archive_order(
    order_id: uuid.UUID,
    user: OrderWriter,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Переместить заявку в архив из любого незакрытого статуса."""
    return await _transition(
        session,
        order_id,
        allowed=("draft", "active", "confirmation", "closed"),
        target="archived",
        user=user,
        event_type=ORDER_EVENT_ARCHIVED,
    )


@router.patch("/{order_id}/quote")
async def update_quote(
    order_id: uuid.UUID,
    payload: UpdateQuoteIn,
    user: OrderWriter,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Ручная правка КП: НДС, скидка, поставщик — с обязательной причиной.

    Решение №5 задачи №7: правки пишутся в журнал `quote_adjustments`
    (старое/новое значение, причина, кто); актуальное значение — последняя
    запись по полю. Применение правок к пересчёту — шаг 5 (обвязка фронта).
    """
    order = await _require_order(session, order_id)
    if order.status in _READ_ONLY_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"правка КП в статусе «{order.status}» недоступна",
        )

    changes: dict[str, str] = {}
    if payload.vatRate is not None:
        changes["vat_rate"] = f"{payload.vatRate:g}"
    if payload.specialDiscount is not None:
        changes["special_discount"] = f"{payload.specialDiscount:g}"
    if payload.supplierId is not None:
        supplier = await session.get(Supplier, payload.supplierId)
        if supplier is None:
            raise HTTPException(status_code=404, detail="поставщик не найден")
        changes["supplier"] = supplier.name
    if not changes:
        raise HTTPException(
            status_code=400,
            detail="передайте хотя бы одно поле: vatRate, specialDiscount или supplierId",
        )
    reason = (payload.reason or "").strip()
    if ("vat_rate" in changes or "special_discount" in changes) and not reason:
        raise HTTPException(
            status_code=400,
            detail="при ручном изменении НДС или скидки укажите причину",
        )

    current = await _quote_state(session, order)
    for field, new_value in changes.items():
        if current.get(field) == new_value:
            continue
        session.add(
            QuoteAdjustment(
                request_id=order.id,
                field=field,
                old_value=current.get(field),
                new_value=new_value,
                reason=reason,
                actor_id=user.id,
            )
        )
    await session.flush()

    quote_state = await _quote_state(session, order)
    special_discount = None
    if "special_discount" in quote_state:
        try:
            special_discount = Decimal(str(quote_state["special_discount"]))
        except Exception:
            special_discount = None

    calculation = await _latest_calculation(session, order)
    if calculation is not None:
        service = CalculationService(session)
        await service.recalculate(
            calculation,
            special_discount_percent=special_discount,
        )

    await session.commit()
    card = await _load_card(session, order.id)
    assert card is not None
    return await _card_dict(session, *card)


@router.post("/{order_id}/files")
async def add_order_file(
    order_id: uuid.UUID,
    payload: OrderFileIn,
    user: OrderWriter,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Зарегистрировать файл заявки: имя и роль.

    Бинарные данные загружаются каноническим `POST /uploads`; здесь заявка
    запоминает файл для карточки (контракт `frontend/API.md`).
    """
    order = await _require_order(session, order_id)
    if order.status in _READ_ONLY_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"добавление файлов в статусе «{order.status}» недоступно",
        )
    kind = (payload.kind or "other").strip().lower()
    if kind not in _FILE_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"неизвестный тип файла: {kind}; допустимо {', '.join(_FILE_KINDS)}",
        )
    session.add(
        OrderFile(
            request_id=order.id,
            file_name=payload.fileName.strip(),
            kind=kind,
        )
    )
   
    await session.commit()

    card = await _load_card(session, order.id)
    assert card is not None
    return await _card_dict(session, *card)


class QuoteSelection(BaseModel):
    """Переопределение выбора исполнения для строки заявки перед расчётом КП."""

    line_id: uuid.UUID
    canonical_id: uuid.UUID | None = None
    price_item_id: uuid.UUID | None = None
    quantity: float | None = Field(default=None, gt=0)


class QuoteSelectionsIn(BaseModel):
    """Тело запроса на сборку КП: необязательные переопределения по строкам."""

    selections: list[QuoteSelection] = Field(default_factory=list)


def _quote_skip(line: PriceRequestLine, reason: str) -> dict[str, object]:
    return {
        "line_id": str(line.id),
        "ordinal": line.ordinal,
        "source_line": line.source_line,
        "reason": reason,
    }


async def _offer_price_item_id(
    service: VariantService,
    line: PriceRequestLine,
    canonical_id: uuid.UUID,
    *,
    check_batch_id: uuid.UUID | None = None,
    supplier: str = "",
) -> uuid.UUID | None:
    """Позиция прайса для канона строки: первый оффер с ценой в активном прайсе.

    Подбор идёт в контексте заявки и с учётом заявленного поставщика: разовый
    счёт/КП чужой заявки не подставляется, а при указанном поставщике цены
    других поставщиков не рассматриваются.
    """
    query = (line.query_name or "").strip()
    if not query:
        return None
    _, rows = await service.by_query(
        query, supplier=supplier, check_batch_id=check_batch_id, limit=100
    )
    for variant in rows:
        if uuid.UUID(variant.canonical_id) != canonical_id:
            continue
        for offer in variant.offers:
            if offer.price is None:
                continue
            return uuid.UUID(offer.price_item_id)
        return None
    return None


async def _has_active_price(
    session: AsyncSession,
    price_item_id: uuid.UUID,
    *,
    check_batch_id: uuid.UUID | None = None,
) -> bool:
    """Есть ли цена у позиции активного прайса (для ручного переопределения).

    Разовый счёт/КП проходит проверку только в своей заявке: вне её он не
    является действующей ценой, даже если версия ещё не снята с публикации.
    """
    item = await session.get(SupplierPriceItem, price_item_id)
    if item is None:
        return False
    version = await session.get(PriceVersion, item.price_version_id)
    if version is None or not price_version_is_searchable(version, check_batch_id):
        return False
    return (
        await session.scalar(
            select(SupplierPriceValue.id)
            .where(SupplierPriceValue.item_id == price_item_id)
            .limit(1)
        )
    ) is not None


@router.post("/{order_id}/quote")
async def create_quote_from_order(
    order_id: uuid.UUID,
    user: OrderWriter,
    payload: QuoteSelectionsIn | None = None,
    session: AsyncSession = Depends(get_session),
    project: Project = Depends(get_current_project),
) -> dict[str, object]:
    """Собрать КП из подобранной заявки: канон -> цена активного прайса -> расчёт.

    Строки без подобранного канона и без цены в активных прайсах не роняют
    расчёт: они пропускаются и перечисляются в `skipped`, чтобы менеджер видел,
    что именно не вошло в КП и почему.
    """
    card = await _load_card(session, order_id)
    if card is None:
        raise HTTPException(status_code=404, detail="заявка не найдена")
    order, lines = card

    selections = {
        entry.line_id: entry for entry in (payload.selections if payload is not None else [])
    }
    suppliers_by_position = await _batch_suppliers(session, order)
    variant_service = VariantService(session)
    quote_lines: list[LineInput] = []
    skipped: list[dict[str, object]] = []

    for line in lines:
        selection = selections.get(line.id)
        if selection is not None and selection.quantity is not None:
            quantity = Decimal(str(selection.quantity))
        elif line.quantity is not None:
            quantity = Decimal(str(line.quantity))
        else:
            quantity = Decimal("1")
        canonical_id = (
            selection.canonical_id
            if selection is not None and selection.canonical_id is not None
            else line.matched_canonical_id
        )
        price_item_id = (
            selection.price_item_id
            if selection is not None and selection.price_item_id is not None
            else None
        )
        if canonical_id is None and price_item_id is None:
            skipped.append(_quote_skip(line, "строка не подобрана: нет канона"))
            continue
        if price_item_id is None:
            price_item_id = await _offer_price_item_id(
                variant_service,
                line,
                canonical_id,
                check_batch_id=order.check_batch_id,
                supplier=suppliers_by_position.get(line.ordinal, ""),
            )
            if price_item_id is None:
                skipped.append(
                    _quote_skip(line, "нет цены в активных прайсах по подобранному канону")
                )
                continue
        elif not await _has_active_price(
            session, price_item_id, check_batch_id=order.check_batch_id
        ):
            skipped.append(
                _quote_skip(line, "нет цены в активных прайсах по выбранной позиции")
            )
            continue

        quote_lines.append(
            LineInput(
                price_item_id=price_item_id,
                quantity=quantity,
                source_line=line.source_line,
                canonical_id=canonical_id,
            )
        )

    service = CalculationService(session)
    calculation = await service.create(
        project_id=project.id,
        lines=quote_lines,
        title=order.title or f"КП по заявке {order.number or order.id.hex[:8]}",
        request_id=order.id,
        check_batch_id=order.check_batch_id,
    )
    quote_state = await _quote_state(session, order)
    special_discount = None
    if "special_discount" in quote_state:
        try:
            special_discount = Decimal(str(quote_state["special_discount"]))
        except Exception:
            special_discount = None

    totals = await service.recalculate(
        calculation,
        special_discount_percent=special_discount,
    )
    await session.commit()
    payload_out = _calculation_dict(calculation, totals)
    payload_out["request_id"] = str(order.id)
    payload_out["skipped"] = skipped
    return payload_out
