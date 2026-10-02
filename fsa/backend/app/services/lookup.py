"""Два входа в реестр — по номеру сертификата и по коду продукта.

Номер работает, когда документ уже найден и распознан. У строки прайса номера
нет: есть артикул и наименование, и именно от них расчёт идёт к документации.
Поэтому вход два, а ответ один — карточка реестра, её вердикт на дату и, если
файл документа у нас есть, его состояние вместе с подтверждением человека.

Правило разрешения конфликтов одно: **при нескольких кандидатах ничего не
подставляется автоматически**. Возвращается список с оценками, а привязку
подтверждает менеджер — ложно приложенный чужой сертификат дороже, чем пустая
ячейка «проверить вручную».
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.fsa.normalize import normalize_number, similarity
from app.fsa.product_keys import designations_of, lookup_keys
from app.fsa.schema import Verdict
from app.models import (
    Document,
    DocumentCheck,
    RegistryCertificate,
    RegistryProductKey,
)
from app.repository import RegistryRepository, to_domain
from app.services.review import ReviewService, effective_state

# Совпало обозначение — это ещё не совпал товар: под «ОП-5» у одного
# изготовителя может быть и порошковый, и углекислотный. Наименование
# сравнивается дополнительно, и слишком далёкие кандидаты не показываются.
MIN_NAME_SIMILARITY = 0.1


@dataclass(frozen=True, slots=True)
class Candidate:
    certificate: RegistryCertificate
    matched_by: str
    matched_key: str
    score: float


def verdict_of(certificate: RegistryCertificate, on: date) -> str:
    if certificate.status in {"Приостановлен"}:
        return Verdict.SUSPENDED.value
    if certificate.valid_until is None:
        return Verdict.UNKNOWN.value
    if certificate.valid_until < on:
        return Verdict.EXPIRED.value
    if certificate.status in {"Действует", "Возобновлен", "Продлен"}:
        return Verdict.VALID.value
    return Verdict.TERMINATED.value


class LookupService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._registry = RegistryRepository(session)
        self._review = ReviewService(session)

    # ---------------------------------------------------------------- индекс

    async def rebuild_product_keys(self, *, batch: int = 2000) -> dict[str, int]:
        """Пересобрать индекс обозначений из карточек реестра.

        Индекс производный: его можно снести и построить заново после каждого
        импорта среза. Дешевле, чем поддерживать инкрементально и однажды
        разъехаться с картотекой.
        """
        await self._session.execute(RegistryProductKey.__table__.delete())
        total_certificates = 0
        total_keys = 0
        last_id = None
        while True:
            query = select(RegistryCertificate).order_by(RegistryCertificate.id).limit(batch)
            if last_id is not None:
                query = query.where(RegistryCertificate.id > last_id)
            rows = list(await self._session.scalars(query))
            if not rows:
                break
            last_id = rows[-1].id
            for row in rows:
                total_certificates += 1
                # Одно и то же обозначение приходит из разных полей карточки
                # («Модель» и «Полное наименование») и после раскрытия
                # диапазонов. Внутри карточки ключ должен быть один: пара
                # (карточка, ключ) уникальна, и дубль роняет всю пересборку.
                keys = dict.fromkeys(
                    designations_of(
                        row.product_designation,
                        row.product_model,
                        row.product_brand,
                        row.product_article,
                        row.product_full_name,
                    )
                )
                for key in keys:
                    self._session.add(
                        RegistryProductKey(certificate_id=row.id, product_key=key)
                    )
                    total_keys += 1
            await self._session.flush()
        return {"certificates": total_certificates, "keys": total_keys}

    async def product_key_stats(self) -> dict[str, int]:
        keys = await self._session.scalar(
            select(func.count()).select_from(RegistryProductKey)
        )
        covered = await self._session.scalar(
            select(func.count(func.distinct(RegistryProductKey.certificate_id)))
        )
        return {"keys": keys or 0, "certificates_covered": covered or 0}

    # ---------------------------------------------------------------- поиск

    async def by_number(self, number: str, *, on: date) -> Candidate | None:
        certificate = await self._registry.get(number)
        if certificate is None:
            return None
        return Candidate(
            certificate=certificate,
            matched_by="registration_number",
            matched_key=normalize_number(number),
            score=1.0,
        )

    async def by_product(
        self, query: str, *, on: date, limit: int = 10
    ) -> list[Candidate]:
        """Кандидаты по коду продукта: точное совпадение обозначения."""
        keys = lookup_keys(query)
        if not keys:
            return []

        rows = list(
            await self._session.execute(
                select(RegistryCertificate, RegistryProductKey.product_key)
                .join(
                    RegistryProductKey,
                    RegistryProductKey.certificate_id == RegistryCertificate.id,
                )
                .where(RegistryProductKey.product_key.in_(keys))
                .limit(500)
            )
        )

        candidates: list[Candidate] = []
        seen: set[str] = set()
        for row, matched_key in rows:
            certificate = to_domain(row)
            if certificate.registration_number_key in seen:
                continue
            seen.add(certificate.registration_number_key)
            score = similarity(query, certificate.product_full_name)
            if score < MIN_NAME_SIMILARITY and len(keys) > 1:
                continue
            candidates.append(
                Candidate(
                    certificate=certificate,
                    matched_by="product_key",
                    matched_key=matched_key,
                    score=round(score, 3),
                )
            )

        # Живые вперёд, среди них — с более поздним сроком и более похожим
        # наименованием. Порядок и есть подсказка менеджеру, что смотреть первым.
        candidates.sort(
            key=lambda item: (
                verdict_of(item.certificate, on) != Verdict.VALID.value,
                -(item.certificate.valid_until.toordinal()
                  if item.certificate.valid_until
                  else 0),
                -item.score,
            )
        )
        return candidates[:limit]

    # ------------------------------------------------- документы и привязка

    async def documents_for(self, certificate_key: str) -> list[dict[str, object]]:
        """Файлы, которые мы уже загрузили под этот номер, с подтверждением."""
        documents = list(
            await self._session.scalars(
                select(Document).where(
                    Document.registration_number_key == certificate_key
                )
            )
        )
        result: list[dict[str, object]] = []
        for document in documents:
            check = await self._review.latest_check(document.id)
            decision = await self._review.latest_decision(document.id)
            state = effective_state(document, check, decision)
            result.append(
                {
                    "document_id": str(document.id),
                    "filename": document.filename,
                    "supplier": document.supplier,
                    "sha256": document.sha256,
                    "verdict": state.verdict,
                    "confirmed": state.decided and not state.stale,
                    "confirmed_by": state.reviewer,
                    "confirmed_at": state.decided_at,
                    "action": state.action,
                    "stale": state.stale,
                    "attachable": state.attachable,
                }
            )
        # Подтверждённый файл — первый: именно его ссылку кладут в расчёт.
        result.sort(key=lambda item: (not item["attachable"], not item["confirmed"]))
        return result

    async def link(
        self,
        *,
        position_ref: str,
        number: str = "",
        product: str = "",
        on: date,
        limit: int = 5,
    ) -> dict[str, object]:
        """Ответ для расчёта: одна привязка либо список кандидатов.

        Автоматическая привязка возможна ровно в одном случае: кандидат один,
        он действует, и загруженный документ по нему подтверждён человеком.
        Во всех остальных — `needs_manual_review` и список для выбора.
        """
        candidates: list[Candidate] = []
        if number:
            found = await self.by_number(number, on=on)
            if found is not None:
                candidates = [found]
        elif product:
            candidates = await self.by_product(product, on=on, limit=limit)

        if not candidates:
            return {
                "position_ref": position_ref,
                "matched_by": "none",
                "verdict": Verdict.NOT_FOUND.value if number else Verdict.UNKNOWN.value,
                "needs_manual_review": True,
                "review_reasons": [
                    "сертификат по номеру не найден"
                    if number
                    else "по коду продукта в реестре ничего не нашлось"
                ],
                "candidates": [],
            }

        best = candidates[0]
        verdict = verdict_of(best.certificate, on)
        documents = await self.documents_for(best.certificate.registration_number_key)
        attachable_document = next(
            (item for item in documents if item["attachable"]), None
        )

        reasons: list[str] = []
        if len(candidates) > 1:
            reasons.append(
                f"по коду продукта найдено несколько сертификатов ({len(candidates)}) "
                "— выбор за менеджером"
            )
        if verdict != Verdict.VALID.value:
            reasons.append(f"вердикт реестра: {verdict}")
        if not documents:
            reasons.append("файл документа не загружен — приложить нечего")
        elif attachable_document is None:
            reasons.append("документ загружен, но не подтверждён человеком")

        return {
            "position_ref": position_ref,
            "matched_by": best.matched_by,
            "matched_key": best.matched_key,
            "score": best.score,
            "registration_number": best.certificate.registration_number,
            "verdict": verdict,
            "valid_until": best.certificate.valid_until.isoformat()
            if best.certificate.valid_until
            else "",
            "manufacturer": best.certificate.manufacturer_name,
            "product_full_name": best.certificate.product_full_name,
            "document": attachable_document,
            "documents": documents,
            "needs_manual_review": bool(reasons),
            "review_reasons": reasons,
            "candidates": [
                {
                    "registration_number": item.certificate.registration_number,
                    "verdict": verdict_of(item.certificate, on),
                    "valid_until": item.certificate.valid_until.isoformat()
                    if item.certificate.valid_until
                    else "",
                    "manufacturer": item.certificate.manufacturer_name,
                    "product_full_name": item.certificate.product_full_name[:200],
                    "matched_key": item.matched_key,
                    "score": item.score,
                }
                for item in candidates
            ],
        }


async def latest_check_for(session: AsyncSession, document_id) -> DocumentCheck | None:
    return await ReviewService(session).latest_check(document_id)
