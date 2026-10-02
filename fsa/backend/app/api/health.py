from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.repository import RegistryRepository

router = APIRouter()


@router.get("/health")
async def health(session: AsyncSession = Depends(get_session)) -> dict[str, object]:
    """Открыт без ключа — для healthcheck, как у OCR-движков."""
    stats = await RegistryRepository(session).stats()
    return {
        "status": "ok",
        "engine": settings.ocr_engine,
        "registry": {"certificates": stats.certificates, "numbers": stats.numbers},
    }
