"""Пересборка канонического слоя не должна рвать привязку документов.

rebuild() строит каноны из прайсов заново, но canonical_products удаляются
вместе с document_links (ON DELETE CASCADE): привязка «канон → паспорт/
сертификат», сделанная скриптами stage-2, молча терялась на каждой пересборке.
Канон с документом обязан пережить rebuild, а позиции прайсов с тем же
match_key — влиться в него, а не породить дубликат.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.matching.canonical import CanonicalService
from app.models import (
    CanonicalProduct,
    CanonicalProductSource,
    DocumentLink,
    ProductDocument,
)

from tests.test_matching import add_item, make_version, open_session


async def _attach_document(db, canonical: CanonicalProduct) -> None:
    """Привязать документ к канону, как это делают скрипты stage-2."""
    document = ProductDocument(
        title="Сертификат соответствия",
        registration_number="ЕАЭС RU С-RU.ПБ01.В.00272/21",
    )
    db.add(document)
    await db.flush()
    db.add(
        DocumentLink(
            document_id=document.id,
            canonical_product_id=canonical.id,
            matched_by="registry",
            evidence={"registration_number": document.registration_number},
        )
    )
    await db.flush()


def _by_title(rows, fragment: str) -> CanonicalProduct:
    return next(row for row in rows if fragment in row.title)


def test_rebuild_keeps_document_linked_canonical_and_link(tmp_path):
    """Канон с привязанным документом переживает rebuild вместе со связью."""

    async def exercise():
        async with open_session(tmp_path) as db:
            version = await make_version(db, supplier_name="Supplier A")
            await add_item(db, version, "Огнетушитель ОП-4(з)-АВСЕ", "ОП-4")
            await CanonicalService(db).rebuild()
            await db.commit()

            canonical = (await db.scalars(select(CanonicalProduct))).one()
            canonical_id = canonical.id
            await _attach_document(db, canonical)
            await db.commit()

            await CanonicalService(db).rebuild()
            await db.commit()
            db.expire_all()

            rows = list(await db.scalars(select(CanonicalProduct)))
            assert len(rows) == 1
            assert rows[0].id == canonical_id
            assert rows[0].sources_count == 1

            links = list(await db.scalars(select(DocumentLink)))
            assert len(links) == 1
            assert links[0].canonical_product_id == canonical_id

    asyncio.run(exercise())


def test_rebuild_merges_same_key_items_into_document_linked_canonical(tmp_path):
    """Позиции с тем же ключом вливаются в защищённый канон без дубликатов."""

    async def exercise():
        async with open_session(tmp_path) as db:
            version = await make_version(db, supplier_name="Supplier A")
            item_a = await add_item(db, version, "Огнетушитель ОП-4(з)-АВСЕ", "ОП-4")
            item_a_id = item_a.id
            await CanonicalService(db).rebuild()
            await db.commit()

            canonical = (await db.scalars(select(CanonicalProduct))).one()
            canonical_id = canonical.id
            await _attach_document(db, canonical)
            await db.commit()

            other = await make_version(db, supplier_name="Supplier E")
            item_b = await add_item(db, other, "ОГНЕТУШИТЕЛЬ ОП 4 з АВСЕ", "оп-4")
            item_b_id = item_b.id
            await CanonicalService(db).rebuild()
            await db.commit()
            db.expire_all()

            rows = list(await db.scalars(select(CanonicalProduct)))
            assert len(rows) == 1
            assert rows[0].id == canonical_id
            assert rows[0].sources_count == 2

            sources = list(
                await db.scalars(
                    select(CanonicalProductSource).where(
                        CanonicalProductSource.canonical_product_id == canonical_id
                    )
                )
            )
            assert {source.price_item_id for source in sources} == {item_a_id, item_b_id}

            links = list(await db.scalars(select(DocumentLink)))
            assert len(links) == 1
            assert links[0].canonical_product_id == canonical_id

    asyncio.run(exercise())


def test_rebuild_prunes_unlinked_canonicals_but_keeps_document_linked(tmp_path):
    """Без документов канон пересоздаётся как раньше; защищённый остаётся."""

    async def exercise():
        async with open_session(tmp_path) as db:
            version = await make_version(db, supplier_name="Supplier A")
            await add_item(db, version, "Огнетушитель ОП-4(з)-АВСЕ", "ОП-4")
            removed_item = await add_item(db, version, "Рукав пожарный РПК-50", "РПК-50")
            await CanonicalService(db).rebuild()
            await db.commit()

            rows = list(await db.scalars(select(CanonicalProduct)))
            protected = _by_title(rows, "ОП-4")
            protected_id = protected.id
            await _attach_document(db, protected)
            await db.commit()

            await db.delete(removed_item)
            await db.flush()
            await CanonicalService(db).rebuild()
            await db.commit()
            db.expire_all()

            rows = list(await db.scalars(select(CanonicalProduct)))
            assert [row.id for row in rows] == [protected_id]
            assert rows[0].sources_count == 1
            links = list(await db.scalars(select(DocumentLink)))
            assert len(links) == 1
            assert links[0].canonical_product_id == protected_id

    asyncio.run(exercise())


def test_rebuild_keeps_document_linked_canonical_without_matching_items(tmp_path):
    """Защищённый канон без позиций прайса остаётся с нулевым счётчиком."""

    async def exercise():
        async with open_session(tmp_path) as db:
            version = await make_version(db, supplier_name="Supplier A")
            item = await add_item(db, version, "Огнетушитель ОП-4(з)-АВСЕ", "ОП-4")
            await CanonicalService(db).rebuild()
            await db.commit()

            canonical = (await db.scalars(select(CanonicalProduct))).one()
            canonical_id = canonical.id
            await _attach_document(db, canonical)
            await db.commit()

            await db.delete(item)
            await db.flush()
            await CanonicalService(db).rebuild()
            await db.commit()
            db.expire_all()

            row = await db.get(CanonicalProduct, canonical_id)
            assert row is not None
            assert row.sources_count == 0
            links = list(await db.scalars(select(DocumentLink)))
            assert len(links) == 1
            assert links[0].canonical_product_id == canonical_id

    asyncio.run(exercise())


def test_rebuild_without_documents_replaces_rows_as_before(tmp_path):
    """Обычный rebuild (без документов) ведёт себя как раньше."""

    async def exercise():
        async with open_session(tmp_path) as db:
            version = await make_version(db, supplier_name="Supplier C")
            await add_item(db, version, "Ороситель СВН-15", "СВН-15")
            await CanonicalService(db).rebuild()
            await db.commit()
            first_id = (await db.scalars(select(CanonicalProduct))).one().id

            await CanonicalService(db).rebuild()
            await db.commit()
            db.expire_all()

            rows = list(await db.scalars(select(CanonicalProduct)))
            assert len(rows) == 1
            assert rows[0].id != first_id
            assert rows[0].sources_count == 1
            assert len(list(await db.scalars(select(DocumentLink)))) == 0

    asyncio.run(exercise())