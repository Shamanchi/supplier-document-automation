from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.fsa.schema import to_jsonable
from app.repository import RegistryRepository

router = APIRouter(prefix="/v1/registry", tags=["registry"])


@router.get("/stats")
async def stats(
    on: date | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    checked_on = on or date.today()
    result = await RegistryRepository(session).stats(checked_on)
    return {
        "certificates": result.certificates,
        "numbers": result.numbers,
        "alive": result.alive,
        "checked_on": checked_on.isoformat(),
    }


@router.get("/search")
async def search(
    q: str = Query(min_length=2),
    limit: int = Query(default=25, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    found = await RegistryRepository(session).search(q, limit=limit)
    return {"query": q, "items": to_jsonable(found)}


@router.get("/certificates/{registration_number:path}/successors")
async def successors(
    registration_number: str,
    on: date | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    repository = RegistryRepository(session)
    certificate = await repository.get(registration_number)
    if certificate is None:
        raise HTTPException(status_code=404, detail="certificate not found")
    checked_on = on or date.today()
    return {
        "registration_number": certificate.registration_number,
        "checked_on": checked_on.isoformat(),
        "alive": certificate.is_alive_on(checked_on),
        "successors": to_jsonable(
            await repository.find_successors(certificate, checked_on)
        ),
    }


@router.get("/certificates/{registration_number:path}")
async def get_certificate(
    registration_number: str,
    on: date | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    repository = RegistryRepository(session)
    certificate = await repository.get(registration_number)
    if certificate is None:
        known = await repository.is_known(registration_number)
        raise HTTPException(
            status_code=404,
            detail=(
                "номер есть в реестре, но подробной карточки нет"
                if known
                else "номер не найден в реестре"
            ),
        )
    checked_on = on or date.today()
    return {
        "certificate": to_jsonable(certificate),
        "checked_on": checked_on.isoformat(),
        "alive": certificate.is_alive_on(checked_on),
    }
