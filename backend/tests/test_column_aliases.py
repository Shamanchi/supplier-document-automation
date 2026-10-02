"""Словарь подписей колонок в БД: приоритет областей и исключения."""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.column_aliases import AliasDictionary, normalize_alias, remember
from app.models import Base, ColumnAliasScope, PriceFile, Supplier
from app.parsing.headers import suggest_role


def _run(coro):
    return asyncio.run(coro)


async def _session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)()


def test_alias_is_stored_lowercase_and_whitespace_collapsed() -> None:
    assert normalize_alias("  Цена   ОПТ ") == "цена опт"
    assert normalize_alias("ЦЕНА ОПТ") == normalize_alias("цена опт")


def test_global_alias_applies_to_every_price_list() -> None:
    async def scenario() -> None:
        async with await _session() as session:
            await remember(session, "Цена ОПТ", role="price", price_kind="wholesale",
                           vat_mode="with_vat")
            dictionary = await AliasDictionary.load(session)
            suggestion = dictionary.suggest("цена   опт")
            assert suggestion.role == "price"
            assert suggestion.price_descriptor["kind"] == "wholesale"
            assert suggestion.price_descriptor["vat_mode"] == "with_vat"

    _run(scenario())


def test_a_supplier_exception_overrides_the_global_entry() -> None:
    """У одного поставщика «расч.» — оптовая цена, у остальных — не цена."""

    async def scenario() -> None:
        async with await _session() as session:
            supplier = Supplier(code="s1", name="S1")
            session.add(supplier)
            await session.flush()

            await remember(session, "Расч.", role="attribute")
            await remember(
                session,
                "Расч.",
                role="price",
                price_kind="wholesale",
                scope=ColumnAliasScope.supplier.value,
                supplier_id=supplier.id,
            )

            everyone = await AliasDictionary.load(session)
            assert everyone.suggest("расч.").role == "attribute"

            theirs = await AliasDictionary.load(session, supplier_id=supplier.id)
            assert theirs.suggest("расч.").role == "price"
            assert theirs.suggest("расч.").price_descriptor["kind"] == "wholesale"

    _run(scenario())


def test_a_price_file_exception_wins_over_the_supplier_entry() -> None:
    async def scenario() -> None:
        async with await _session() as session:
            supplier = Supplier(code="s2", name="S2")
            session.add(supplier)
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="p.xlsx",
                storage_key="k",
                sha256="h",
                size_bytes=1,
                mime="application/vnd.ms-excel",
            )
            session.add(price_file)
            await session.flush()

            await remember(session, "Тариф", role="price", price_kind="retail",
                           scope=ColumnAliasScope.supplier.value, supplier_id=supplier.id)
            await remember(session, "Тариф", role="price", price_kind="purchase",
                           scope=ColumnAliasScope.price_file.value,
                           price_file_id=price_file.id)

            dictionary = await AliasDictionary.load(
                session, supplier_id=supplier.id, price_file_id=price_file.id
            )
            assert dictionary.suggest("тариф").price_descriptor["kind"] == "purchase"

    _run(scenario())


def test_an_unknown_caption_falls_back_to_the_builtin_dictionary() -> None:
    async def scenario() -> None:
        async with await _session() as session:
            dictionary = await AliasDictionary.load(session)
            assert dictionary.suggest("Наименование").role == "name"
            assert dictionary.suggest("Цена розничная").role == "price"

    _run(scenario())


def test_remembering_the_same_alias_twice_updates_instead_of_duplicating() -> None:
    async def scenario() -> None:
        async with await _session() as session:
            await remember(session, "Опт", role="price", price_kind="wholesale")
            await remember(session, "  ОПТ  ", role="price", price_kind="dealer")
            dictionary = await AliasDictionary.load(session)
            assert dictionary.suggest("опт").price_descriptor["kind"] == "dealer"

    _run(scenario())


def test_the_dictionary_overrides_the_builtin_role_during_a_parse() -> None:
    """Словарь действует через `suggest_role`, а не только при прямом вызове.

    `suggest_role` зовут из двух десятков мест разбора без сессии БД, поэтому
    словарь подключается контекстом на время разбора. Проверяем, что подпись
    перекрывается внутри контекста и не течёт наружу.
    """

    async def scenario() -> None:
        async with await _session() as session:
            await remember(
                session,
                "Расч.",
                role="price",
                price_kind="wholesale",
                vat_mode="without_vat",
            )
            dictionary = await AliasDictionary.load(session)

        assert suggest_role("Расч.").role != "price"
        with dictionary.active():
            resolved = suggest_role("Расч.")
            assert resolved.role == "price"
            assert resolved.price_descriptor["kind"] == "wholesale"
            # Подписи вне словаря по-прежнему разбирает встроенный словарь.
            assert suggest_role("Наименование").role == "name"
        assert suggest_role("Расч.").role != "price"

    _run(scenario())
