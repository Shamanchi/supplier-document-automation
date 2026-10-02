"""Сведение позиций поставщиков к каноническим товарам.

Правило слияния простое и намеренно консервативное: позиции сходятся в один
канон, если у них совпал нормализованный артикул, а при его отсутствии —
нормализованное наименование. Более умное слияние (по похожести) здесь опасно:
ошибка канона размножается на все прайсы сразу и портит и подбор, и привязку
документов.

Спорные случаи не прячутся: если под одним артикулом у разных поставщиков
лежат заметно разные наименования, канон помечается `disputed` и ждёт человека.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.matching.text import (
    article_key,
    embed,
    normalize_name,
    significant_tokens,
)
from app.models import (
    CanonicalProduct,
    CanonicalProductSource,
    DocumentLink,
    MergeStatus,
    PriceFile,
    PriceVersion,
    SupplierPriceItem,
)

# Ниже этого пересечения значимых токенов наименования под одним артикулом
# считаются разными товарами, а канон — спорным.
DISPUTE_SIMILARITY = 0.34

# Размер пачки для потокового чтения и пакетной записи.
CHUNK = 5_000

# Сколько наименований под одним ключом хранить для проверки на спорность.
TITLE_SAMPLES = 5


@dataclass(frozen=True, slots=True)
class RebuildResult:
    items: int
    canonicals: int
    disputed: int


# Ключ слияния — колонка с ограничением длины, а наименования в прайсах бывают
# в сотни символов (услуги описывают абзацем). Длинный ключ схлопывается в
# читаемый префикс плюс хеш: префикс нужен человеку в логах, хеш — уникальности.
MAX_KEY_LENGTH = 200


def _bounded(prefix: str, value: str) -> str:
    key = f"{prefix}:{value}"
    if len(key) <= MAX_KEY_LENGTH:
        return key
    digest = hashlib.blake2s(value.encode("utf-8"), digest_size=8).hexdigest()
    head = value[: MAX_KEY_LENGTH - len(prefix) - len(digest) - 2]
    return f"{prefix}:{head}#{digest}"


def match_key_for(item: SupplierPriceItem) -> tuple[str, str]:
    """Ключ слияния и способ, которым он получен."""
    article = article_key(item.normalized_article or item.source_article)
    if article:
        return _bounded("article", article), "article"
    return _bounded("name", normalize_name(item.normalized_name or item.source_name)), "name"


def _similarity(left: str, right: str) -> float:
    a, b = set(significant_tokens(left)), set(significant_tokens(right))
    if not a or not b:
        return 1.0
    return len(a & b) / len(a | b)


class CanonicalService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _document_linked_keys(self) -> dict[str, uuid.UUID]:
        """match_key -> id канонов, к которым привязаны документы (document_links).

        Каскадный delete от canonical_products сносит и привязки, поэтому такие
        каноны rebuild не удаляет, а вливает в них позиции прайсов с тем же ключом.
        """
        rows = await self._session.execute(
            select(CanonicalProduct.id, CanonicalProduct.match_key)
            .join(DocumentLink, DocumentLink.canonical_product_id == CanonicalProduct.id)
            .distinct()
        )
        return {row.match_key: row.id for row in rows}

    async def rebuild(self, *, limit: int = 0) -> RebuildResult:
        """Пересобрать канонический слой из разобранных позиций.

        Слой производный: его дешевле построить заново, чем поддерживать
        инкрементально и однажды разойтись с прайсами. Прайсы заказчика — это
        сотни тысяч строк, поэтому проход потоковый, а запись пакетная: ORM
        построчно на таком объёме уходит в десятки минут.

        Каноны, к которым привязаны документы (document_links), пересборку
        переживают: иначе ON DELETE CASCADE рвёт связку «канон → паспорт»,
        созданную скриптами. Позиции прайсов с тем же match_key вливаются в
        такой канон обновлением полей и источников вместо вставки дубликата.
        """
        protected = await self._document_linked_keys()

        await self._session.execute(CanonicalProductSource.__table__.delete())
        if protected:
            await self._session.execute(
                CanonicalProduct.__table__.delete().where(
                    CanonicalProduct.id.not_in(list(protected.values()))
                )
            )
        else:
            await self._session.execute(CanonicalProduct.__table__.delete())

        query = (
            select(
                SupplierPriceItem.id,
                SupplierPriceItem.source_name,
                SupplierPriceItem.normalized_name,
                SupplierPriceItem.source_article,
                SupplierPriceItem.normalized_article,
                SupplierPriceItem.manual_category_id,
                PriceVersion.supplier_id,
                PriceFile.project_id,
            )
            .join(PriceVersion, PriceVersion.id == SupplierPriceItem.price_version_id)
            .join(PriceFile, PriceFile.id == PriceVersion.price_file_id)
            .order_by(SupplierPriceItem.created_at)
        )
        if limit:
            query = query.limit(limit)

        groups: dict[str, dict[str, object]] = {}
        sources: list[tuple[uuid.UUID, str, uuid.UUID | None, uuid.UUID | None]] = []
        items = 0

        result = await self._session.stream(query.execution_options(yield_per=CHUNK))
        async for row in result:
            items += 1
            article = article_key(row.normalized_article or row.source_article)
            title = (row.source_name or row.normalized_name or "").strip()
            key = (
                _bounded("article", article)
                if article
                else _bounded("name", normalize_name(title))
            )
            group = groups.get(key)
            if group is None:
                groups[key] = {
                    "id": uuid.uuid4(),
                    "title": title,
                    "article": article or None,
                    "category_id": row.manual_category_id,
                    "count": 1,
                    # Хранится не весь список наименований, а несколько
                    # образцов: расхождение видно и по ним, а память на
                    # полумиллионе строк не растёт линейно.
                    "samples": [title],
                }
            else:
                group["count"] = int(group["count"]) + 1
                samples = group["samples"]
                if len(samples) < TITLE_SAMPLES:
                    samples.append(title)
            sources.append((row.id, key, row.supplier_id, row.project_id))

        # Группа с ключом защищённого канона вливается в существующую строку:
        # match_key уникален, и вставка дубликата упала бы на ограничении.
        for key, group in groups.items():
            canonical_id = protected.get(key)
            if canonical_id is not None:
                group["id"] = canonical_id

        disputed = 0
        fresh_rows: list[dict[str, object]] = []
        merged: list[tuple[uuid.UUID, dict[str, object]]] = []
        for key, group in groups.items():
            samples = list(group["samples"])
            status = MergeStatus.auto.value
            if len(samples) > 1:
                worst = min(_similarity(samples[0], other) for other in samples[1:])
                if worst < DISPUTE_SIMILARITY:
                    status = MergeStatus.disputed.value
                    disputed += 1
            title = str(group["title"])
            row: dict[str, object] = {
                "title": title,
                "normalized_title": normalize_name(title),
                "normalized_article": group["article"],
                "category_id": group["category_id"],
                "merge_status": status,
                "sources_count": group["count"],
                "embedding": embed(title),
            }
            if key in protected:
                merged.append((group["id"], row))
            else:
                fresh_rows.append({"id": group["id"], "match_key": key, **row})

        now = datetime.now(timezone.utc)
        for canonical_id, row in merged:
            await self._session.execute(
                update(CanonicalProduct)
                .where(CanonicalProduct.id == canonical_id)
                .values(updated_at=now, **row)
            )

        # Защищённый канон без текущих позиций остаётся (к нему привязаны
        # документы), но его источники пересобраны с нуля — счётчик к факту.
        for key, canonical_id in protected.items():
            if key not in groups:
                await self._session.execute(
                    update(CanonicalProduct)
                    .where(CanonicalProduct.id == canonical_id)
                    .values(sources_count=0, updated_at=now)
                )

        for start in range(0, len(fresh_rows), CHUNK):
            await self._session.execute(
                insert(CanonicalProduct), fresh_rows[start : start + CHUNK]
            )

        source_rows = [
            {
                "id": uuid.uuid4(),
                "canonical_product_id": groups[key]["id"],
                "price_item_id": item_id,
                "supplier_id": supplier_id,
                "project_id": project_id,
            }
            for item_id, key, supplier_id, project_id in sources
        ]
        for start in range(0, len(source_rows), CHUNK):
            await self._session.execute(
                insert(CanonicalProductSource), source_rows[start : start + CHUNK]
            )

        await self._session.flush()
        return RebuildResult(items=items, canonicals=len(groups), disputed=disputed)

    async def confirm(self, canonical_id, *, status: str) -> CanonicalProduct | None:
        canonical = await self._session.get(CanonicalProduct, canonical_id)
        if canonical is None:
            return None
        canonical.merge_status = status
        await self._session.flush()
        return canonical

    async def stats(self) -> dict[str, int]:
        rows = list(await self._session.scalars(select(CanonicalProduct)))
        return {
            "canonicals": len(rows),
            "auto": sum(1 for row in rows if row.merge_status == MergeStatus.auto.value),
            "confirmed": sum(
                1 for row in rows if row.merge_status == MergeStatus.confirmed.value
            ),
            "disputed": sum(
                1 for row in rows if row.merge_status == MergeStatus.disputed.value
            ),
            "sources": sum(row.sources_count for row in rows),
        }
