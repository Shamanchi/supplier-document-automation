"""Celery-точки входа интеграции с Bitrix24.

В задачу передаётся только идентификатор задания: полезная нагрузка не должна
нести состояние, иначе повторная доставка выполнялась бы по устаревшим данным.

Ретраев Celery здесь нет намеренно. Политика повторов описана контрактом и
живёт в `retry.py`: она учитывает `Retry-After` портала и различает
повторяемые и терминальные ошибки, чего механизм Celery не знает. Задача
всегда завершается успешно, а следующая попытка приходит от диспетчера.
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
from app.modules.integrations.bitrix.client import build_bitrix_client
from app.modules.integrations.bitrix.executor import BitrixJobExecutor, due_jobs
from app.modules.integrations.bitrix.queue import EVENT_TYPE, enqueue_bitrix_job
from app.modules.order_matching import models as matching_models  # noqa: F401
from app.modules.reporting import models as reporting_models  # noqa: F401
from app.modules.rules import models as rule_models  # noqa: F401
from app.modules.verdicts import models as verdict_models  # noqa: F401
from app.worker import celery_app

T = TypeVar("T")


@celery_app.task(name="app.bitrix.run_job", acks_late=True)
def run_bitrix_job(job_id: str) -> dict:
    """Выполняет одно задание интеграции."""
    outcome = _run(_execute(uuid.UUID(job_id)))
    return {
        "status": outcome.status,
        "attempt": outcome.attempt,
        "skipped": outcome.skipped,
    }


@celery_app.task(name="app.bitrix.dispatch_outbox")
def dispatch_bitrix_outbox() -> int:
    """Публикует committed-строки outbox; повторная доставка безопасна."""
    return _run(_dispatch())


@celery_app.task(name="app.bitrix.dispatch_retries")
def dispatch_bitrix_retries() -> int:
    """Возвращает в очередь задания, у которых наступило время повтора."""
    return _run(_dispatch_retries())


async def _execute(job_id: uuid.UUID):
    settings = get_settings()
    async with SessionLocal() as session:
        executor = BitrixJobExecutor(
            session, settings, S3Storage(settings), build_bitrix_client(settings)
        )
        return await executor.execute(job_id)


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
                "app.bitrix.run_job",
                args=[row.payload["jobId"]],
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


async def _dispatch_retries() -> int:
    """Запланированный повтор возвращается в очередь через тот же outbox.

    Прямая отправка в Celery была бы короче, но тогда повтор потерял бы
    транзакционную гарантию доставки, которая есть у первой попытки.
    """
    async with SessionLocal() as session:
        due = await due_jobs(session)
        for job_id, attempt in due:
            await enqueue_bitrix_job(session, job_id=job_id, attempt=attempt)
        await session.commit()
        return len(due)


def _run(awaitable: Awaitable[T]) -> T:
    async def execute() -> T:
        try:
            return await awaitable
        finally:
            # Задачи Celery идут через asyncio.run: соединения asyncpg из пула
            # не должны утечь в event loop следующей задачи.
            await engine.dispose()

    return asyncio.run(execute())
