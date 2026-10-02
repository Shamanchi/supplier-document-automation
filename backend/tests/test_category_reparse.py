"""Category-only reparse identity safety."""

import asyncio
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.category_reparse import (
    CategoryRecord,
    _candidate_records,
    cap_category_path,
    exact_category_matches,
    safe_stored_category_path,
)
from app.models import Base, PriceFile, PriceVersion, Supplier, VersionStatus
from app.parsing.pipeline import ParsedItem, ParseResult


def _record(
    *, item_id: uuid.UUID | None, row: int, name: str, category: str | None
) -> CategoryRecord:
    return CategoryRecord(
        item_id=item_id,
        source_sheet="Прайс",
        source_row=row,
        normalized_name=name,
        category_path=category,
    )


def test_exact_category_matches_updates_only_unique_physical_identity() -> None:
    first_id = uuid.uuid4()
    second_id = uuid.uuid4()
    stored = [
        _record(item_id=first_id, row=10, name="товар а", category="Цена / К"),
        _record(item_id=second_id, row=11, name="товар б", category="Цена / Б"),
    ]
    candidates = [
        _record(item_id=None, row=10, name="товар а", category="А"),
        _record(item_id=None, row=11, name="другое имя", category="Б"),
    ]

    assert exact_category_matches(stored, candidates) == {first_id: "А"}


def test_exact_category_matches_rejects_ambiguous_duplicate_rows() -> None:
    stored_id = uuid.uuid4()
    stored = [_record(item_id=stored_id, row=10, name="товар", category="Старая")]
    candidates = [
        _record(item_id=None, row=10, name="товар", category="А"),
        _record(item_id=None, row=10, name="товар", category="Б"),
    ]

    assert exact_category_matches(stored, candidates) == {}


def test_cap_category_path_keeps_six_most_local_levels() -> None:
    assert cap_category_path("A / B / C / D / E / F / G / H") == "C / D / E / F / G / H"
    assert cap_category_path("A / B") == "A / B"
    assert cap_category_path("Цена / Асбесто / РТИ") == "Асбесто / РТИ"
    assert cap_category_path("Цены указаны на сайте") is None
    assert cap_category_path(None) is None


def test_cap_category_path_drops_outline_markers_without_semantic_text() -> None:
    assert cap_category_path("3.0 / 4.0 / 10.0") is None
    assert cap_category_path("1. / 2. / 3.") is None
    assert cap_category_path("3.6 Оптические кабели / 3.6.1 Муфты") == (
        "3.6 Оптические кабели / 3.6.1 Муфты"
    )


def test_safe_stored_category_path_uses_only_meaningful_sheet_titles() -> None:
    assert safe_stored_category_path(None, "Крепёж") == "Крепёж"
    assert safe_stored_category_path(None, "Лист1") is None
    assert safe_stored_category_path(None, "20.05.2026") is None
    assert (
        safe_stored_category_path(None, "Каталог - Оптические кабели свя") is None
    )


def test_candidate_records_keeps_safe_rows_from_partially_ambiguous_workbook(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            supplier = Supplier(code="partial-category", name="Partial Category")
            session.add(supplier)
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="partial.xls",
                storage_key="partial.xls",
                sha256="p" * 64,
                size_bytes=1,
                mime="application/vnd.ms-excel",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id,
                price_file_id=price_file.id,
                version_no=1,
            )
            session.add(version)
            await session.flush()

            async def rejected_parse(*_args, **_kwargs) -> str:
                return VersionStatus.needs_mapping.value

            partial = ParseResult(
                needs_mapping=True,
                items=[
                    ParsedItem(
                        source_name="Брюки утепленные",
                        normalized_name="брюки утепленные",
                        source_article="БРЮ300",
                        normalized_article="БРЮ300",
                        unit_code=None,
                        category_path="Брюки, полукомбинезоны утепленные",
                        dimensions=None,
                        description=None,
                        attribute_values=[],
                        source_sheet="Зимняя одежда",
                        source_row=75,
                    )
                ],
            )
            monkeypatch.setattr("app.category_reparse.storage.get_price_file", lambda _key: b"xls")
            monkeypatch.setattr("app.category_reparse.parse_and_persist", rejected_parse)
            monkeypatch.setattr(
                "app.category_reparse.parse_workbook", lambda *_args, **_kwargs: partial
            )

            records, error = await _candidate_records(session, version)
            assert error is None
            assert records == [
                CategoryRecord(
                    item_id=None,
                    source_sheet="Зимняя одежда",
                    source_row=75,
                    normalized_name="брюки утепленные",
                    category_path="Брюки, полукомбинезоны утепленные",
                )
            ]
        await engine.dispose()

    asyncio.run(scenario())
