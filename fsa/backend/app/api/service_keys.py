"""Управление сервисными ключами: список, выпуск, отзыв.

Сам ключ показывается ровно один раз — в ответе на выпуск. В базе его нет, и
восстановить его нельзя: потерянный ключ отзывается и выпускается заново. Это
неудобно один раз и защищает постоянно.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import ServiceClient
from app.service_keys import ALL_SCOPES, ServiceKeyError, ServiceKeyService

router = APIRouter(prefix="/v1/service-keys", tags=["service-keys"])


def _client_dict(client: ServiceClient) -> dict[str, object]:
    return {
        "id": str(client.id),
        "name": client.name,
        "key_prefix": client.key_prefix,
        "scopes": client.scopes.split(","),
        "is_active": client.is_active,
        "note": client.note,
        "created_at": client.created_at.isoformat() if client.created_at else None,
        "last_used_at": client.last_used_at.isoformat() if client.last_used_at else None,
    }


@router.get("")
async def list_keys(session: AsyncSession = Depends(get_session)) -> dict[str, object]:
    rows = list(await session.scalars(select(ServiceClient).order_by(ServiceClient.name)))
    return {
        "scopes": list(ALL_SCOPES),
        "total": len(rows),
        "items": [_client_dict(row) for row in rows],
    }


@router.post("")
async def issue_key(
    payload: dict = Body(...),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    try:
        client, raw_key = await ServiceKeyService(session).issue(
            name=str(payload.get("name", "")),
            scopes=payload.get("scopes") or "read",
            note=str(payload.get("note", "")),
        )
    except ServiceKeyError as error:
        raise HTTPException(status_code=400, detail=str(error))
    await session.commit()
    return {
        **_client_dict(client),
        "key": raw_key,
        "warning": "ключ показывается один раз, сохраните его сейчас",
    }


@router.post("/{name}/revoke")
async def revoke_key(
    name: str, session: AsyncSession = Depends(get_session)
) -> dict[str, object]:
    client = await ServiceKeyService(session).revoke(name)
    if client is None:
        raise HTTPException(status_code=404, detail="потребитель не найден")
    await session.commit()
    return _client_dict(client)
