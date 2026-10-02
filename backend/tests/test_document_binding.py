# -*- coding: utf-8 -*-
"""Тест авто-привязки документа к канонам (backend/app/matching/document_binding.py)."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.matching.document_binding import auto_bind_document
from app.models import (
    Base,
    CanonicalProduct,
    CanonicalProductSource,
    DocumentLink,
    ProductDocument,
)


@asynccontextmanager
async def open_session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'binding.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with maker() as db:
            yield db
    finally:
        await engine.dispose()


def make_canonical(db, title: str) -> CanonicalProduct:
    canonical = CanonicalProduct(
        match_key=title.upper(),
        title=title,
        normalized_title=title.upper(),
    )
    db.add(canonical)
    return canonical


def add_source(db, canonical: CanonicalProduct) -> None:
    db.add(
        CanonicalProductSource(
            canonical_product_id=canonical.id,
            price_item_id=uuid.uuid4(),
            supplier_id=None,
        )
    )


def test_auto_bind_document_creates_confirmed_links(tmp_path):
    asyncio.run(_exercise_binding(tmp_path))


async def _exercise_binding(tmp_path):
    async with open_session(tmp_path) as db:
        plain = make_canonical(
            db, "ГОА-II-0,35-020-020 (защищаемый объем 17,5 куб. м …)"
        )
        transport = make_canonical(
            db, "ГОА-II-0,35-020-020(А) (транспортные …)"
        )
        variant = make_canonical(
            db, "ГОА(Т1)-II-0,35-020-020 (защищаемый объем …)"
        )
        accessory = make_canonical(
            db, "Кронштейн настенный для ГОА-II-0,35-020-020"
        )
        for canonical in (plain, transport, variant, accessory):
            pass
        await db.flush()
        for canonical in (plain, transport, variant, accessory):
            add_source(db, canonical)
        await db.flush()

        document = ProductDocument(
            kind="passport",
            title="ГЕНЕРАТОР ОГНЕТУШАЩЕГО АЭРОЗОЛЯ ГОА-II-0,35-020-020",
            storage_key="sha256/abc/passport.pdf",
            sha256="abc",
            source="passport_extraction",
        )
        db.add(document)
        await db.flush()

        outcome = await auto_bind_document(
            db, document=document, product_name=document.title, confirmed_by="tester"
        )
        assert outcome.created == 2
        assert outcome.existing == 0
        linked_ids = {str(item.canonical_id) for item in outcome.matched}
        assert str(plain.id) in linked_ids
        assert str(transport.id) in linked_ids
        assert str(variant.id) not in linked_ids
        assert str(accessory.id) not in linked_ids

        rows = (
            await db.execute(
                DocumentLink.__table__.select().where(
                    DocumentLink.document_id == document.id
                )
            )
        ).all()
        assert len(rows) == 2
        assert all(row.confirmed_by == "tester" for row in rows)

        # Повторный вызов идемпотентен.
        again = await auto_bind_document(
            db, document=document, product_name=document.title, confirmed_by="tester"
        )
        assert again.created == 0
        assert again.existing == 2
