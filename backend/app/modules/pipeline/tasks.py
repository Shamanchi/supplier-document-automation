"""Celery-точки входа исполнения прогонов.

В задачу передаются только идентификаторы: полезная нагрузка не должна нести
состояние, иначе повторная доставка выполнялась бы по устаревшим данным.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable
from typing import TypeVar

from sqlalchemy import text

from app.config import get_settings
from app.db import SessionLocal, engine
from app.modules.catalogs import models as catalog_models  # noqa: F401
from app.modules.certificates import models as certificate_models  # noqa: F401
from app.modules.checks import models as check_models  # noqa: F401
from app.modules.documents.storage import S3Storage
from app.modules.identity import models as identity_models  # noqa: F401
from app.modules.order_matching import models as matching_models  # noqa: F401
from app.modules.pipeline.queue import EVENT_TYPE
from app.modules.pipeline.service import PipelineExecutor
from app.modules.reporting import models as reporting_models  # noqa: F401
from app.modules.rules import models as rule_models  # noqa: F401
from app.modules.verdicts import models as verdict_models  # noqa: F401
from app.worker import celery_app

T = TypeVar("T")


@celery_app.task(
    bind=True,
    name="app.pipeline.run",
    max_retries=3,
    acks_late=True,
)
def run_pipeline(self, pipeline_run_id: str) -> dict:
    """Исполняет один прогон позиции."""
    token = str(self.request.id or uuid.uuid4())
    try:
        outcome = _run(_execute(uuid.UUID(pipeline_run_id), token))
    except Exception as exc:
        countdown = min(60, 2 ** (self.request.retries + 1))
        raise self.retry(exc=exc, countdown=countdown) from exc
    return {
        "status": outcome.status,
        "runId": str(outcome.run_id) if outcome.run_id else None,
        "parameters": outcome.parameters,
    }


@celery_app.task(name="app.pipeline.dispatch_outbox")
def dispatch_pipeline_outbox() -> int:
    """Публикует committed-строки outbox; повторная доставка безопасна."""
    return _run(_dispatch())


async def _execute(pipeline_run_id: uuid.UUID, token: str):
    settings = get_settings()
    async with SessionLocal() as session:
        executor = PipelineExecutor(session, settings, S3Storage(settings))
        return await executor.execute(pipeline_run_id, worker_token=token)


async def _dispatch() -> int:
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, payload FROM integration_outbox "
                    "WHERE event_type = :event_type AND published_at IS NULL "
                    "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 50"
                ),
                {"event_type": EVENT_TYPE},
            )
        ).all()
        for row in rows:
            celery_app.send_task(
                "app.pipeline.run",
                args=[row.payload["pipelineRunId"]],
                task_id=str(uuid.uuid4()),
            )
            await session.execute(
                text(
                    "UPDATE integration_outbox SET published_at = now() WHERE id = :id"
                ),
                {"id": row.id},
            )
        await session.commit()
        return len(rows)


def _run(awaitable: Awaitable[T]) -> T:
    async def execute() -> T:
        try:
            return await awaitable
        finally:
            # Задачи Celery идут через asyncio.run: соединения asyncpg из пула
            # не должны утечь в event loop следующей задачи.
            await engine.dispose()

    return asyncio.run(execute())
