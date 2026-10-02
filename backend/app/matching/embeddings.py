"""Индекс эмбеддингов позиций: расширение при добавлении, пересборка при правке.

Вектор считается **по позиции прайса**, а не по каноническому товару: только
так работают фильтры «искать в этих версиях этих поставщиков», и только так
переиндексация одной версии не трогает остальные.

Две операции, и разница между ними принципиальная:

* **расширение** (`index_version`) — приехал новый прайс, его позиции
  досчитываются и добавляются к существующему индексу; чужие версии не
  трогаются;
* **пересборка** (`rebuild_version`) — прайс перезалит или переразобран, у
  версии старые векторы удаляются и считаются заново.

Внутри пересборки лишняя работа всё равно не делается: у каждой записи хранится
`text_hash` поискового текста, и совпадающие строки не пересчитываются. При
правке прайса меняется обычно десяток строк из тысячи.

Модель эмбеддера фиксируется в ключе (`model_name`, `model_revision`,
`normalizer_version`): смена модели не переписывает историю, а добавляет
параллельный набор векторов — старые кандидаты остаются объяснимыми.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matching.embedder import active_embedder
from app.matching.text import normalize_name
from app.models import (
    GlobalCategory,
    ItemEmbedding,
    PriceFile,
    PriceVersion,
    SupplierPriceItem,
)

# Версия нормализатора текста. Модель и её ревизия берутся у активного
# эмбеддера: смена модели меняет ключ записи, поэтому новые векторы ложатся
# рядом со старыми, а не поверх них.
NORMALIZER_VERSION = "v1"

CHUNK = 2_000


@dataclass(frozen=True, slots=True)
class IndexResult:
    version_id: str
    items: int
    inserted: int
    reused: int
    removed: int
    mode: str  # extend | rebuild


def search_text(
    *,
    name: str,
    article: str | None,
    category: str | None,
    attributes: str | None = None,
) -> str:
    """Текст, который уходит в эмбеддер.

    Формула из архитектуры: имя, артикул, категория, атрибуты. Цена сюда не
    входит намеренно — она не описывает идентичность товара, а меняется чаще
    всего, и её попадание в вектор заставляло бы переиндексировать прайс на
    каждом изменении цен.
    """
    parts = [normalize_name(name)]
    if article:
        parts.append(f"артикул:{normalize_name(article)}")
    if category:
        parts.append(f"категория:{normalize_name(category)}")
    if attributes:
        parts.append(f"атрибуты:{normalize_name(attributes)}")
    return " | ".join(parts)


def embedding_text(*, name: str, article: str | None) -> str:
    """Что именно уходит в вектор у текущего эмбеддера.

    Полный поисковый текст (с категорией и атрибутами) хранится рядом и нужен
    модели, которая понимает смысл. Триграммный вектор от лишних полей только
    портится: категория повторяется у тысяч позиций и размывает сходство,
    поэтому в него идут наименование и артикул.
    """
    parts = [normalize_name(name)]
    if article:
        parts.append(normalize_name(article))
    return " ".join(parts)


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class EmbeddingIndexService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def index_version(
        self, version_id: uuid.UUID, *, rebuild: bool = False, embedder=None
    ) -> IndexResult:
        """Досчитать индекс версии (расширение) либо пересобрать её целиком."""
        version = await self._session.get(PriceVersion, version_id)
        price_file = (
            await self._session.get(PriceFile, version.price_file_id) if version else None
        )
        if version is None:
            return IndexResult(str(version_id), 0, 0, 0, 0, "rebuild" if rebuild else "extend")

        removed = 0
        if rebuild:
            # Пересборка сносит записи версии целиком, но только текущей
            # модели: у другой модели свой набор векторов, и он не устарел.
            model = (embedder or active_embedder())
            result = await self._session.execute(
                delete(ItemEmbedding).where(
                    ItemEmbedding.price_version_id == version_id,
                    ItemEmbedding.model_name == model.name,
                    ItemEmbedding.model_revision == model.revision,
                )
            )
            removed = result.rowcount or 0

        embedder = embedder or active_embedder()
        existing: dict[uuid.UUID, str] = {}
        if not rebuild:
            rows = await self._session.execute(
                select(ItemEmbedding.supplier_price_item_id, ItemEmbedding.text_hash).where(
                    ItemEmbedding.price_version_id == version_id,
                    ItemEmbedding.model_name == embedder.name,
                    ItemEmbedding.model_revision == embedder.revision,
                    ItemEmbedding.normalizer_version == NORMALIZER_VERSION,
                )
            )
            existing = {row[0]: row[1] for row in rows}

        query = (
            select(
                SupplierPriceItem.id,
                SupplierPriceItem.source_name,
                SupplierPriceItem.normalized_name,
                SupplierPriceItem.source_article,
                SupplierPriceItem.normalized_article,
                SupplierPriceItem.category_path,
                GlobalCategory.name,
            )
            .join(
                GlobalCategory,
                GlobalCategory.id == SupplierPriceItem.manual_category_id,
                isouter=True,
            )
            .where(SupplierPriceItem.price_version_id == version_id)
        )

        pending: list[dict[str, object]] = []
        stale_items: list[uuid.UUID] = []
        items = 0
        reused = 0

        result = await self._session.stream(query.execution_options(yield_per=CHUNK))
        async for row in result:
            items += 1
            text = search_text(
                name=row.source_name or row.normalized_name or "",
                article=row.normalized_article or row.source_article,
                category=row[6] or row.category_path,
            )
            vector_text = embedding_text(
                name=row.source_name or row.normalized_name or "",
                article=row.normalized_article or row.source_article,
            )
            digest = text_hash(f"{text}\n{vector_text}")
            previous = existing.get(row.id)
            if previous == digest:
                reused += 1
                continue
            if previous is not None:
                # Строка изменилась: старый вектор удаляется, а не остаётся
                # вторым вариантом того же товара.
                stale_items.append(row.id)
            pending.append(
                {
                    "id": uuid.uuid4(),
                    "supplier_price_item_id": row.id,
                    "price_version_id": version_id,
                    "supplier_id": version.supplier_id,
                    "project_id": price_file.project_id if price_file else None,
                    "model_name": embedder.name,
                    "model_revision": embedder.revision,
                    "normalizer_version": NORMALIZER_VERSION,
                    "text_hash": digest,
                    "search_text": text,
                    # Вектор считается пачкой ниже: модель на CPU кодирует
                    # батчами на порядок быстрее, чем построчно.
                    "embedding": None,
                    "_vector_text": vector_text,
                }
            )

        for start in range(0, len(stale_items), CHUNK):
            batch = stale_items[start : start + CHUNK]
            result = await self._session.execute(
                delete(ItemEmbedding).where(
                    ItemEmbedding.supplier_price_item_id.in_(batch),
                    ItemEmbedding.model_name == embedder.name,
                    ItemEmbedding.model_revision == embedder.revision,
                    ItemEmbedding.normalizer_version == NORMALIZER_VERSION,
                )
            )
            removed += result.rowcount or 0

        for start in range(0, len(pending), CHUNK):
            batch = pending[start : start + CHUNK]
            vectors = embedder.encode([str(row.pop("_vector_text")) for row in batch])
            for row, vector in zip(batch, vectors):
                row["embedding"] = vector
            await self._session.execute(insert(ItemEmbedding), batch)

        await self._session.flush()
        return IndexResult(
            version_id=str(version_id),
            items=items,
            inserted=len(pending),
            reused=reused,
            removed=removed,
            mode="rebuild" if rebuild else "extend",
        )

    async def refresh_version(self, version_id: uuid.UUID) -> IndexResult | None:
        """Пересобрать версию, но только если она уже в индексе.

        Вызывается после повторного разбора прайса: если версию никто не
        индексировал, считать её векторы рано — она ещё не участвует в подборе.
        """
        indexed = await self._session.scalar(
            select(ItemEmbedding.id)
            .where(ItemEmbedding.price_version_id == version_id)
            .limit(1)
        )
        if indexed is None:
            return None
        return await self.index_version(version_id, rebuild=True)

    async def drop_version(self, version_id: uuid.UUID) -> int:
        """Убрать версию из индекса — например, когда она вытеснена новой."""
        result = await self._session.execute(
            delete(ItemEmbedding).where(ItemEmbedding.price_version_id == version_id)
        )
        await self._session.flush()
        return result.rowcount or 0

    async def stats(self) -> dict[str, object]:
        rows = await self._session.execute(
            select(ItemEmbedding.price_version_id, ItemEmbedding.model_name)
        )
        pairs = list(rows)
        return {
            "vectors": len(pairs),
            "versions": len({pair[0] for pair in pairs}),
            "models": sorted({pair[1] for pair in pairs}),
            "current_model": (
                f"{active_embedder().name}:{active_embedder().revision}/{NORMALIZER_VERSION}"
            ),
        }
