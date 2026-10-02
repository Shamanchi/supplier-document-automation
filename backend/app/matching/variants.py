"""Все позиции прайсов, относящиеся к обозначению из строки заявки.

Каскад отвечает на вопрос «какой товар имел в виду клиент» и отдаёт тройку
лучших. Менеджеру этого мало: по строке «Огнетушитель ОП-4 закачной» он хочет
видеть **весь ряд ОП-4** — «(з) ВСЕ ЗПУ Россия», «(з) АВСЕ ЗПУ Китай»,
«Премиум красный» и так далее, у всех поставщиков и с ценами. Выбор исполнения
всё равно за ним, и делать этот выбор вслепую по трём кандидатам неправильно.

Поэтому здесь другой контракт: не «лучший кандидат», а **полная выборка по
обозначению** с пометкой, что из неё ближе к запросу. Ранжирование остаётся —
но ничего не отсекается.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matching.text import designations, normalize_name, significant_tokens
from app.models import (
    CanonicalProduct,
    CanonicalProductSource,
    PriceDocumentType,
    PriceVersion,
    Supplier,
    SupplierPriceItem,
    SupplierPriceValue,
    VersionStatus,
)

# Верхняя граница выборки: под одним обозначением у семи поставщиков редко
# бывает больше нескольких десятков исполнений, а запрос вроде «К-1» может
# зацепить сотни.
MAX_VARIANTS = 100

# Версии, позиции которых показываются менеджеру. Снятый с публикации прайс в
# выборку не попадает — его цены недействительны.
# Global price lists are effective after activation; a validated supplier quote
# is effective only in its own check batch.
SEARCHABLE_STATUSES = (VersionStatus.active.value, VersionStatus.validated.value)


def searchable_document_scope(check_batch_id: uuid.UUID | None):
    """Какие ценовые документы участвуют в подборе.

    Общий прайс (`PRICE_LIST`) доступен всем заявкам только после активации.
    Разовый счёт/КП (`SUPPLIER_QUOTE`) доступен только своей заявке после
    валидации и не попадает в общий каталог.
    """
    clauses = [
        and_(
            PriceVersion.document_type == PriceDocumentType.price_list.value,
            PriceVersion.status == VersionStatus.active.value,
        )
    ]
    if check_batch_id is not None:
        clauses.append(
            and_(
                PriceVersion.document_type
                == PriceDocumentType.supplier_quote.value,
                PriceVersion.check_batch_id == check_batch_id,
                PriceVersion.status == VersionStatus.validated.value,
            )
        )
    return or_(*clauses)


def price_version_is_searchable(
    version: PriceVersion, check_batch_id: uuid.UUID | None
) -> bool:
    if version.document_type == PriceDocumentType.price_list.value:
        return version.status == VersionStatus.active.value
    return (
        version.document_type == PriceDocumentType.supplier_quote.value
        and version.status == VersionStatus.validated.value
        and check_batch_id is not None
        and version.check_batch_id == check_batch_id
    )


@dataclass(slots=True)
class Offer:
    """Позиция конкретного поставщика под каноническим товаром."""

    supplier: str
    name: str
    unit: str | None
    price: Decimal | None
    currency: str | None
    price_version_id: str
    # Идентификатор строки прайса: по нему предложение кладётся в расчёт, где
    # заново берутся цена под количество и скидки поставщика.
    price_item_id: str
    # Снимок источника цены для расчётной строки: поставщик и назначение
    # документа (общий прайс или разовый счёт/КП заявки).
    supplier_id: str
    document_type: str


@dataclass(slots=True)
class Variant:
    canonical_id: str
    title: str
    article: str | None
    merge_status: str
    matched_designation: str
    relevance: float
    offers: list[Offer] = field(default_factory=list)


class VariantService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def by_query(
        self,
        query: str,
        *,
        supplier: str = "",
        supplier_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        check_batch_id: uuid.UUID | None = None,
        limit: int = MAX_VARIANTS,
    ) -> tuple[list[str], list[Variant]]:
        """Все варианты по обозначениям из запроса и сами обозначения.

        Возвращается пара, потому что менеджеру важно видеть, по какому именно
        обозначению собрана выборка: «ОП-4» и «ОП-40» отличаются одной цифрой,
        и молча подставленный не тот ряд хуже пустого ответа.
        """
        wanted = designations(query)
        if not wanted:
            return [], []

        rows = list(
            await self._session.scalars(
                select(CanonicalProduct)
                .where(
                    or_(
                        *[
                            CanonicalProduct.normalized_title.like(f"%{item}%")
                            for item in wanted[:3]
                        ]
                    )
                )
                .limit(MAX_VARIANTS * 3)
            )
        )

        query_tokens = set(significant_tokens(query))
        variants: list[Variant] = []
        for row in rows:
            title_designations = set(designations(row.normalized_title))
            matched = next((item for item in wanted if item in title_designations), "")
            if not matched:
                # Совпало подстрокой, но не как обозначение: «ОП-4» внутри
                # «ОП-40» — другой товар, а не исполнение.
                continue
            found = set(significant_tokens(row.title))
            relevance = (
                len(query_tokens & found) / len(query_tokens) if query_tokens else 0.0
            )
            variants.append(
                Variant(
                    canonical_id=str(row.id),
                    title=row.title,
                    article=row.normalized_article,
                    merge_status=row.merge_status,
                    matched_designation=matched,
                    relevance=round(relevance, 3),
                )
            )

        if not variants:
            return wanted, []

        await self._attach_offers(
            variants,
            supplier=supplier,
            supplier_id=supplier_id,
            project_id=project_id,
            check_batch_id=check_batch_id,
        )
        # Канон без единого предложения в выбранной области показывать незачем:
        # это товар из другого проекта или снятого прайса.
        variants = [item for item in variants if item.offers]
        if not variants:
            return wanted, []

        # Сначала то, что ближе к формулировке заявки, потом — остальной ряд.
        # Ничего не отсекается: менеджер выбирает исполнение сам.
        variants.sort(key=lambda item: (-item.relevance, normalize_name(item.title)))
        return wanted, variants[:limit]

    async def by_ids(
        self,
        canonical_ids: list[uuid.UUID],
        *,
        supplier: str = "",
        supplier_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        check_batch_id: uuid.UUID | None = None,
    ) -> list[Variant]:
        """Варианты по списку канонов с их предложениями.

        Обратная сторона `by_query`: там выборка строится от обозначения,
        а здесь каноны уже известны (например, их нашёл каскад подбора по
        имени позиции) — нужно показать их ряд исполнений с ценами. Канон
        без единого предложения отсеивается по той же причине, что и в
        `by_query`: показывать товар без цены менеджеру незачем.
        """
        ids = list(dict.fromkeys(canonical_ids))
        if not ids:
            return []
        rows = list(
            await self._session.scalars(
                select(CanonicalProduct).where(CanonicalProduct.id.in_(ids))
            )
        )
        variants = [
            Variant(
                canonical_id=str(row.id),
                title=row.title,
                article=row.normalized_article,
                merge_status=row.merge_status,
                matched_designation="",
                relevance=0.0,
            )
            for row in rows
        ]
        if not variants:
            return []
        await self._attach_offers(
            variants,
            supplier=supplier,
            supplier_id=supplier_id,
            project_id=project_id,
            check_batch_id=check_batch_id,
        )
        return [item for item in variants if item.offers]

    async def _attach_offers(
        self,
        variants: list[Variant],
        *,
        supplier: str,
        supplier_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        check_batch_id: uuid.UUID | None = None,
    ) -> None:
        """Позиции поставщиков с ценами для каждого канонического товара."""
        by_id = {uuid.UUID(item.canonical_id): item for item in variants}
        statement = (
            select(
                CanonicalProductSource.canonical_product_id,
                Supplier.name,
                SupplierPriceItem.source_name,
                SupplierPriceItem.unit_code,
                SupplierPriceItem.price_version_id,
                SupplierPriceValue.amount,
                SupplierPriceValue.currency,
                SupplierPriceItem.id,
                PriceVersion.supplier_id,
                PriceVersion.document_type,
            )
            .join(
                SupplierPriceItem,
                SupplierPriceItem.id == CanonicalProductSource.price_item_id,
            )
            .join(PriceVersion, PriceVersion.id == SupplierPriceItem.price_version_id)
            .join(Supplier, Supplier.id == PriceVersion.supplier_id, isouter=True)
            .join(
                SupplierPriceValue,
                SupplierPriceValue.item_id == SupplierPriceItem.id,
                isouter=True,
            )
            .where(
                CanonicalProductSource.canonical_product_id.in_(by_id),
                searchable_document_scope(check_batch_id),
            )
        )
        if supplier:
            statement = statement.where(Supplier.name.ilike(f"%{supplier.strip()}%"))
        if supplier_id is not None:
            statement = statement.where(PriceVersion.supplier_id == supplier_id)
        if project_id is not None:
            statement = statement.where(
                CanonicalProductSource.project_id == project_id
            )

        seen: set[tuple[uuid.UUID, str, str]] = set()
        for row in await self._session.execute(statement):
            variant = by_id.get(row[0])
            if variant is None:
                continue
            # У позиции бывает несколько цен (ступени количества, розница и
            # опт). Для обзора хватает одной строки на позицию — иначе один
            # товар растянется на десяток одинаковых предложений.
            key = (row[0], row[1] or "", row[2] or "")
            if key in seen:
                continue
            seen.add(key)
            variant.offers.append(
                Offer(
                    supplier=row[1] or "",
                    name=row[2] or "",
                    unit=row[3],
                    price=row[5],
                    currency=row[6],
                    price_version_id=str(row[4]),
                    price_item_id=str(row[7]),
                    supplier_id=str(row[8]) if row[8] is not None else "",
                    document_type=row[9] or PriceDocumentType.price_list.value,
                )
            )

        for variant in variants:
            variant.offers.sort(key=lambda offer: (offer.supplier, offer.name))
