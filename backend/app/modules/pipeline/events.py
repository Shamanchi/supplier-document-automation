"""Запись событий прогона и их представление в SSE.

Событие пишется в той же транзакции, что и смена состояния этапа: иначе клиент
мог бы увидеть событие, которого ещё нет в базе, или наоборот — состояние без
события, и `Last-Event-ID` перестал бы быть надёжным курсором.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.pipeline.models import PipelineEvent

#: Типы событий контракта (check-items §SSE).
STAGE_CHANGED = "pipeline.stage_changed.v1"
RUN_COMPLETED = "pipeline.run_completed.v1"
RUN_FAILED = "pipeline.run_failed.v1"
RUN_CANCELLED = "pipeline.run_cancelled.v1"

#: Типы, после которых поток закрывается: прогон завершён, ждать больше нечего.
TERMINAL_EVENTS = frozenset({RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED})


@dataclass(frozen=True)
class Progress:
    current: int
    total: int
    unit: str = "PAGE"


async def record_event(
    session: AsyncSession,
    *,
    check_item_id: uuid.UUID,
    pipeline_run_id: uuid.UUID,
    event_type: str,
    stage: str | None = None,
    status: str | None = None,
    progress: Progress | None = None,
    payload: dict | None = None,
) -> PipelineEvent:
    """Добавляет событие в журнал; commit — на вызывающем."""
    event = PipelineEvent(
        check_item_id=check_item_id,
        pipeline_run_id=pipeline_run_id,
        event_type=event_type,
        stage=stage,
        status=status,
        progress_current=progress.current if progress else None,
        progress_total=progress.total if progress else None,
        progress_unit=progress.unit if progress else None,
        payload=payload or {},
    )
    session.add(event)
    await session.flush()
    return event


def render_event(event: PipelineEvent) -> str:
    """Одно событие в формате text/event-stream.

    Поля `id`/`event`/`data` — ровно те, что в примере контракта; клиент
    восстанавливает поток, присылая последний `id` в `Last-Event-ID`.
    """
    data = {
        "checkItemId": str(event.check_item_id),
        "pipelineRunId": str(event.pipeline_run_id),
        "stage": event.stage,
        "status": event.status,
        "occurredAt": event.occurred_at.isoformat().replace("+00:00", "Z"),
        **event.payload,
    }
    if event.progress_current is not None and event.progress_total is not None:
        data["progress"] = {
            "current": event.progress_current,
            "total": event.progress_total,
            "unit": event.progress_unit,
        }
    body = json.dumps(data, ensure_ascii=False)
    return f"id: {event.id}\nevent: {event.event_type}\ndata: {body}\n\n"
