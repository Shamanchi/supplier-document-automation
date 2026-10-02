"""Клиент контура сертификатов (сервис FSA) и кэш привязок.

Загрузчик прайсов не знает ни про OCR, ни про реестр Росаккредитации: он
спрашивает «что с документацией у этого изделия» и получает вердикт на дату.
Так граница между модулями остаётся там, где её провело ТЗ, — Фаза 2 находит
документы, но не разбирается в них.

Сервис может быть не поднят, и это нормальный режим, а не отказ: расчёт
продолжается, а в позиции честно стоит «не проверено». Молчаливая подстановка
«документа нет» была бы хуже — её не отличить от проверенного отсутствия.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import CanonicalProduct, DocumentLink, ProductDocument

TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class CertificateInfo:
    available: bool
    verdict: str
    registration_number: str = ""
    valid_until: str = ""
    manufacturer: str = ""
    product_full_name: str = ""
    matched_by: str = ""
    score: float = 0.0
    needs_manual_review: bool = True
    review_reasons: tuple[str, ...] = ()
    candidates: int = 0
    document_confirmed_by: str = ""
    reason_unavailable: str = ""

    @property
    def attachable(self) -> bool:
        """Приложить к КП можно только подтверждённый действующий документ."""
        return (
            self.available
            and self.verdict == "valid"
            and not self.needs_manual_review
            and bool(self.document_confirmed_by)
        )


UNAVAILABLE = CertificateInfo(
    available=False,
    verdict="unknown",
    reason_unavailable="сервис сертификатов не настроен",
)


class CertificateService:
    """Спрашивает FSA про изделие и запоминает подтверждённые привязки."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def configured(self) -> bool:
        return bool(getattr(settings, "fsa_base_url", ""))

    async def for_product(
        self,
        *,
        product: str,
        position_ref: str = "",
        number: str = "",
        on: date | None = None,
    ) -> CertificateInfo:
        if not self.configured:
            return UNAVAILABLE

        params: dict[str, str] = {"position_ref": position_ref}
        if number:
            params["number"] = number
        else:
            params["product"] = product
        if on is not None:
            params["on"] = on.isoformat()

        headers = {}
        token = getattr(settings, "fsa_api_token", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"

        url = f"{settings.fsa_base_url.rstrip('/')}/v1/lookup/link"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.get(url, params=params, headers=headers)
                response.raise_for_status()
                payload = response.json()
        except Exception as error:  # noqa: BLE001 — важен факт недоступности
            return CertificateInfo(
                available=False,
                verdict="unknown",
                reason_unavailable=f"сервис сертификатов недоступен: {error}",
            )

        document = payload.get("document") or {}
        return CertificateInfo(
            available=True,
            verdict=str(payload.get("verdict") or "unknown"),
            registration_number=str(payload.get("registration_number") or ""),
            valid_until=str(payload.get("valid_until") or ""),
            manufacturer=str(payload.get("manufacturer") or ""),
            product_full_name=str(payload.get("product_full_name") or ""),
            matched_by=str(payload.get("matched_by") or ""),
            score=float(payload.get("score") or 0.0),
            needs_manual_review=bool(payload.get("needs_manual_review", True)),
            review_reasons=tuple(payload.get("review_reasons") or ()),
            candidates=len(payload.get("candidates") or ()),
            document_confirmed_by=str(document.get("confirmed_by") or ""),
        )

    async def save_link(
        self,
        *,
        canonical: CanonicalProduct,
        info: CertificateInfo,
        matched_key: str = "",
        checked_on: date | None = None,
    ) -> DocumentLink | None:
        """Сохранить привязку с доказательством.

        Без `evidence` через полгода не ответить, почему к позиции приложен
        именно этот документ, — поэтому пустую привязку не пишем вовсе.
        """
        if not info.available or not info.registration_number:
            return None

        document = await self._session.scalar(
            select(ProductDocument).where(
                ProductDocument.registration_number == info.registration_number
            )
        )
        if document is None:
            document = ProductDocument(
                kind="certificate",
                title=info.product_full_name[:200],
                registration_number=info.registration_number,
                source="fsa_service",
            )
            self._session.add(document)
            await self._session.flush()

        link = await self._session.scalar(
            select(DocumentLink).where(
                DocumentLink.document_id == document.id,
                DocumentLink.canonical_product_id == canonical.id,
            )
        )
        evidence = {
            "registration_number": info.registration_number,
            "matched_key": matched_key,
            "matched_by": info.matched_by,
            "valid_until": info.valid_until,
            "verdict": info.verdict,
            "confirmed_by": info.document_confirmed_by,
            "checked_on": (checked_on or date.today()).isoformat(),
        }
        if link is None:
            link = DocumentLink(
                document_id=document.id,
                canonical_product_id=canonical.id,
                matched_by="registry",
                confidence=info.score or None,
                evidence=evidence,
                confirmed_by=info.document_confirmed_by or None,
            )
            self._session.add(link)
        else:
            link.evidence = evidence
            link.confirmed_by = info.document_confirmed_by or None
        await self._session.flush()
        return link
