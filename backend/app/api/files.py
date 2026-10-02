"""Price-file endpoints."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.config import settings
from app.db import get_session
from app.models import PriceFile
from app.schemas import DownloadUrl

router = APIRouter(tags=["price-files"])


@router.get("/price-files/{file_id}/download-url", response_model=DownloadUrl)
async def download_url(
    file_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> DownloadUrl:
    price_file = await session.get(PriceFile, file_id)
    if price_file is None:
        raise HTTPException(status_code=404, detail="Файл не найден")
    url = storage.presigned_get(price_file.storage_key)
    return DownloadUrl(url=url, expires_in=settings.presign_expiry_seconds)
