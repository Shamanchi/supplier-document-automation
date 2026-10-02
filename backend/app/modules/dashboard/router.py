"""Модуль `dashboard` (docs/api/dashboard/README.md).

Read-only агрегат стартового экрана: три KPI и срез последних заявок.
Собственных сущностей не создаёт — читает `check_batches` и связанные таблицы,
владельцем которых остаётся модуль checks.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import Field
from sqlalchemy import func, select

from app.core.schemas import BaseSchema
from app.modules.checks.models import (
    STATUS_TO_API,
    CheckBatch,
    CheckItem,
)
from app.modules.identity.dependencies import CurrentUser, SessionDep
from app.modules.verdicts.models import ItemVerdict

router = APIRouter(tags=["dashboard"])

DEFAULT_WINDOW_DAYS = 7
DEFAULT_RECENT_LIMIT = 5
MAX_RECENT_LIMIT = 20

#: Подписи укрупнённых статусов. Само отображение статусов принадлежит модулю
#: checks (владельцу сущности), здесь — только текст для экрана.
STATUS_LABELS: dict[str, str] = {
    "DRAFT": "Черновик",
    "ACTIVE": "В работе",
    "CONFIRMATION": "На подтверждении",
    "CLOSED": "Завершена",
    "ARCHIVED": "В архиве",
}

_ACTIVE_DB_STATUSES = ("QUEUED", "RUNNING", "REVIEW_REQUIRED")
_DONE_DB_STATUSES = ("COMPLETED", "CANCELLED", "ARCHIVED")


class Period(BaseSchema):
    # `from` — ключевое слово Python, поэтому поле названо from_, а наружу
    # уходит под явным алиасом. Он важнее alias_generator: тот дал бы "from_".
    from_: date = Field(alias="from")
    to: date
    days: int


class Kpi(BaseSchema):
    id: Literal["all", "active", "done"]
    label: str
    value: int
    detail: str
    tone: Literal["success", "warning", "default"]


class BatchSummary(BaseSchema):
    id: UUID
    number: str
    customer: str
    status: str
    status_label: str
    progress: int
    created_at: datetime


class DashboardResponse(BaseSchema):
    period: Period
    kpis: list[Kpi]
    recent_check_batches: list[BatchSummary]


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(
    session: SessionDep,
    user: CurrentUser,
    window_days: Annotated[int, Query(ge=1, le=365)] = DEFAULT_WINDOW_DAYS,
    recent_limit: Annotated[int, Query(ge=1, le=MAX_RECENT_LIMIT)] = DEFAULT_RECENT_LIMIT,
) -> DashboardResponse:
    now = datetime.now(UTC)
    window_start = now - timedelta(days=window_days)
    previous_start = window_start - timedelta(days=window_days)

    counts = await _status_counts(session, window_start, now)
    previous_total = await _total_in(session, previous_start, window_start)

    total = sum(counts.values())
    active = sum(counts.get(s, 0) for s in _ACTIVE_DB_STATUSES)
    done = sum(counts.get(s, 0) for s in _DONE_DB_STATUSES)
    review_required = counts.get("REVIEW_REQUIRED", 0)
    approved = await _approved_items(session, window_start, now)

    return DashboardResponse(
        period=Period(
            from_=window_start.date(),
            to=now.date(),
            days=window_days,
        ),
        kpis=[
            Kpi(
                id="all",
                label="Всего заявок",
                value=total,
                detail=_trend(total, previous_total),
                tone="success",
            ),
            Kpi(
                id="active",
                label="В работе",
                value=active,
                detail=_plural_review(review_required),
                tone="warning",
            ),
            Kpi(
                id="done",
                label="Готово",
                value=done,
                detail=f"{approved} подтверждены",
                tone="success",
            ),
        ],
        recent_check_batches=await _recent(session, recent_limit),
    )


async def _status_counts(session, start: datetime, end: datetime) -> dict[str, int]:
    rows = await session.execute(
        select(CheckBatch.status, func.count())
        .where(CheckBatch.created_at >= start, CheckBatch.created_at <= end)
        .group_by(CheckBatch.status)
    )
    return {status: count for status, count in rows.all()}


async def _total_in(session, start: datetime, end: datetime) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(CheckBatch)
            .where(CheckBatch.created_at >= start, CheckBatch.created_at < end)
        )
    ) or 0


async def _approved_items(session, start: datetime, end: datetime) -> int:
    """Сколько позиций подтверждено менеджером за окно."""
    return (
        await session.scalar(
            select(func.count())
            .select_from(ItemVerdict)
            .join(CheckItem, CheckItem.id == ItemVerdict.check_item_id)
            .join(CheckBatch, CheckBatch.id == CheckItem.batch_id)
            .where(
                CheckBatch.created_at >= start,
                CheckBatch.created_at <= end,
                ItemVerdict.manager_verdict != "PENDING",
            )
        )
    ) or 0


async def _recent(session, limit: int) -> list[BatchSummary]:
    batches = (
        await session.scalars(
            select(CheckBatch).order_by(CheckBatch.created_at.desc()).limit(limit)
        )
    ).all()
    if not batches:
        return []

    progress = await _progress_by_batch(session, [b.id for b in batches])
    summaries = []
    for batch in batches:
        api_status = STATUS_TO_API.get(batch.status, "DRAFT")
        summaries.append(
            BatchSummary(
                id=batch.id,
                number=batch.title or f"З-{str(batch.id)[:8]}",
                customer=batch.customer or "—",
                status=api_status,
                status_label=STATUS_LABELS[api_status],
                progress=progress.get(batch.id, 0),
                created_at=batch.created_at,
            )
        )
    return summaries


async def _progress_by_batch(session, batch_ids: list[UUID]) -> dict[UUID, int]:
    """Доля завершённых позиций пакета в процентах."""
    rows = await session.execute(
        select(
            CheckItem.batch_id,
            func.count(),
            func.count().filter(CheckItem.status.in_(("COMPLETED", "CANCELLED"))),
        )
        .where(CheckItem.batch_id.in_(batch_ids))
        .group_by(CheckItem.batch_id)
    )
    return {
        batch_id: round(finished * 100 / total) if total else 0
        for batch_id, total, finished in rows.all()
    }


def _trend(current: int, previous: int) -> str:
    if previous == 0:
        return "нет данных за прошлый период" if current == 0 else "первые заявки за период"
    change = round((current - previous) * 100 / previous)
    sign = "+" if change > 0 else ""
    return f"{sign}{change}% к прошлому периоду"


def _plural_review(count: int) -> str:
    if count == 0:
        return "нет заявок на проверке"
    tail = count % 10
    if tail == 1 and count % 100 != 11:
        return f"{count} требует проверки"
    return f"{count} требуют проверки"
