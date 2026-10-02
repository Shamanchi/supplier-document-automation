"""Доступ к реестру в базе.

Раньше индекс жил целиком в памяти процесса; в сервисе это заменено на
запросы к Postgres — иначе каждый воркер держал бы свою копию, а полный
реестр в неё не помещается.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.fsa.normalize import normalize_number, similarity
from app.fsa.schema import (
    CertificateStatus,
    RegistryCertificate as CertificateModel,
    SuccessorCandidate,
)
from app.models import RegistryCertificate, RegistryNumber

SUCCESSOR_MIN_SCORE = 0.3
SUCCESSOR_LIMIT = 3
# Живых сертификатов у одного изготовителя единицы, но у крупных бывает и
# сотни; ограничение защищает от того, чтобы тянуть их все ради сравнения.
SUCCESSOR_SCAN_LIMIT = 500

ALIVE_STATUSES = tuple(
    status.value for status in CertificateStatus if status.is_alive
)


@dataclass(frozen=True, slots=True)
class RegistryStats:
    certificates: int
    numbers: int
    alive: int


def to_domain(row: RegistryCertificate) -> CertificateModel:
    """Строка таблицы → каноническая модель схемы."""
    return CertificateModel(
        registration_number=row.registration_number,
        registration_number_key=row.registration_number_key,
        status=CertificateStatus.parse(row.status),
        status_raw=row.status_raw,
        registered_at=row.registered_at,
        valid_until=row.valid_until,
        suspended_at=row.suspended_at,
        suspension_until=row.suspension_until,
        terminated_at=row.terminated_at,
        resumed_at=row.resumed_at,
        extended_at=row.extended_at,
        object_type=row.object_type,
        blank_number=row.blank_number,
        annex_blank_number=row.annex_blank_number,
        technical_regulations=row.technical_regulations,
        product_group=row.product_group,
        applicant_name=row.applicant_name,
        applicant_inn=row.applicant_inn,
        manufacturer_name=row.manufacturer_name,
        manufacturer_inn=row.manufacturer_inn,
        manufacturer_ogrn=row.manufacturer_ogrn,
        product_full_name=row.product_full_name,
        product_designation=row.product_designation,
        product_type=row.product_type,
        product_brand=row.product_brand,
        product_model=row.product_model,
        product_article=row.product_article,
        okpd2_tnved=row.okpd2_tnved,
        product_standards=row.product_standards,
        source_slice=row.source_slice,
    )


class RegistryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def stats(self, on: date | None = None) -> RegistryStats:
        certificates = await self._session.scalar(
            select(func.count()).select_from(RegistryCertificate)
        )
        numbers = await self._session.scalar(
            select(func.count()).select_from(RegistryNumber)
        )
        alive_query = select(func.count()).select_from(RegistryCertificate).where(
            RegistryCertificate.status.in_(ALIVE_STATUSES)
        )
        if on is not None:
            alive_query = alive_query.where(RegistryCertificate.valid_until >= on)
        alive = await self._session.scalar(alive_query)
        return RegistryStats(
            certificates=certificates or 0, numbers=numbers or 0, alive=alive or 0
        )

    async def get_row(self, registration_number: str) -> RegistryCertificate | None:
        key = normalize_number(registration_number)
        return await self._session.scalar(
            select(RegistryCertificate).where(
                RegistryCertificate.registration_number_key == key
            )
        )

    async def get(self, registration_number: str) -> CertificateModel | None:
        row = await self.get_row(registration_number)
        return to_domain(row) if row else None

    async def is_known(self, registration_number: str) -> bool:
        key = normalize_number(registration_number)
        found = await self._session.scalar(
            select(RegistryNumber.registration_number_key).where(
                RegistryNumber.registration_number_key == key
            )
        )
        return found is not None

    async def search(
        self, query: str, *, limit: int = 25
    ) -> list[CertificateModel]:
        """Поиск по номеру, изготовителю или наименованию продукции."""
        pattern = f"%{query.strip()}%"
        rows = await self._session.scalars(
            select(RegistryCertificate)
            .where(
                RegistryCertificate.registration_number_key.ilike(
                    f"%{normalize_number(query)}%"
                )
                | RegistryCertificate.manufacturer_name.ilike(pattern)
                | RegistryCertificate.product_full_name.ilike(pattern)
            )
            .order_by(RegistryCertificate.valid_until.desc().nullslast())
            .limit(limit)
        )
        return [to_domain(row) for row in rows]

    async def find_successors(
        self,
        certificate: CertificateModel,
        on: date,
        *,
        min_score: float = SUCCESSOR_MIN_SCORE,
        limit: int = SUCCESSOR_LIMIT,
    ) -> list[SuccessorCandidate]:
        """Живые сертификаты того же изготовителя на схожую продукцию.

        Явной связи «старый → новый» реестр не хранит, поэтому кандидат
        остаётся подсказкой и никогда не подставляется автоматически.
        """
        inn = certificate.manufacturer_inn.strip()
        source = " ".join(certificate.product_keys)
        if not inn or not source:
            return []

        rows = await self._session.scalars(
            select(RegistryCertificate)
            .where(
                RegistryCertificate.manufacturer_inn == inn,
                RegistryCertificate.status.in_(ALIVE_STATUSES),
                RegistryCertificate.valid_until >= on,
                RegistryCertificate.registration_number_key
                != certificate.registration_number_key,
            )
            .limit(SUCCESSOR_SCAN_LIMIT)
        )

        scored: list[SuccessorCandidate] = []
        for row in rows:
            candidate = to_domain(row)
            score = similarity(source, " ".join(candidate.product_keys))
            if score < min_score:
                continue
            scored.append(
                SuccessorCandidate(
                    certificate=candidate,
                    score=round(score, 3),
                    reason=(
                        f"тот же изготовитель по ИНН, схожесть наименования {score:.2f}"
                    ),
                )
            )
        scored.sort(key=lambda item: item.score, reverse=True)
        return scored[:limit]
