"""Постановка задания интеграции в очередь через транзакционный outbox.

Тот же приём, что у прогонов и извлечения сертификатов: строка outbox пишется в
одной транзакции с созданием задания, поэтому задача не может появиться раньше
самого задания, а откат запроса не оставляет висящей задачи.

Ключ идемпотентности включает номер попытки: ручной retry и автоматический
повтор — это новые доставки того же задания, и без номера попытки outbox
отбросил бы их как дубликат.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

EXECUTOR_VERSION = "bitrix-exec-v1"
EVENT_TYPE = "integration.bitrix.job.requested.v1"


def job_key(job_id: uuid.UUID, attempt: int) -> str:
    return f"bitrix-job:{job_id}:attempt:{attempt}:{EXECUTOR_VERSION}"


async def enqueue_bitrix_job(
    session: AsyncSession, *, job_id: uuid.UUID, attempt: int = 0
) -> None:
    """Пишет outbox-событие в транзакции создания или повтора задания."""
    await session.execute(
        text(
            "INSERT INTO integration_outbox "
            "(aggregate_type, aggregate_id, event_type, payload, idempotency_key) "
            "VALUES ('INTEGRATION_JOB', :job_id, :event_type, "
            "CAST(:payload AS jsonb), :key) "
            "ON CONFLICT (idempotency_key) DO NOTHING"
        ),
        {
            "job_id": job_id,
            "event_type": EVENT_TYPE,
            "payload": json.dumps({"jobId": str(job_id), "attempt": attempt}),
            "key": job_key(job_id, attempt),
        },
    )
