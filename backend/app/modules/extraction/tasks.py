"""Celery entrypoints for durable certificate extraction."""

from __future__ import annotations

import asyncio
import uuid
from typing import Awaitable, TypeVar

from sqlalchemy import text

from app.config import get_settings
from app.db import SessionLocal, engine
from app.modules.catalogs import models as catalog_models  # noqa: F401
from app.modules.checks import models as check_models  # noqa: F401
from app.modules.identity import models as identity_models  # noqa: F401
from app.modules.documents.storage import S3Storage
from app.modules.extraction.queue import EVENT_TYPE
from app.modules.extraction.service import CertificateExtractionService
from app.worker import celery_app

T = TypeVar("T")


@celery_app.task(
    bind=True,
    name="app.certificates.extract",
    max_retries=3,
    acks_late=True,
)
def extract_certificate(self, certificate_id: str, run_id: str | None = None) -> dict:
    """Process one certificate; Celery payload contains IDs only."""
    token = str(self.request.id or uuid.uuid4())
    try:
        outcome = _run(
            _extract(
                uuid.UUID(certificate_id),
                token,
                uuid.UUID(run_id) if run_id else None,
            )
        )
    except Exception as exc:
        countdown = min(60, 2 ** (self.request.retries + 1))
        raise self.retry(exc=exc, countdown=countdown) from exc
    return {
        "status": outcome.status,
        "runId": str(outcome.run_id) if outcome.run_id else None,
        "parameters": outcome.parameters,
    }


@celery_app.task(name="app.certificates.dispatch_outbox")
def dispatch_certificate_outbox() -> int:
    """Publish committed outbox rows; duplicate delivery remains safe."""
    return _run(_dispatch())


async def _extract(certificate_id: uuid.UUID, token: str, run_id: uuid.UUID | None):
    settings = get_settings()
    async with SessionLocal() as session:
        service = CertificateExtractionService(session, settings, S3Storage(settings))
        return await service.process(
            certificate_id, worker_token=token, requested_run_id=run_id
        )


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
            payload = row.payload
            task_id = str(uuid.uuid4())
            celery_app.send_task(
                "app.certificates.extract",
                args=[payload["certificateId"], payload.get("runId")],
                task_id=task_id,
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
            # Celery tasks use asyncio.run; pooled asyncpg connections must not
            # leak into the event loop created for the next task.
            await engine.dispose()

    return asyncio.run(execute())
