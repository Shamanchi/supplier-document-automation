"""Постановка прогона в очередь через транзакционный outbox.

Приём тот же, что у извлечения сертификатов: строка outbox пишется в одной
транзакции с созданием прогона, поэтому задача не может появиться раньше самого
прогона, а откат запроса не оставляет висящей задачи. Диспетчер публикует
committed-строки в Celery; повторная доставка безопасна — исполнитель проверяет
состояние прогона.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: Версия исполнителя; входит в ключ идемпотентности, чтобы смена версии могла
#: потребовать новой доставки для того же прогона.
EXECUTOR_VERSION = "pipeline-exec-v1"
EVENT_TYPE = "pipeline.run.requested.v1"


def run_key(pipeline_run_id: uuid.UUID) -> str:
    return f"pipeline-run:{pipeline_run_id}:{EXECUTOR_VERSION}"


async def enqueue_pipeline_run(
    session: AsyncSession,
    *,
    check_item_id: uuid.UUID,
    pipeline_run_id: uuid.UUID,
) -> None:
    """Пишет outbox-событие в транзакции создания прогона."""
    payload = {
        "checkItemId": str(check_item_id),
        "pipelineRunId": str(pipeline_run_id),
    }
    await session.execute(
        text(
            "INSERT INTO integration_outbox "
            "(aggregate_type, aggregate_id, event_type, payload, idempotency_key) "
            "VALUES ('CHECK_ITEM', :item_id, :event_type, "
            "CAST(:payload AS jsonb), :key) "
            "ON CONFLICT (idempotency_key) DO NOTHING"
        ),
        {
            "item_id": check_item_id,
            "event_type": EVENT_TYPE,
            "payload": json.dumps(payload),
            "key": run_key(pipeline_run_id),
        },
    )
