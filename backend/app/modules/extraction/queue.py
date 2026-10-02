"""Transactional enqueue operation for certificate extraction."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

PIPELINE_VERSION = "native-v1"
EVENT_TYPE = "certificate.extraction.requested.v1"


def extraction_key(certificate_id: uuid.UUID, document_version_id: uuid.UUID) -> str:
    return (
        f"certificate:{certificate_id}:version:{document_version_id}:{PIPELINE_VERSION}"
    )


async def enqueue_certificate_extraction(
    session: AsyncSession,
    *,
    certificate_id: uuid.UUID,
    document_version_id: uuid.UUID,
    run_id: uuid.UUID | None = None,
    event_key: str | None = None,
) -> None:
    """Write an outbox event in the same transaction as the certificate."""
    key = event_key or extraction_key(certificate_id, document_version_id)
    payload = {
        "certificateId": str(certificate_id),
        "documentVersionId": str(document_version_id),
    }
    if run_id is not None:
        payload["runId"] = str(run_id)
    await session.execute(
        text(
            "INSERT INTO integration_outbox "
            "(aggregate_type, aggregate_id, event_type, payload, idempotency_key) "
            "VALUES ('CERTIFICATE', :certificate_id, :event_type, "
            "CAST(:payload AS jsonb), :key) "
            "ON CONFLICT (idempotency_key) DO NOTHING"
        ),
        {
            "certificate_id": certificate_id,
            "event_type": EVENT_TYPE,
            "payload": json.dumps(payload),
            "key": key,
        },
    )
