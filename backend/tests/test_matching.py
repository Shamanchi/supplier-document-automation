from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.matching.canonical import CanonicalService
from app.api.matching import _project_scope
from app.matching.cascade import AUTO_ACCEPT, NEEDS_REVIEW, MatchService
from app.matching.certificates import CertificateService
from app.matching.requests import parse_line, parse_text
from app.matching.text import cosine, designations, embed, normalize_article, normalize_name
from app.models import (
    Base,
    MergeStatus,
    PriceFile,
    PriceVersion,
    Project,
    Supplier,
    SupplierPriceItem,
)

PROJECT_ID = uuid.UUID("10000000-0000-4000-8000-000000000001")


def test_company_catalog_is_default_and_current_scope_is_explicit():
    project = Project(id=PROJECT_ID, name="Основной", slug="main")
    assert _project_scope({}, project) is None
    assert _project_scope({"project": "all"}, project) is None
    assert _project_scope({"project": "current"}, project) == PROJECT_ID


@asynccontextmanager
async def open_session(tmp_path):
    """Отдельная SQLite-база на тест: схема боевая, данных нет."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'matching.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as db:
            yield db
    finally:
        await engine.dispose()


async def make_version(db, *, supplier_name: str) -> PriceVersion:
    project = await db.get(Project, PROJECT_ID)
    if project is None:
        project = Project(id=PROJECT_ID, name="Основной", slug=f"main-{uuid.uuid4().hex[:6]}")
        db.add(project)
        await db.flush()
    supplier = Supplier(code=f"{uuid.uuid4().hex[:8]}", name=supplier_name)
    db.add(supplier)
    await db.flush()
    price_file = PriceFile(
        project_id=project.id,
        supplier_id=supplier.id,
        original_filename=f"{supplier_name}.xlsx",
        storage_key=f"{supplier_name}.xlsx",
        sha256=uuid.uuid4().hex,
        size_bytes=10,
        mime="application/vnd.ms-excel",
    )
    db.add(price_file)
    await db.flush()
    version = PriceVersion(
        supplier_id=supplier.id,
        price_file_id=price_file.id,
        version_no=1,
        status="active",
    )
    db.add(version)
    await db.flush()
    return version


async def add_item(db, version: PriceVersion, name: str, article: str | None = None) -> SupplierPriceItem:
    item = SupplierPriceItem(
        price_version_id=version.id,
        source_name=name,
        normalized_name=normalize_name(name),
        source_article=article,
        normalized_article=article,
        source_sheet="Лист1",
        source_row=1,
        created_at=datetime.now(timezone.utc),
    )
    db.add(item)
    await db.flush()
    return item


# ------------------------------------------------------------------ вектор


def test_embedding_is_stable_between_calls():
    """Хеш вектора не должен зависеть от запуска процесса — иначе индекс мусор."""
    assert embed("Огнетушитель ОП-5(з)") == embed("Огнетушитель ОП-5(з)")


def test_similar_names_are_closer_than_unrelated():
    op5 = embed("Огнетушитель ОП-5(з)-АВСЕ")
    op5_variant = embed("огнетушитель ОП 5 з АВСЕ")
    hose = embed("Рукав пожарный РПК-50 с гайками")

    assert cosine(op5, op5_variant) > cosine(op5, hose)


def test_homoglyphs_collapse_in_article():
    """«ОП» кириллицей и латиницей — один и тот же артикул."""
    assert normalize_article("ОП-5") == normalize_article("OП-5")


# ------------------------------------------------------------ разбор заявки


@pytest.mark.parametrize(
    ("line", "name", "quantity", "unit"),
    [
        ("Огнетушитель ОП-4 закачной — 25 шт", "Огнетушитель ОП-4 закачной", 25, "шт"),
        ("1. Рукав пожарный РПК-50, 10 м", "Рукав пожарный РПК-50", 10, "м"),
        ("Шкаф ШПК-320 2 компл", "Шкаф ШПК-320", 2, "компл"),
    ],
)
def test_parse_line_splits_quantity_and_unit(line, name, quantity, unit):
    parsed = parse_line(line)

    assert parsed.query_name == name
    assert parsed.quantity == Decimal(str(quantity))
    assert parsed.unit_code == unit


def test_parse_line_without_unit_assumes_pieces_and_warns():
    parsed = parse_line("Модуль МПП-2,5 - 4")

    assert parsed.quantity == Decimal("4")
    assert parsed.unit_code == "шт"
    assert any("единица не указана" in warning for warning in parsed.warnings)


def test_parse_line_reports_missing_quantity():
    parsed = parse_line("Ороситель спринклерный водяной")

    assert parsed.quantity is None
    assert any("количество не распознано" in warning for warning in parsed.warnings)


def test_parse_text_skips_empty_and_separator_lines():
    lines = parse_text("Огнетушитель ОП-4 — 2 шт\n\n-----\nРукав РПК-50 — 3 шт")

    assert [line.ordinal for line in lines] == [1, 2]


# ----------------------------------------------------------------- канон


def test_rebuild_merges_same_article_across_suppliers(tmp_path):
    async def exercise():
        async with open_session(tmp_path) as session:
            first = await make_version(session, supplier_name="Supplier A")
            second = await make_version(session, supplier_name="Supplier E")
            await add_item(session, first, "Огнетушитель ОП-5(з)-АВСЕ", "ОП-5")
            await add_item(session, second, "ОГНЕТУШИТЕЛЬ ОП 5 з АВСЕ", "оп-5")

            result = await CanonicalService(session).rebuild()

            assert result.items == 2
            assert result.canonicals == 1

    asyncio.run(exercise())


def test_rebuild_marks_disputed_when_names_disagree(tmp_path):
    async def exercise():
        async with open_session(tmp_path) as session:
            version = await make_version(session, supplier_name="Supplier A")
            await add_item(session, version, "Огнетушитель порошковый ОП-5", "X-100")
            await add_item(session, version, "Кабель силовой ВВГнг 3х2.5", "X-100")

            result = await CanonicalService(session).rebuild()

            assert result.canonicals == 1
            assert result.disputed == 1
            stats = await CanonicalService(session).stats()
            assert stats["disputed"] == 1

    asyncio.run(exercise())


def test_rebuild_is_idempotent(tmp_path):
    async def exercise():
        async with open_session(tmp_path) as session:
            version = await make_version(session, supplier_name="Supplier C")
            await add_item(session, version, "Ороситель СВН-15", "СВН-15")

            first = await CanonicalService(session).rebuild()
            second = await CanonicalService(session).rebuild()

            assert (first.items, first.canonicals) == (second.items, second.canonicals)

    asyncio.run(exercise())


# ---------------------------------------------------------------- каскад


def test_article_stage_beats_vector(tmp_path):
    async def exercise():
        async with open_session(tmp_path) as session:
            version = await make_version(session, supplier_name="Supplier A")
            await add_item(session, version, "Огнетушитель ОП-5(з)-АВСЕ", "ОП-5")
            await add_item(session, version, "Огнетушитель ОП-4(з)-АВСЕ", "ОП-4")
            await CanonicalService(session).rebuild()

            result = await MatchService(session).match("ОП-5")

            assert result.stage == "article"
            assert result.decision == "auto"
            assert "ОП-5" in result.best.title

    asyncio.run(exercise())


def test_vector_stage_finds_reworded_name(tmp_path):
    async def exercise():
        async with open_session(tmp_path) as session:
            version = await make_version(session, supplier_name="Supplier A")
            await add_item(session, version, "Рукав пожарный напорный РПК-50 с головками")
            await CanonicalService(session).rebuild()

            result = await MatchService(session).match("напорный пожарный рукав РПК-50")

            assert result.best is not None
            assert result.stage in {"designation", "vector", "fuzzy"}
            assert result.decision in {"auto", "review"}

    asyncio.run(exercise())


def test_unrelated_query_is_not_found(tmp_path):
    async def exercise():
        async with open_session(tmp_path) as session:
            version = await make_version(session, supplier_name="Supplier A")
            await add_item(session, version, "Огнетушитель ОП-5(з)-АВСЕ", "ОП-5")
            await CanonicalService(session).rebuild()

            result = await MatchService(session).match("сервер стоечный 2U 64 ГБ памяти")

            assert result.decision == "not_found"
            assert result.stage == "none"

    asyncio.run(exercise())


def test_thresholds_follow_specification():
    """Пороги — часть контракта с ТЗ, а не деталь реализации."""
    assert AUTO_ACCEPT == 0.85
    assert NEEDS_REVIEW == 0.60


def test_manager_choice_becomes_synonym(tmp_path):
    async def exercise():
        async with open_session(tmp_path) as session:
            version = await make_version(session, supplier_name="Supplier A")
            await add_item(session, version, "Ороситель спринклерный СВН-15", "СВН-15")
            await CanonicalService(session).rebuild()
            matcher = MatchService(session)
            candidate = (await matcher.match("СВН-15")).best

            await matcher.remember("спринклер водяной низкий", uuid.UUID(candidate.canonical_id))
            result = await matcher.match("Спринклер водяной низкий")

            assert result.stage == "synonym"
            assert result.decision == "auto"
            assert result.best.canonical_id == candidate.canonical_id

    asyncio.run(exercise())


# ----------------------------------------------------------- сертификаты


def test_certificates_absent_service_is_not_an_error(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "fsa_base_url", "", raising=False)

    async def exercise():
        async with open_session(tmp_path) as session:
            info = await CertificateService(session).for_product(product="ОП-5")

            assert info.available is False
            assert info.attachable is False
            assert "не настроен" in info.reason_unavailable

    asyncio.run(exercise())


def test_certificate_is_attachable_only_when_confirmed(tmp_path):
    async def exercise():
        async with open_session(tmp_path) as session:
            from app.matching.certificates import CertificateInfo

            unconfirmed = CertificateInfo(
                available=True, verdict="valid", needs_manual_review=False
            )
            confirmed = CertificateInfo(
                available=True,
                verdict="valid",
                needs_manual_review=False,
                document_confirmed_by="Иванов",
            )
            expired = CertificateInfo(
                available=True,
                verdict="expired",
                needs_manual_review=False,
                document_confirmed_by="Иванов",
            )

            assert unconfirmed.attachable is False
            assert confirmed.attachable is True
            assert expired.attachable is False

    asyncio.run(exercise())


def test_saved_link_carries_evidence(tmp_path):
    async def exercise():
        async with open_session(tmp_path) as session:
            from app.matching.certificates import CertificateInfo

            version = await make_version(session, supplier_name="Supplier A")
            await add_item(session, version, "Огнетушитель ОП-5(з)-АВСЕ", "ОП-5")
            await CanonicalService(session).rebuild()
            canonical = (await MatchService(session).match("ОП-5")).best
            from app.models import CanonicalProduct

            row = await session.get(CanonicalProduct, uuid.UUID(canonical.canonical_id))

            link = await CertificateService(session).save_link(
                canonical=row,
                info=CertificateInfo(
                    available=True,
                    verdict="valid",
                    registration_number="ЕАЭС RU С-RU.ПБ97.В.00272/21",
                    valid_until="2026-11-14",
                    needs_manual_review=False,
                    document_confirmed_by="Иванов",
                ),
                matched_key="ОП-5",
            )

            assert link is not None
            assert link.matched_by == "registry"
            assert link.evidence["registration_number"] == "ЕАЭС RU С-RU.ПБ97.В.00272/21"
            assert link.evidence["confirmed_by"] == "Иванов"
            assert link.confirmed_by == "Иванов"

    asyncio.run(exercise())


def test_link_is_not_saved_without_certificate(tmp_path):
    async def exercise():
        async with open_session(tmp_path) as session:
            from app.matching.certificates import UNAVAILABLE
            from app.models import CanonicalProduct

            version = await make_version(session, supplier_name="Supplier A")
            await add_item(session, version, "Ороситель СВН-15", "СВН-15")
            await CanonicalService(session).rebuild()
            row = (await session.scalars(__import__("sqlalchemy").select(CanonicalProduct))).first()

            assert await CertificateService(session).save_link(canonical=row, info=UNAVAILABLE) is None

    asyncio.run(exercise())


def test_long_names_do_not_overflow_match_key():
    """Услуги описывают абзацем — ключ слияния обязан оставаться в колонке."""
    from app.matching.canonical import MAX_KEY_LENGTH, match_key_for

    class _Item:
        normalized_article = None
        source_article = None
        normalized_name = "Переосвидетельствование огнетушителей " * 20
        source_name = normalized_name

    key, how = match_key_for(_Item())

    assert how == "name"
    assert len(key) <= MAX_KEY_LENGTH


# ------------------------------------------------------- индекс эмбеддингов


def test_index_extends_without_touching_other_versions(tmp_path):
    """Новый прайс расширяет индекс, чужие версии не пересчитываются."""

    async def exercise():
        async with open_session(tmp_path) as session:
            from app.matching.embeddings import EmbeddingIndexService

            first = await make_version(session, supplier_name="Supplier A")
            await add_item(session, first, "Огнетушитель ОП-4(з)-АВСЕ", "ОП-4")
            second = await make_version(session, supplier_name="Supplier E")
            await add_item(session, second, "Рукав пожарный РПК-50")

            service = EmbeddingIndexService(session)
            built_first = await service.index_version(first.id)
            built_second = await service.index_version(second.id)

            assert built_first.mode == "extend"
            assert (built_first.inserted, built_second.inserted) == (1, 1)
            stats = await service.stats()
            assert stats["vectors"] == 2
            assert stats["versions"] == 2

    asyncio.run(exercise())


def test_repeated_index_reuses_unchanged_rows(tmp_path):
    """Повторный проход по неизменённому прайсу не пересчитывает векторы."""

    async def exercise():
        async with open_session(tmp_path) as session:
            from app.matching.embeddings import EmbeddingIndexService

            version = await make_version(session, supplier_name="Supplier C")
            await add_item(session, version, "Ороситель СВН-15", "СВН-15")
            service = EmbeddingIndexService(session)

            first = await service.index_version(version.id)
            second = await service.index_version(version.id)

            assert first.inserted == 1
            assert second.inserted == 0
            assert second.reused == 1

    asyncio.run(exercise())


def test_changed_row_replaces_its_vector(tmp_path):
    """Изменилась строка прайса — старый вектор уходит, новый встаёт."""

    async def exercise():
        async with open_session(tmp_path) as session:
            from app.matching.embeddings import EmbeddingIndexService

            version = await make_version(session, supplier_name="Supplier C")
            item = await add_item(session, version, "Ороситель СВН-15", "СВН-15")
            service = EmbeddingIndexService(session)
            await service.index_version(version.id)

            item.source_name = "Ороситель спринклерный СВН-15 универсальный"
            item.normalized_name = normalize_name(item.source_name)
            await session.flush()
            second = await service.index_version(version.id)

            assert second.inserted == 1
            assert second.removed == 1
            stats = await service.stats()
            assert stats["vectors"] == 1

    asyncio.run(exercise())


def test_rebuild_drops_vectors_of_removed_rows(tmp_path):
    """Пересборка версии убирает векторы исчезнувших строк."""

    async def exercise():
        async with open_session(tmp_path) as session:
            from sqlalchemy import delete as sa_delete

            from app.matching.embeddings import EmbeddingIndexService
            from app.models import ItemEmbedding, SupplierPriceItem as Item

            version = await make_version(session, supplier_name="Supplier A")
            keep = await add_item(session, version, "Огнетушитель ОП-4(з)", "ОП-4")
            drop = await add_item(session, version, "Огнетушитель ОП-8(з)", "ОП-8")
            service = EmbeddingIndexService(session)
            await service.index_version(version.id)

            await session.execute(sa_delete(Item).where(Item.id == drop.id))
            await session.flush()
            rebuilt = await service.index_version(version.id, rebuild=True)

            assert rebuilt.mode == "rebuild"
            assert rebuilt.items == 1
            assert rebuilt.inserted == 1
            remaining = list(await session.scalars(select_all(ItemEmbedding)))
            assert [row.supplier_price_item_id for row in remaining] == [keep.id]

    asyncio.run(exercise())


def select_all(model):
    from sqlalchemy import select

    return select(model)


def test_refresh_skips_version_that_was_never_indexed(tmp_path):
    """Переразбор неиндексированной версии не запускает счёт векторов."""

    async def exercise():
        async with open_session(tmp_path) as session:
            from app.matching.embeddings import EmbeddingIndexService

            version = await make_version(session, supplier_name="Supplier E")
            await add_item(session, version, "Шкаф ШПК-320")

            assert await EmbeddingIndexService(session).refresh_version(version.id) is None

    asyncio.run(exercise())


def test_search_text_excludes_price_and_keeps_article(tmp_path):
    """Цена в вектор не входит: она не описывает идентичность товара."""
    from app.matching.embeddings import search_text

    text = search_text(name="Огнетушитель ОП-4", article="ОП-4", category="огнетушители")

    assert "артикул:" in text
    assert "категория:" in text
    assert "цена" not in text.lower()


def test_vector_stage_uses_item_index(tmp_path):
    """Подбор идёт по индексу позиций и сводит найденное к канону."""

    async def exercise():
        async with open_session(tmp_path) as session:
            from app.matching.embeddings import EmbeddingIndexService

            version = await make_version(session, supplier_name="Supplier A")
            await add_item(session, version, "Рукав пожарный напорный РПК-50 с головками")
            await CanonicalService(session).rebuild()
            await EmbeddingIndexService(session).index_version(version.id)

            result = await MatchService(session).match("напорный пожарный рукав РПК-50")

            assert result.best is not None
            assert result.stage in {"designation", "vector", "fuzzy"}

    asyncio.run(exercise())


def test_fuzzy_does_not_match_on_a_single_shared_word(tmp_path):
    """«Сервер стоечный» не должен находить «профиль стоечный»."""

    async def exercise():
        async with open_session(tmp_path) as session:
            version = await make_version(session, supplier_name="Supplier E")
            await add_item(session, version, "Профиль 100х50 3м стоечный (0,45мм)")
            await CanonicalService(session).rebuild()

            result = await MatchService(session).match("сервер стоечный 2U 64 ГБ")

            assert result.decision == "not_found"

    asyncio.run(exercise())


def test_partial_index_does_not_hide_unindexed_prices(tmp_path):
    """Проиндексирована одна версия — товар из другой всё равно находится."""

    async def exercise():
        async with open_session(tmp_path) as session:
            from app.matching.embeddings import EmbeddingIndexService

            indexed = await make_version(session, supplier_name="Supplier A")
            await add_item(session, indexed, "Огнетушитель ОП-4(з)-АВСЕ", "ОП-4")
            fresh = await make_version(session, supplier_name="Supplier E")
            await add_item(session, fresh, "Рукав пожарный напорный РПК-50 с головками")
            await CanonicalService(session).rebuild()
            await EmbeddingIndexService(session).index_version(indexed.id)

            result = await MatchService(session).match("напорный пожарный рукав РПК-50")

            assert result.best is not None
            assert "РПК-50" in result.best.title

    asyncio.run(exercise())


def test_designation_stage_wins_over_long_common_word(tmp_path):
    """«ОП-4» важнее слова «огнетушитель», которое есть у половины каталога."""

    async def exercise():
        async with open_session(tmp_path) as session:
            version = await make_version(session, supplier_name="Supplier A")
            await add_item(session, version, "ОП-4 (з) ВСЕ ЗПУ Россия")
            await add_item(session, version, "Подставка под огнетушитель П-15")
            await add_item(session, version, "Шар-Огнетушитель AFO заряд 0,5 кг")
            await CanonicalService(session).rebuild()

            result = await MatchService(session).match("Огнетушитель ОП-4 закачной")

            assert result.stage in {"vector", "designation"}
            assert "ОП-4" in result.best.title

    asyncio.run(exercise())


def test_several_variants_under_one_designation_go_to_manager(tmp_path):
    """Под «ОП-4» лежат разные исполнения — автоприём здесь запрещён."""

    async def exercise():
        async with open_session(tmp_path) as session:
            version = await make_version(session, supplier_name="Supplier A")
            await add_item(session, version, "ОП-4 (з) ВСЕ ЗПУ Россия")
            await add_item(session, version, "ОП-4 (з) АВСЕ ЗПУ Китай")
            await CanonicalService(session).rebuild()

            result = await MatchService(session).match("ОП-4 закачной")

            assert result.decision in {"review", "clarify"}
            assert len(result.candidates) >= 2

    asyncio.run(exercise())


def test_generic_query_asks_for_clarification(tmp_path):
    """Родовой запрос без модели — «уточните», а не «не найдено»."""

    async def exercise():
        async with open_session(tmp_path) as session:
            version = await make_version(session, supplier_name="Supplier A")
            await add_item(session, version, "Ремкомплект гидропульта Профи")
            await add_item(session, version, "Ремкомплект гидропульта Ермак")
            await CanonicalService(session).rebuild()

            result = await MatchService(session).match("ЗПУ запасное (ремкомплект)")

            assert result.decision == "clarify"
            assert result.candidates

    asyncio.run(exercise())


def test_absent_product_is_not_found_not_clarify(tmp_path):
    """Товара нет в прайсах — это «не найдено», а не «уточните»."""

    async def exercise():
        async with open_session(tmp_path) as session:
            version = await make_version(session, supplier_name="Supplier A")
            await add_item(session, version, "Огнетушитель ОП-4 (з) ВСЕ")
            await CanonicalService(session).rebuild()

            result = await MatchService(session).match(
                "Тепловизор Keysight U5857A -20…+1200°C"
            )

            assert result.decision == "not_found"

    asyncio.run(exercise())


def test_supplier_scope_limits_candidates(tmp_path):
    """Заявка «по прайсу Supplier A» не должна отвечать чужой позицией."""

    async def exercise():
        async with open_session(tmp_path) as session:
            first = await make_version(session, supplier_name="Supplier A")
            second = await make_version(session, supplier_name="Supplier B")
            await add_item(session, first, "ОП-5 (з) ВСЕ ЗПУ Россия")
            await add_item(session, second, "ОП-5 (з) ВСЕ ЗПУ РФ")
            await CanonicalService(session).rebuild()

            scoped = await MatchService(session).match("ОП-5 (з) ВСЕ", supplier="Supplier B")

            assert scoped.best is not None
            assert "РФ" in scoped.best.title

    asyncio.run(exercise())


# ------------------------------------------------------------------ варианты


async def add_priced_item(db, version, name, amount, article=None):
    from decimal import Decimal

    from app.models import SupplierPriceValue

    item = await add_item(db, version, name, article)
    db.add(
        SupplierPriceValue(
            item_id=item.id,
            price_kind="base",
            amount=Decimal(str(amount)),
            currency="RUB",
        )
    )
    await db.flush()
    return item


def test_variants_return_whole_designation_row(tmp_path):
    """По «ОП-4 закачной» менеджер видит весь ряд ОП-4, а не тройку лучших."""

    async def exercise():
        async with open_session(tmp_path) as session:
            from app.matching.variants import VariantService

            first = await make_version(session, supplier_name="Supplier A")
            second = await make_version(session, supplier_name="Supplier B")
            await add_priced_item(session, first, "ОП-4 (з) ВСЕ ЗПУ Россия", 460)
            await add_priced_item(session, first, "ОП-4 (з) АВСЕ ЗПУ Китай", 448)
            await add_priced_item(session, first, "ОП-4 (з) АВСЕ Премиум красный", 512)
            await add_priced_item(session, second, "ОП-4 (з) ВСЕ ЗПУ РФ", 455)
            await add_priced_item(session, first, "ОП-5 (з) ВСЕ ЗПУ Россия", 509)
            await CanonicalService(session).rebuild()

            found, variants = await VariantService(session).by_query(
                "Огнетушитель ОП-4 закачной"
            )

            assert found == designations("ОП-4")
            titles = [variant.title for variant in variants]
            assert len(titles) == 4
            assert all("ОП-4" in title for title in titles)
            assert not any("ОП-5" in title for title in titles)

    asyncio.run(exercise())


def test_variants_carry_supplier_and_price(tmp_path):
    """Ряд бесполезен без цены и поставщика — выбирать не из чего."""

    async def exercise():
        async with open_session(tmp_path) as session:
            from app.matching.variants import VariantService

            version = await make_version(session, supplier_name="Supplier A")
            await add_priced_item(session, version, "ОП-4 (з) ВСЕ ЗПУ Россия", 460)
            await CanonicalService(session).rebuild()

            _, variants = await VariantService(session).by_query("ОП-4")

            offers = variants[0].offers
            assert offers[0].supplier == "Supplier A"
            assert float(offers[0].price) == 460.0

    asyncio.run(exercise())


def test_variants_do_not_mix_similar_designations(tmp_path):
    """«ОП-4» не должен притягивать «ОП-40»: это другой товар, а не исполнение."""

    async def exercise():
        async with open_session(tmp_path) as session:
            from app.matching.variants import VariantService

            version = await make_version(session, supplier_name="Supplier A")
            await add_priced_item(session, version, "ОП-4 (з) ВСЕ ЗПУ Россия", 460)
            await add_priced_item(session, version, "ОП-40 (з) ВСЕ", 9800)
            await CanonicalService(session).rebuild()

            _, variants = await VariantService(session).by_query("ОП-4 закачной")

            titles = [variant.title for variant in variants]
            assert titles == ["ОП-4 (з) ВСЕ ЗПУ Россия"]

    asyncio.run(exercise())


def test_variants_respect_supplier_scope(tmp_path):
    """Заявка по прайсу одного поставщика не должна показывать чужие цены."""

    async def exercise():
        async with open_session(tmp_path) as session:
            from app.matching.variants import VariantService

            first = await make_version(session, supplier_name="Supplier A")
            second = await make_version(session, supplier_name="Supplier B")
            await add_priced_item(session, first, "ОП-4 (з) ВСЕ ЗПУ Россия", 460)
            await add_priced_item(session, second, "ОП-4 (з) ВСЕ ЗПУ РФ", 455)
            await CanonicalService(session).rebuild()

            _, variants = await VariantService(session).by_query(
                "ОП-4", supplier="Supplier B"
            )

            suppliers = {offer.supplier for variant in variants for offer in variant.offers}
            assert suppliers == {"Supplier B"}

    asyncio.run(exercise())


def test_variants_empty_without_designation(tmp_path):
    """Без обозначения ряд не собрать — это работа каскада, а не выборки."""

    async def exercise():
        async with open_session(tmp_path) as session:
            from app.matching.variants import VariantService

            version = await make_version(session, supplier_name="Supplier A")
            await add_priced_item(session, version, "Порошок Триумф АВСЕ", 120)
            await CanonicalService(session).rebuild()

            found, variants = await VariantService(session).by_query("порошок огнетушащий")

            assert found == []
            assert variants == []

    asyncio.run(exercise())


# ----------------------------------------------------------- проектная область


async def make_project(db, name: str) -> "Project":
    from app.models import Project as ProjectModel

    project = ProjectModel(
        id=uuid.uuid4(), name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:6]}"
    )
    db.add(project)
    await db.flush()
    return project


async def make_version_in(db, project, *, supplier_name: str):
    """Версия прайса в конкретном проекте."""
    from app.models import PriceFile as File, PriceVersion as Version, Supplier as Sup

    supplier = Sup(code=uuid.uuid4().hex[:8], name=supplier_name)
    db.add(supplier)
    await db.flush()
    price_file = File(
        project_id=project.id,
        supplier_id=supplier.id,
        original_filename=f"{supplier_name}.xlsx",
        storage_key=f"{supplier_name}-{uuid.uuid4().hex[:6]}.xlsx",
        sha256=uuid.uuid4().hex,
        size_bytes=10,
        mime="application/vnd.ms-excel",
    )
    db.add(price_file)
    await db.flush()
    version = Version(
        supplier_id=supplier.id,
        price_file_id=price_file.id,
        version_no=1,
        status="active",
    )
    db.add(version)
    await db.flush()
    return version


def test_match_is_limited_to_its_project(tmp_path):
    """Подбор в проекте не должен отвечать позицией чужого проекта."""

    async def exercise():
        async with open_session(tmp_path) as session:
            first = await make_project(session, "Основной")
            second = await make_project(session, "Тендерный")
            await add_item(
                session,
                await make_version_in(session, first, supplier_name="Supplier A"),
                "ОП-4 (з) ВСЕ ЗПУ Россия",
            )
            await add_item(
                session,
                await make_version_in(session, second, supplier_name="Supplier B"),
                "ОП-4 (з) ВСЕ ЗПУ РФ",
            )
            await CanonicalService(session).rebuild()

            scoped = await MatchService(session).match("ОП-4", project_id=second.id)

            assert scoped.best is not None
            assert "РФ" in scoped.best.title

    asyncio.run(exercise())


def test_match_across_all_projects(tmp_path):
    """Режим «по всем проектам» видит оба прайса."""

    async def exercise():
        async with open_session(tmp_path) as session:
            first = await make_project(session, "Основной")
            second = await make_project(session, "Тендерный")
            await add_item(
                session,
                await make_version_in(session, first, supplier_name="Supplier A"),
                "ОП-4 (з) ВСЕ ЗПУ Россия",
            )
            await add_item(
                session,
                await make_version_in(session, second, supplier_name="Supplier B"),
                "ОП-4 (з) ВСЕ ЗПУ РФ",
            )
            await CanonicalService(session).rebuild()

            from app.matching.variants import VariantService

            _, everywhere = await VariantService(session).by_query("ОП-4")
            _, scoped = await VariantService(session).by_query("ОП-4", project_id=first.id)

            assert len(everywhere) == 2
            assert len(scoped) == 1

    asyncio.run(exercise())


def test_canonical_sources_remember_their_project(tmp_path):
    """Проект дублируется в источники канона — иначе фильтр стоил бы join."""

    async def exercise():
        async with open_session(tmp_path) as session:
            from sqlalchemy import select as sa_select

            from app.models import CanonicalProductSource

            project = await make_project(session, "Основной")
            version = await make_version_in(session, project, supplier_name="Supplier C")
            await add_item(session, version, "Ороситель СВН-15")
            await CanonicalService(session).rebuild()

            rows = list(await session.scalars(sa_select(CanonicalProductSource)))

            assert rows
            assert all(row.project_id == project.id for row in rows)

    asyncio.run(exercise())


def test_background_rebuild_returns_task_id(monkeypatch):
    """Пересборка канона по умолчанию уходит в очередь, а не в HTTP-запрос.

    Синхронный ответ на минутную операцию не доживает до конца: обратный
    прокси рвёт соединение, запрос отменяется вместе с транзакцией, и слой
    остаётся пустым — ровно это и случилось на боевом каталоге.
    """
    import app.api.matching as api

    class FakeTask:
        id = "task-1"

    monkeypatch.setattr(api.rebuild_canonical_layer, "delay", lambda *a, **k: FakeTask())
    payload = asyncio.run(api.rebuild_canonical(limit=0, background=True, session=None, _admin=None))
    assert payload == {"mode": "background", "task_id": "task-1"}


def test_index_rebuild_is_background_only(monkeypatch):
    import app.api.matching as api

    class FakeTask:
        id = "task-2"

    monkeypatch.setattr(api.rebuild_search_index, "delay", lambda *a, **k: FakeTask())
    payload = asyncio.run(api.rebuild_index(rebuild=True, _admin=None))
    assert payload["mode"] == "background"
    assert payload["task_id"] == "task-2"


def test_accessory_does_not_outrank_the_device(tmp_path):
    """«Кронштейн для ОУ-2» не должен быть ответом на запрос огнетушителя.

    Обозначение у принадлежности совпадает с обозначением прибора, и по одному
    только совпадению кода кронштейн выигрывал у самого огнетушителя — ровно
    это и происходило на боевом каталоге.
    """

    async def scenario():
        async with open_session(tmp_path) as db:
            version = await make_version(db, supplier_name="Supplier A")
            await add_item(db, version, "Огнетушитель ОУ-2 ВСЕ d=108 мм")
            await add_item(db, version, "Кроншт. ТГ2 транспортный (для горизонт. крепл. ОУ-2)")
            await add_item(db, version, "Чехол ОУ-2 140*640 мм")
            await CanonicalService(db).rebuild()
            await db.commit()

            result = await MatchService(db).match("Огнетушитель ОУ-2")
            assert result.best is not None
            assert "КРОНШТ" not in result.best.title.upper()
            assert "ЧЕХОЛ" not in result.best.title.upper()

    asyncio.run(scenario())


def test_accessory_is_found_when_it_is_what_was_asked(tmp_path):
    """Если менеджер просит именно чехол, штраф не должен ему мешать."""

    async def scenario():
        async with open_session(tmp_path) as db:
            version = await make_version(db, supplier_name="Supplier A")
            await add_item(db, version, "Огнетушитель ОУ-2 ВСЕ d=108 мм")
            await add_item(db, version, "Чехол ОУ-2 140*640 мм")
            await CanonicalService(db).rebuild()
            await db.commit()

            result = await MatchService(db).match("Чехол ОУ-2")
            assert result.best is not None
            assert "ЧЕХОЛ" in result.best.title.upper()

    asyncio.run(scenario())


# ------------------------------------- ценовые документы из сделки (этап 7)
#
# Общий прайс (`PRICE_LIST`) виден всем заявкам, разовый счёт/КП
# (`SUPPLIER_QUOTE`) — только той, ради которой он загружен. Снимок цены в
# строке расчёта фиксирует источник: активация нового прайса не переписывает
# уже собранное КП.


async def make_document_version(
    db, *, supplier_name: str, document_type: str, check_batch_id=None
):
    project = await db.get(Project, PROJECT_ID)
    if project is None:
        project = Project(
            id=PROJECT_ID, name="Основной", slug=f"main-{uuid.uuid4().hex[:6]}"
        )
        db.add(project)
        await db.flush()
    supplier = Supplier(code=uuid.uuid4().hex[:8], name=supplier_name)
    db.add(supplier)
    await db.flush()
    price_file = PriceFile(
        project_id=project.id,
        supplier_id=supplier.id,
        original_filename=f"{supplier_name}.pdf",
        storage_key=f"{supplier_name}-{uuid.uuid4().hex[:6]}.pdf",
        sha256=uuid.uuid4().hex,
        size_bytes=10,
        mime="application/pdf",
        source="BITRIX",
    )
    db.add(price_file)
    await db.flush()
    version = PriceVersion(
        supplier_id=supplier.id,
        price_file_id=price_file.id,
        version_no=1,
        status=(
            "validated"
            if document_type == "SUPPLIER_QUOTE"
            else "active"
        ),
        document_type=document_type,
        check_batch_id=check_batch_id,
    )
    db.add(version)
    await db.flush()
    return version


def test_price_list_is_visible_to_every_batch(tmp_path):
    """Общий прайс участвует в подборе и без контекста заявки, и внутри неё."""

    async def exercise():
        async with open_session(tmp_path) as session:
            from app.matching.variants import VariantService
            from app.models import PriceDocumentType

            version = await make_document_version(
                session,
                supplier_name="Supplier A",
                document_type=PriceDocumentType.price_list.value,
            )
            await add_priced_item(session, version, "ОП-4 (з) ВСЕ ЗПУ Россия", 460)
            await CanonicalService(session).rebuild()

            _, scoped = await VariantService(session).by_query(
                "ОП-4", check_batch_id=uuid.uuid4()
            )
            _, everywhere = await VariantService(session).by_query("ОП-4")

            assert any(variant.offers for variant in scoped)
            assert any(variant.offers for variant in everywhere)

    asyncio.run(exercise())


def test_supplier_quote_is_scoped_to_its_batch(tmp_path):
    """Разовый счёт виден только в своей заявке, но не в чужой и не в общем списке."""

    async def exercise():
        async with open_session(tmp_path) as session:
            from app.matching.variants import VariantService
            from app.models import PriceDocumentType

            own = uuid.uuid4()
            other = uuid.uuid4()
            version = await make_document_version(
                session,
                supplier_name="Supplier B",
                document_type=PriceDocumentType.supplier_quote.value,
                check_batch_id=own,
            )
            await add_priced_item(session, version, "ОП-4 (з) ВСЕ ЗПУ РФ", 455)
            await CanonicalService(session).rebuild()

            from app.matching.cascade import MatchService

            own_match = await MatchService(session).match("ОП-4", check_batch_id=own)
            foreign_match = await MatchService(session).match(
                "ОП-4", check_batch_id=other
            )
            no_context_match = await MatchService(session).match("ОП-4")

            _, in_own = await VariantService(session).by_query(
                "ОП-4", check_batch_id=own
            )
            _, in_foreign = await VariantService(session).by_query(
                "ОП-4", check_batch_id=other
            )
            _, no_context = await VariantService(session).by_query("ОП-4")

            assert own_match.best is not None
            assert foreign_match.best is None
            assert no_context_match.best is None
            assert any(variant.offers for variant in in_own)
            assert all(not variant.offers for variant in in_foreign)
            assert all(not variant.offers for variant in no_context)

    asyncio.run(exercise())


def test_finalized_calculation_keeps_price_snapshot(tmp_path):
    """Активация нового прайса не переписывает уже собранное КП."""

    async def exercise():
        from app.matching.calculations import CalculationService, LineInput
        from app.models import PriceDocumentType

        async with open_session(tmp_path) as session:
            batch = uuid.uuid4()
            version = await make_document_version(
                session,
                supplier_name="Supplier A",
                document_type=PriceDocumentType.price_list.value,
            )
            item = await add_priced_item(session, version, "ОП-4 (з) ВСЕ ЗПУ Россия", 460)
            await CanonicalService(session).rebuild()

            calculation = await CalculationService(session).create(
                project_id=PROJECT_ID,
                lines=[LineInput(price_item_id=item.id, quantity=Decimal("2"))],
                title="КП",
                check_batch_id=batch,
            )
            stored_price = calculation.items[0].base_price
            stored_version = calculation.items[0].price_version_id
            assert stored_price == Decimal("460.00")

            # Менеджер активирует новый прайс: старая версия вытесняется.
            version.status = "superseded"
            new_version = await make_document_version(
                session,
                supplier_name="Supplier B",
                document_type=PriceDocumentType.price_list.value,
            )
            await add_priced_item(session, new_version, "ОП-4 (з) ВСЕ ЗПУ Россия", 999)
            await CanonicalService(session).rebuild()
            await session.flush()
            await session.refresh(calculation.items[0])

            assert calculation.items[0].price_version_id == stored_version
            assert calculation.items[0].base_price == stored_price
            assert calculation.items[0].price_document_type == "PRICE_LIST"
            assert calculation.items[0].price_source == "BITRIX"

    asyncio.run(exercise())
