"""Admin-only lifecycle for external price-import service identities."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthUser, require_admin
from app.db import get_session
from app.import_auth import generate_service_api_key, webhook_secret_for_client
from app.models import ServiceClient
from app.schemas import ServiceClientCreate, ServiceClientOut, ServiceClientSecretOut


router = APIRouter(tags=["service-clients"])


@router.get("/service-clients", response_model=list[ServiceClientOut])
async def list_service_clients(
    _admin: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[ServiceClientOut]:
    rows = (
        await session.execute(
            select(ServiceClient)
            .where(ServiceClient.is_internal.is_(False))
            .order_by(ServiceClient.created_at)
        )
    ).scalars().all()
    return [ServiceClientOut.model_validate(row) for row in rows]


@router.post("/service-clients", response_model=ServiceClientSecretOut, status_code=201)
async def create_service_client(
    payload: ServiceClientCreate,
    _admin: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ServiceClientSecretOut:
    raw_key, prefix, digest = generate_service_api_key()
    client = ServiceClient(
        name=payload.name,
        key_prefix=prefix,
        key_digest=digest,
        requests_per_minute=payload.requests_per_minute,
        max_concurrent_imports=payload.max_concurrent_imports,
        max_input_tokens_per_import=payload.max_input_tokens_per_import,
        max_output_tokens_per_import=payload.max_output_tokens_per_import,
    )
    session.add(client)
    await session.commit()
    return ServiceClientSecretOut(
        client=ServiceClientOut.model_validate(client),
        api_key=raw_key,
        webhook_secret=webhook_secret_for_client(client.id),
    )


async def _get_client(session: AsyncSession, client_id: uuid.UUID) -> ServiceClient:
    client = await session.get(ServiceClient, client_id)
    if client is None or client.is_internal:
        raise HTTPException(status_code=404, detail="Service client не найден")
    return client


@router.post("/service-clients/{client_id}/rotate", response_model=ServiceClientSecretOut)
async def rotate_service_client(
    client_id: uuid.UUID,
    _admin: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ServiceClientSecretOut:
    client = await _get_client(session, client_id)
    raw_key, prefix, digest = generate_service_api_key()
    client.key_prefix = prefix
    client.key_digest = digest
    client.is_active = True
    await session.commit()
    return ServiceClientSecretOut(
        client=ServiceClientOut.model_validate(client),
        api_key=raw_key,
        webhook_secret=webhook_secret_for_client(client.id),
    )


@router.post("/service-clients/{client_id}/revoke", response_model=ServiceClientOut)
async def revoke_service_client(
    client_id: uuid.UUID,
    _admin: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ServiceClientOut:
    client = await _get_client(session, client_id)
    client.is_active = False
    await session.commit()
    return ServiceClientOut.model_validate(client)
