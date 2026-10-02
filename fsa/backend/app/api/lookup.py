from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.services.lookup import LookupService, verdict_of

router = APIRouter(prefix="/v1/lookup", tags=["lookup"])


def _certificate_dict(certificate, on: date) -> dict[str, object]:
    return {
        "registration_number": certificate.registration_number,
        "status": certificate.status_raw or certificate.status.value,
        "verdict": verdict_of(certificate, on),
        "valid_until": certificate.valid_until.isoformat()
        if certificate.valid_until
        else "",
        "manufacturer": certificate.manufacturer_name,
        "manufacturer_inn": certificate.manufacturer_inn,
        "product_full_name": certificate.product_full_name,
        "technical_regulations": certificate.technical_regulations,
    }


@router.get("/by-number")
async def by_number(
    number: str = Query(min_length=6),
    on: date | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Карточка и вердикт по номеру сертификата плюс наши файлы по нему."""
    checked_on = on or date.today()
    service = LookupService(session)
    found = await service.by_number(number, on=checked_on)
    if found is None:
        raise HTTPException(status_code=404, detail="сертификат не найден в реестре")
    documents = await service.documents_for(
        found.certificate.registration_number_key
    )
    return {
        "matched_by": found.matched_by,
        "checked_on": checked_on.isoformat(),
        "certificate": _certificate_dict(found.certificate, checked_on),
        "documents": documents,
    }


@router.get("/by-product")
async def by_product(
    code: str = Query(min_length=2, description="артикул, обозначение или строка прайса"),
    on: date | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Сертификаты по коду продукта.

    Кандидаты возвращаются списком и никогда не подставляются автоматически:
    одно обозначение встречается у нескольких сертификатов разных лет.
    """
    checked_on = on or date.today()
    candidates = await LookupService(session).by_product(
        code, on=checked_on, limit=limit
    )
    return {
        "query": code,
        "checked_on": checked_on.isoformat(),
        "total": len(candidates),
        "items": [
            {
                **_certificate_dict(item.certificate, checked_on),
                "matched_key": item.matched_key,
                "score": item.score,
            }
            for item in candidates
        ],
    }


@router.get("/link")
async def link(
    position_ref: str = Query(default=""),
    number: str = Query(default=""),
    product: str = Query(default=""),
    on: date | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Привязка документа к позиции расчёта — то, что забирает price_loader_app.

    Автоматическая привязка возможна ровно в одном случае: кандидат один, он
    действует, и файл по нему подтверждён человеком.
    """
    if not number and not product:
        raise HTTPException(status_code=400, detail="нужен number или product")
    return await LookupService(session).link(
        position_ref=position_ref,
        number=number,
        product=product,
        on=on or date.today(),
    )


@router.post("/product-keys/rebuild")
async def rebuild(
    session: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Пересобрать индекс обозначений после импорта среза реестра."""
    result = await LookupService(session).rebuild_product_keys()
    await session.commit()
    return result


@router.get("/product-keys/stats")
async def stats(session: AsyncSession = Depends(get_session)) -> dict[str, int]:
    return await LookupService(session).product_key_stats()
