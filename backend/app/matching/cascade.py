"""Каскад подбора позиции заявки по каноническому слою.

Порядок ступеней — от точного к догадке, и он же определяет доверие к
результату: словарь синонимов (человек уже подтверждал) → артикул (совпало
однозначное поле) → вектор (похожесть наименования, pgvector) → обозначение
изделия («ОП-4», «К-01») → fuzzy (перестановки и опечатки). Первая ступень,
давшая результат выше порога, останавливает каскад: нижние ступени слабее, и
их «лучший» ответ не должен перебивать более надёжный.

Ступень обозначения стоит после вектора, а не до него, и это измерено на
голдсете: до вектора она перехватывала запросы, которые вектор разрешал точно,
и отправляла их менеджеру (top-1 0.867 против 0.933). Её место — подстраховка
там, где похожесть наименования не сработала.

Пороги из ТЗ: ≥ 0.85 — автоприём, 0.60…0.85 — топ-3 на подтверждение менеджеру,
< 0.60 — «не найдено». Значение порога не размазано по коду: оно здесь, потому
что от него зависит, что уйдёт в КП без человека.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from rapidfuzz import fuzz
from sqlalchemy import or_, select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.matching.embedder import active_embedder, build_embedder, cosine
from app.matching.variants import searchable_document_scope
from app.matching.text import (
    article_key,
    designations,
    normalize_name,
    significant_tokens,
)
from app.models import (
    CanonicalProduct,
    CanonicalProductSource,
    ItemEmbedding,
    MatchStage,
    PriceDocumentType,
    PriceVersion,
    Supplier,
    SupplierPriceItem,
    SynonymDictionary,
    VersionStatus,
)

AUTO_ACCEPT = 0.85
NEEDS_REVIEW = 0.60

# Похожее в каталоге есть, но выбрать по запросу нельзя: «ЗПУ запасное
# (ремкомплект)» без модели огнетушителя. Для менеджера это не «нет такого», а
# «уточни, что именно» — и путать эти два исхода нельзя.
NEEDS_CLARIFICATION = 0.40

TOP_N = 3

# Сколько кандидатов тянуть из базы на векторной ступени, прежде чем
# пересчитать и отсортировать. Больше — дороже, меньше — рискуем не увидеть
# правильный вариант из-за коллизий хеширования.
VECTOR_CANDIDATES = 50

# Верхняя граница выборки для fuzzy-ступени после грубого отсева по токенам.
FUZZY_POOL = 2_000

# Сколько канонов тянуть на ступени обозначения. Обозначение узкое, совпадений
# обычно единицы; предел защищает от запроса с одной буквой и цифрой.
# Пул кандидатов по обозначению. На боевом каталоге под одним обозначением
# лежат сотни строк («ОП-4» встречается у 257 канонов), и выборка без
# упорядочивания вытаскивала произвольные из них: настоящий огнетушитель мог
# в неё не попасть, а «кронштейн для ОУ-2» — попасть.
DESIGNATION_POOL = 2_000

# Потолок оценки, когда под обозначением найдено несколько исполнений: чуть
# ниже порога автоприёма, чтобы выбор ушёл менеджеру.
AMBIGUOUS_DESIGNATION = 0.84

# Слова, которые превращают позицию в принадлежность к изделию, а не в само
# изделие. Обозначение у них совпадает так же, как у прибора («кронштейн для
# ОУ-2» против огнетушителя ОУ-2), поэтому без этого различения ступень
# обозначений отвечает аксессуаром на запрос прибора.
ACCESSORY_MARKERS = frozenset(
    {
        "KPOHШTEЙH", "KPOHШT", "ЧEXOЛ", "ПOДCTABKA", "TEЛEЖKA", "TEЛEГA",
        "ШЛAHГ", "PACTPYБ", "MAKET", "ПOCOБИE", "ЗAГЛYШKA", "ПЛOMБA",
        "ЭTИKETKA", "ПACПOPT", "ПEPEЗAPЯДKA", "ЗAПPABKA", "ИCПЫTAHИE",
        "PEMOHT", "ЗИП", "MAHOMETP",
    }
)
ACCESSORY_PENALTY = 0.7


def _is_accessory_for(title_normalized: str, query_normalized: str) -> bool:
    """Кандидат — принадлежность, а запрос спрашивал не о ней."""
    return any(
        marker in title_normalized and marker not in query_normalized
        for marker in ACCESSORY_MARKERS
    )

# Версии, позиции которых участвуют в подборе. Снятый с публикации прайс
# отвечать на заявку не должен, даже если его векторы ещё в индексе.
SEARCHABLE_VERSION_STATUSES = (VersionStatus.active.value, VersionStatus.validated.value)


@dataclass(frozen=True, slots=True)
class Candidate:
    canonical_id: str
    title: str
    normalized_article: str | None
    merge_status: str
    stage: str
    score: float


@dataclass(frozen=True, slots=True)
class MatchResult:
    query: str
    decision: str  # auto | review | clarify | not_found
    stage: str
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


def _decision(score: float) -> str:
    if score >= AUTO_ACCEPT:
        return "auto"
    if score >= NEEDS_REVIEW:
        return "review"
    if score >= NEEDS_CLARIFICATION:
        return "clarify"
    return "not_found"


class MatchService:
    def __init__(self, session: AsyncSession, *, semantic=None) -> None:
        self._session = session
        self._semantic = semantic

    async def match(
        self,
        query: str,
        *,
        limit: int = TOP_N,
        supplier: str = "",
        project_id: uuid.UUID | None = None,
        check_batch_id: uuid.UUID | None = None,
    ) -> MatchResult:
        text = (query or "").strip()
        if not text:
            return MatchResult(query=query, decision="not_found", stage=MatchStage.none.value)

        scope = await self._scope(
            supplier=supplier,
            project_id=project_id,
            check_batch_id=check_batch_id,
        )
        best: list[Candidate] = []
        for stage in (
            self._by_synonym,
            self._by_article,
            self._by_vector,
            self._by_designation,
            self._by_semantic,
            self._by_fuzzy,
        ):
            candidates = await stage(text, limit, scope=scope)
            if candidates and (not best or candidates[0].score > best[0].score):
                best = candidates
            # Досрочный выход только на уверенном ответе. Раньше каскад
            # останавливался на первой же ступени, перешагнувшей порог
            # проверки, и слабое совпадение по вектору (0.68) выигрывало у
            # точного совпадения обозначения (0.84+) просто потому, что вектор
            # стоит раньше: «ШПК-320» отвечал шкафом ШС-К-310НЗК.
            if best and best[0].score >= AUTO_ACCEPT:
                break

        if best and best[0].score >= NEEDS_REVIEW:
            return MatchResult(
                query=text,
                decision=_decision(best[0].score),
                stage=best[0].stage,
                candidates=best[:limit],
            )

        # Ни одна ступень не дотянула до порога проверки. Если похожее всё же
        # нашлось, это запрос без модели, а не отсутствующий товар.
        if best and best[0].score >= NEEDS_CLARIFICATION:
            return MatchResult(
                query=text,
                decision="clarify",
                stage=best[0].stage,
                candidates=best[:limit],
            )
        return MatchResult(query=text, decision="not_found", stage=MatchStage.none.value)

    # ------------------------------------------------------------- ступени

    async def _scope(
        self,
        *,
        supplier: str = "",
        project_id: uuid.UUID | None = None,
        check_batch_id: uuid.UUID | None = None,
    ) -> set[uuid.UUID] | None:
        """Каноны, попадающие в область поиска: проект и/или поставщик.

        Проект — потому что прайсы в приложении разделены по проектам, и подбор
        обязан отвечать из того же набора, что показывает список позиций.
        `project_id=None` означает «искать по всем проектам» — это осознанный
        режим, а не отсутствие фильтра по недосмотру.

        Поставщик — потому что заявка часто ограничена одним прайсом («по
        Supplier A»), и чужая позиция с более высоким баллом здесь неверный
        ответ, а не лучший.
        """
        statement = (
            select(CanonicalProductSource.canonical_product_id)
            .join(
                SupplierPriceItem,
                SupplierPriceItem.id == CanonicalProductSource.price_item_id,
            )
            .join(PriceVersion, PriceVersion.id == SupplierPriceItem.price_version_id)
            .where(
                searchable_document_scope(check_batch_id),
            )
        )
        if supplier:
            statement = statement.join(
                Supplier, Supplier.id == CanonicalProductSource.supplier_id
            ).where(Supplier.name.ilike(f"%{supplier.strip()}%"))
        if project_id is not None:
            statement = statement.where(
                CanonicalProductSource.project_id == project_id
            )
        rows = await self._session.execute(statement)
        return {row[0] for row in rows}

    @staticmethod
    def _within(scope: set[uuid.UUID] | None, row: CanonicalProduct) -> bool:
        return scope is None or row.id in scope

    async def _by_synonym(self, query: str, limit: int, *, scope=None) -> list[Candidate]:
        """Ранее подтверждённый менеджером выбор — сильнее любой эвристики."""
        phrase = normalize_name(query)
        synonym = await self._session.scalar(
            select(SynonymDictionary).where(
                SynonymDictionary.normalized_phrase == phrase,
                SynonymDictionary.status == "active",
            )
        )
        if synonym is None:
            return []
        canonical = await self._session.get(CanonicalProduct, synonym.canonical_product_id)
        if canonical is None or not self._within(scope, canonical):
            return []
        synonym.usage_count += 1
        return [self._candidate(canonical, MatchStage.synonym.value, 1.0)]

    async def _by_article(self, query: str, limit: int, *, scope=None) -> list[Candidate]:
        article = article_key(query)
        if not article:
            return []
        rows = [
            row
            for row in await self._session.scalars(
                select(CanonicalProduct)
                .where(CanonicalProduct.normalized_article == article)
                .limit(DESIGNATION_POOL)
            )
            if self._within(scope, row)
        ]
        return [
            self._candidate(row, MatchStage.article.value, 0.95) for row in rows[:limit]
        ]

    async def _by_designation(
        self, query: str, limit: int, *, scope=None
    ) -> list[Candidate]:
        """Обозначение изделия внутри наименования: «ОП-4», «К-01», «ШПК-320».

        В прайсах заказчика артикул как отдельная колонка почти не встречается,
        а обозначение живёт прямо в названии. Ступень нужна между артикулом и
        вектором: совпадение обозначения — это факт, а не похожесть, и его
        нельзя проигрывать длинному общему слову вроде «огнетушитель».
        """
        wanted = designations(query)
        if not wanted:
            return []

        conditions = [
            CanonicalProduct.normalized_title.like(f"%{item}%") for item in wanted[:2]
        ]
        rows = [
            row
            for row in await self._session.scalars(
                select(CanonicalProduct)
                .where(or_(*conditions))
                # Сначала товары, у которых больше источников: они встречаются
                # у многих поставщиков, а значит это ходовая позиция, а не
                # единичная запись вроде учебного макета.
                .order_by(CanonicalProduct.sources_count.desc())
                .limit(DESIGNATION_POOL)
            )
            if self._within(scope, row)
        ]
        if not rows:
            return []

        query_tokens = set(significant_tokens(query))
        query_normalized = normalize_name(query)
        scored: list[tuple[float, CanonicalProduct]] = []
        for row in rows:
            title_designations = set(designations(row.normalized_title))
            matched = [item for item in wanted if item in title_designations]
            if not matched:
                continue
            base = 0.75 + 0.2 * (len(matched) / len(wanted))
            if query_tokens:
                overlap = len(query_tokens & set(significant_tokens(row.title))) / len(
                    query_tokens
                )
            else:
                overlap = 0.0
            score = base + 0.05 * overlap
            if _is_accessory_for(row.normalized_title, query_normalized):
                # Принадлежность к изделию — не изделие. «Кронштейн для ОУ-2»,
                # «чехол ОУ-3» и «учебное пособие (макет ОП-4)» содержат нужное
                # обозначение и по нему выигрывали у самого огнетушителя.
                # Отсутствие слов запроса в наименовании при этом ничего не
                # значит: у поставщиков позиция часто называется просто
                # «ОУ-3 ВСЕ d=114 мм», без слова «огнетушитель».
                score *= ACCESSORY_PENALTY
            scored.append((round(min(score, 0.99), 4), row))

        scored.sort(key=lambda pair: pair[0], reverse=True)

        # Одно обозначение — один товар: можно принимать автоматически. Если
        # под обозначением лежит несколько исполнений («ОП-4 (з) ВСЕ» и
        # «ОП-4 (з) АВСЕ»), выбор между ними — за менеджером, поэтому оценка
        # опускается под порог автоприёма.
        if len(scored) > 1:
            scored = [(min(score, AMBIGUOUS_DESIGNATION), row) for score, row in scored]

        return [
            self._candidate(row, MatchStage.designation.value, score)
            for score, row in scored[:limit]
        ]

    async def _by_vector(self, query: str, limit: int, *, scope=None) -> list[Candidate]:
        """Семантическая ступень.

        Ищем по индексу позиций прайсов (`item_embeddings`), а не по канону:
        индекс собирается по версиям, и только на позициях работают фильтры
        «искать в этих версиях этих поставщиков». Найденные позиции сводятся к
        каноническому товару — менеджеру показывается изделие, а не строка
        конкретного дилера.

        Индекс наполняется по версиям и почти всегда неполон: часть прайсов
        уже проиндексирована, часть ещё нет. Поэтому при слабом результате по
        индексу ступень до-проверяет канон и берёт лучший из двух наборов —
        иначе товар из непроиндексированного прайса выглядел бы отсутствующим.
        """
        vector = active_embedder().encode([query])[0]
        candidates = await self._vector_over_items(vector, limit, scope)
        if candidates and candidates[0].score >= NEEDS_REVIEW:
            return candidates

        fallback = await self._vector_over_canonicals(vector, limit, scope)
        if not candidates:
            return fallback
        if fallback and fallback[0].score > candidates[0].score:
            return fallback
        return candidates

    async def _vector_over_items(
        self,
        vector: list[float],
        limit: int,
        scope=None,
        *,
        model_name: str = "",
        stage: str = "",
    ) -> list[Candidate]:
        dialect = self._session.bind.dialect.name if self._session.bind else ""
        statement = (
            select(ItemEmbedding.supplier_price_item_id, ItemEmbedding.embedding)
            .join(PriceVersion, PriceVersion.id == ItemEmbedding.price_version_id)
            .where(
                ItemEmbedding.embedding.is_not(None),
                ItemEmbedding.model_name == (model_name or active_embedder().name),
                # Прайс, снятый с публикации, не должен всплывать в подборе,
                # даже если его векторы ещё лежат в индексе.
                PriceVersion.status.in_(SEARCHABLE_VERSION_STATUSES),
            )
        )
        if dialect == "postgresql":
            literal = "[" + ",".join(f"{value:.6f}" for value in vector) + "]"
            statement = statement.order_by(
                sa_text("item_embeddings.embedding <=> CAST(:query_vector AS vector)")
            ).params(query_vector=literal)
        statement = statement.limit(VECTOR_CANDIDATES)

        rows = list(await self._session.execute(statement))
        if not rows:
            return []

        scored_items = sorted(
            ((cosine(vector, list(embedding or [])), item_id) for item_id, embedding in rows),
            key=lambda pair: pair[0],
            reverse=True,
        )
        item_ids = [item_id for _, item_id in scored_items]
        links = {
            row[0]: row[1]
            for row in await self._session.execute(
                select(
                    CanonicalProductSource.price_item_id,
                    CanonicalProductSource.canonical_product_id,
                ).where(CanonicalProductSource.price_item_id.in_(item_ids))
            )
        }
        if not links:
            return []

        # Один канон покрывает позиции нескольких поставщиков: берём лучшую из
        # его позиций, иначе один и тот же товар занял бы весь топ-3.
        best_by_canonical: dict[uuid.UUID, float] = {}
        for score, item_id in scored_items:
            canonical_id = links.get(item_id)
            if canonical_id is None:
                continue
            if score > best_by_canonical.get(canonical_id, 0.0):
                best_by_canonical[canonical_id] = score

        top = [
            pair
            for pair in sorted(
                best_by_canonical.items(), key=lambda pair: pair[1], reverse=True
            )
            if scope is None or pair[0] in scope
        ][:limit]
        rows = {
            row.id: row
            for row in await self._session.scalars(
                select(CanonicalProduct).where(
                    CanonicalProduct.id.in_([canonical_id for canonical_id, _ in top])
                )
            )
        }
        return [
            self._candidate(
                rows[canonical_id], stage or MatchStage.vector.value, round(score, 4)
            )
            for canonical_id, score in top
            if canonical_id in rows and score > 0
        ]

    async def _vector_over_canonicals(
        self, vector: list[float], limit: int, scope=None
    ) -> list[Candidate]:
        dialect = self._session.bind.dialect.name if self._session.bind else ""
        statement = select(CanonicalProduct).where(CanonicalProduct.embedding.is_not(None))
        if dialect == "postgresql":
            literal = "[" + ",".join(f"{value:.6f}" for value in vector) + "]"
            statement = (
                statement.order_by(
                    sa_text("canonical_products.embedding <=> CAST(:query_vector AS vector)")
                )
                .params(query_vector=literal)
                .limit(VECTOR_CANDIDATES)
            )
        rows = [
            row for row in await self._session.scalars(statement) if self._within(scope, row)
        ]
        scored = [(cosine(vector, list(row.embedding or [])), row) for row in rows]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            self._candidate(row, MatchStage.vector.value, round(score, 4))
            for score, row in scored[:limit]
            if score > 0
        ]

    async def _by_semantic(self, query: str, limit: int, *, scope=None) -> list[Candidate]:
        """Смысловая ступень на локальной модели из архитектуры.

        Стоит после обозначения, и это результат замера, а не вкусовщина:
        модель понимает, что «спринклер» и «ороситель» — одно, но не различает
        «ОП-4» и «ОУ-3» (коды для неё почти шум). Как единственный сигнал она
        роняет точность вдвое, как дополнение — вытаскивает то, что не берут
        ни триграммы, ни обозначение.

        Включается настройкой `SEMANTIC_PROVIDER`; без неё ступень пропускается
        и подбор работает как раньше.
        """
        model = self._semantic if self._semantic is not None else self._semantic_embedder()
        if model is None:
            return []
        vector = model.encode([query])[0]
        return await self._vector_over_items(
            vector, limit, scope, model_name=model.name, stage=MatchStage.semantic.value
        )

    def _semantic_embedder(self):
        from app.config import settings

        provider = getattr(settings, "semantic_provider", "")
        if not provider:
            return None
        if self._semantic is None:
            self._semantic = build_embedder(provider)
        return self._semantic

    async def _by_fuzzy(self, query: str, limit: int, *, scope=None) -> list[Candidate]:
        """Последняя ступень: перестановки слов и опечатки.

        Кандидаты сначала грубо отсеиваются по значимым токенам: гонять
        rapidfuzz по всему канону (сотни тысяч записей) на каждый запрос
        незачем, а без отсева ступень становится самой дорогой в каскаде.
        """
        rows = [row for row in await self._fuzzy_pool(query) if self._within(scope, row)]
        target = normalize_name(query)
        wanted = set(significant_tokens(query))
        scored = [
            (self._fuzzy_score(target, wanted, row), row)
            for row in rows
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            self._candidate(row, MatchStage.fuzzy.value, round(score, 4))
            for score, row in scored[:limit]
        ]

    @staticmethod
    def _fuzzy_score(target: str, wanted: set[str], row: CanonicalProduct) -> float:
        """Похожесть строк, взвешенная покрытием значимых слов запроса.

        Голый `token_set_ratio` на каталоге в сотни тысяч позиций выдаёт 0.6 за
        одно общее слово: «сервер стоечный 2U» находил «профиль стоечный».
        Умножение на долю совпавших значимых токенов запроса убирает такие
        совпадения, не трогая случаи, где отличается только порядок слов.
        """
        ratio = fuzz.token_set_ratio(target, row.normalized_title) / 100.0
        if not wanted:
            return ratio
        found = set(significant_tokens(row.title or row.normalized_title))
        coverage = len(wanted & found) / len(wanted)
        return ratio * coverage

    async def _fuzzy_pool(self, query: str) -> list[CanonicalProduct]:
        significant = [token for token in significant_tokens(query) if len(token) >= 3]
        statement = select(CanonicalProduct)
        if significant:
            conditions = [
                CanonicalProduct.normalized_title.like(f"%{token}%")
                for token in significant[:3]
            ]
            statement = statement.where(or_(*conditions))
        return list(await self._session.scalars(statement.limit(FUZZY_POOL)))

    @staticmethod
    def _candidate(row: CanonicalProduct, stage: str, score: float) -> Candidate:
        return Candidate(
            canonical_id=str(row.id),
            title=row.title,
            normalized_article=row.normalized_article,
            merge_status=row.merge_status,
            stage=stage,
            score=score,
        )

    # ------------------------------------------------------------- обучение

    async def remember(self, phrase: str, canonical_id) -> SynonymDictionary:
        """Выбор менеджера превращается в синоним — обучение одним кликом.

        Цель — канон, а не строка конкретного прайса: одно подтверждение
        начинает работать сразу для всех поставщиков.
        """
        normalized = normalize_name(phrase)
        existing = await self._session.scalar(
            select(SynonymDictionary).where(
                SynonymDictionary.normalized_phrase == normalized
            )
        )
        if existing is not None:
            existing.canonical_product_id = canonical_id
            existing.status = "active"
            await self._session.flush()
            return existing
        synonym = SynonymDictionary(
            source_phrase=phrase.strip(),
            normalized_phrase=normalized,
            canonical_product_id=canonical_id,
            origin="manager_choice",
            status="active",
        )
        self._session.add(synonym)
        await self._session.flush()
        return synonym
