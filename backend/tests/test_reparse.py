"""Safe backfill of stored price versions from their MinIO originals."""
from __future__ import annotations

import asyncio
import io
from datetime import datetime, timezone

import openpyxl
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import storage
from app.models import (
    Base,
    ParseRule,
    PriceFile,
    PriceParseIssue,
    PriceVersion,
    Supplier,
    SupplierPriceItem,
    SupplierPriceValue,
    VersionStatus,
)
import app.reparse as reparse
from app.reparse import reparse_versions


HEADERS = [
    "Наименование",
    "Артикул",
    "Толщина металла",
    "Габаритные размеры Длина/глубина/высота, мм",
    "Описание",
    "Цена, с НДС",
]
ROW = [
    "Пост пожарный",
    "ППМП-02",
    "Лист 0.8 мм",
    "1000х900х1670",
    "Передвижной пост для средств пожаротушения",
    "64 500",
]


def _xlsx(*, headers: list[str] = HEADERS, rows: list[list[object]] | None = None) -> bytes:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "Прайс"
    worksheet.append(headers)
    for row in rows or [ROW]:
        worksheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_reparse_cli_requires_an_explicit_target() -> None:
    parser = reparse.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])

    assert parser.parse_args(["--all-versions"]).all_versions is True


def test_reparse_all_versions_can_recover_a_previously_unmapped_price(monkeypatch) -> None:
    asyncio.run(_reparse_all_versions_can_recover_a_previously_unmapped_price(monkeypatch))


async def _reparse_all_versions_can_recover_a_previously_unmapped_price(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    data = _xlsx()
    monkeypatch.setattr(storage, "get_price_file", lambda _storage_key: data)

    try:
        async with session_factory() as session:
            supplier = Supplier(code="reparse-all", name="Reparse all")
            session.add(supplier)
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="recoverable.xlsx",
                storage_key="sha256/recoverable.xlsx",
                sha256="recoverable",
                size_bytes=len(data),
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id,
                price_file_id=price_file.id,
                version_no=1,
                status=VersionStatus.needs_mapping.value,
                item_count=0,
                rows_accepted=0,
            )
            session.add(version)
            await session.commit()

        outcomes = await reparse_versions(session_factory, include_all_statuses=True)

        assert len(outcomes) == 1
        assert outcomes[0].previous_status == VersionStatus.needs_mapping.value
        assert outcomes[0].status == VersionStatus.validated.value
        assert outcomes[0].skipped is False

        async with session_factory() as session:
            refreshed = await session.get(PriceVersion, version.id)
            items = (
                await session.execute(
                    select(SupplierPriceItem).where(SupplierPriceItem.price_version_id == version.id)
                )
            ).scalars().all()
            assert refreshed.status == VersionStatus.validated.value
            assert [item.source_name for item in items] == ["Пост пожарный"]
    finally:
        await engine.dispose()


def test_reparse_preserves_active_status_and_backfills_new_item_fields(monkeypatch) -> None:
    asyncio.run(_reparse_preserves_active_status_and_backfills_new_item_fields(monkeypatch))


async def _reparse_preserves_active_status_and_backfills_new_item_fields(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    data = _xlsx()
    monkeypatch.setattr(storage, "get_price_file", lambda _storage_key: data)

    try:
        async with session_factory() as session:
            supplier = Supplier(code="reparse", name="Reparse")
            session.add(supplier)
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="target.xlsx",
                storage_key="sha256/target.xlsx",
                sha256="target",
                size_bytes=len(data),
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id,
                price_file_id=price_file.id,
                version_no=1,
                status=VersionStatus.active.value,
                activated_at=datetime.now(timezone.utc),
                item_count=1,
                rows_accepted=1,
            )
            session.add(version)
            await session.flush()
            session.add(
                SupplierPriceItem(
                    price_version_id=version.id,
                    source_name="Пост пожарный",
                    normalized_name="пост пожарный",
                    source_article="ППМП-02",
                    normalized_article="ППМП-02",
                    unit_code=None,
                    category_path=None,
                    dimensions=None,
                    description=None,
                    source_sheet="Прайс",
                    source_row=2,
                )
            )
            await session.commit()

            outcomes = await reparse_versions(session_factory, version_ids=[version.id])

            assert len(outcomes) == 1
            assert outcomes[0].previous_status == VersionStatus.active.value
            assert outcomes[0].status == VersionStatus.active.value

        async with session_factory() as session:
            refreshed = await session.get(PriceVersion, version.id)
            item = (
                await session.execute(
                    select(SupplierPriceItem).where(SupplierPriceItem.price_version_id == version.id)
                )
            ).scalars().one()

            assert refreshed.status == VersionStatus.active.value
            assert item.dimensions == "1000х900х1670"
            assert item.description == "Передвижной пост для средств пожаротушения"
            assert [(value.raw_value, value.value_text) for value in item.attribute_values] == [
                ("Лист 0.8 мм", "Лист 0.8 мм")
            ]
    finally:
        await engine.dispose()


def test_reparse_dry_run_reports_plan_without_reading_or_changing_data(monkeypatch) -> None:
    asyncio.run(_reparse_dry_run_reports_plan_without_reading_or_changing_data(monkeypatch))


async def _reparse_dry_run_reports_plan_without_reading_or_changing_data(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    def unexpected_object_read(_storage_key: str) -> bytes:
        raise AssertionError("dry-run must not read MinIO")

    monkeypatch.setattr(storage, "get_price_file", unexpected_object_read)

    try:
        async with session_factory() as session:
            supplier = Supplier(code="reparse-dry", name="Reparse dry run")
            session.add(supplier)
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="target.xlsx",
                storage_key="sha256/target.xlsx",
                sha256="target-dry",
                size_bytes=1,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id,
                price_file_id=price_file.id,
                version_no=1,
                status=VersionStatus.validated.value,
                item_count=7,
                rows_accepted=7,
            )
            session.add(version)
            await session.commit()

            outcomes = await reparse_versions(session_factory, version_ids=[version.id], dry_run=True)

            assert outcomes[0].dry_run is True
            assert outcomes[0].status == VersionStatus.validated.value

        async with session_factory() as session:
            refreshed = await session.get(PriceVersion, version.id)
            assert refreshed.status == VersionStatus.validated.value
            assert refreshed.item_count == 7
    finally:
        await engine.dispose()


def test_reparse_preflight_reads_source_but_rolls_candidate_back(monkeypatch) -> None:
    asyncio.run(_reparse_preflight_reads_source_but_rolls_candidate_back(monkeypatch))


async def _reparse_preflight_reads_source_but_rolls_candidate_back(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    data = _xlsx()
    monkeypatch.setattr(storage, "get_price_file", lambda _storage_key: data)

    try:
        async with session_factory() as session:
            supplier = Supplier(code="reparse-preflight", name="Reparse preflight")
            session.add(supplier)
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="target.xlsx",
                storage_key="sha256/target-preflight.xlsx",
                sha256="target-preflight",
                size_bytes=len(data),
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id,
                price_file_id=price_file.id,
                version_no=1,
                status=VersionStatus.validated.value,
                item_count=1,
                rows_accepted=1,
            )
            session.add(version)
            await session.flush()
            session.add(
                SupplierPriceItem(
                    price_version_id=version.id,
                    source_name="Старый товар",
                    normalized_name="старый товар",
                    source_article=None,
                    normalized_article=None,
                    unit_code=None,
                    category_path=None,
                    dimensions=None,
                    description=None,
                    source_sheet="Прайс",
                    source_row=2,
                )
            )
            await session.commit()

        outcomes = await reparse_versions(session_factory, version_ids=[version.id], preflight=True)

        assert outcomes[0].preflight is True
        assert outcomes[0].safe_to_apply is True
        assert outcomes[0].previous_metrics is not None
        assert outcomes[0].candidate_metrics is not None
        assert outcomes[0].previous_metrics.description_count == 0
        assert outcomes[0].candidate_metrics.description_count == 1
        assert outcomes[0].candidate_metrics.attribute_count == 1

        async with session_factory() as session:
            refreshed = await session.get(PriceVersion, version.id)
            items = (
                await session.execute(
                    select(SupplierPriceItem).where(SupplierPriceItem.price_version_id == version.id)
                )
            ).scalars().all()
            assert refreshed.status == VersionStatus.validated.value
            assert [item.source_name for item in items] == ["Старый товар"]
            assert items[0].description is None
    finally:
        await engine.dispose()


def test_reparse_keeps_existing_rows_when_the_refreshed_parse_needs_mapping(monkeypatch) -> None:
    asyncio.run(_reparse_keeps_existing_rows_when_the_refreshed_parse_needs_mapping(monkeypatch))


async def _reparse_keeps_existing_rows_when_the_refreshed_parse_needs_mapping(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    monkeypatch.setattr(
        storage,
        "get_price_file",
        lambda _storage_key: "Неизвестная колонка;Значение\nТовар;10\n".encode("utf-8"),
    )

    try:
        async with session_factory() as session:
            supplier = Supplier(code="reparse-keep", name="Reparse keep")
            session.add(supplier)
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="unknown.csv",
                storage_key="sha256/unknown.csv",
                sha256="unknown",
                size_bytes=1,
                mime="text/csv",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id,
                price_file_id=price_file.id,
                version_no=1,
                status=VersionStatus.validated.value,
                item_count=1,
                rows_accepted=1,
            )
            session.add(version)
            await session.flush()
            session.add(
                SupplierPriceItem(
                    price_version_id=version.id,
                    source_name="Старый товар",
                    normalized_name="старый товар",
                    source_article=None,
                    normalized_article=None,
                    unit_code=None,
                    category_path=None,
                    dimensions=None,
                    description=None,
                    source_sheet="Прайс",
                    source_row=2,
                )
            )
            await session.commit()

        outcomes = await reparse_versions(session_factory, version_ids=[version.id])

        assert outcomes[0].status == VersionStatus.validated.value
        assert outcomes[0].skipped is True
        assert outcomes[0].error_message == "Пропущено: новый разбор завершился со статусом needs_mapping"
        async with session_factory() as session:
            refreshed = await session.get(PriceVersion, version.id)
            items = (
                await session.execute(
                    select(SupplierPriceItem).where(SupplierPriceItem.price_version_id == version.id)
                )
            ).scalars().all()
        assert refreshed.status == VersionStatus.validated.value
        assert refreshed.item_count == 1
        assert [item.source_name for item in items] == ["Старый товар"]
    finally:
        await engine.dispose()


def test_reparse_keeps_existing_rows_when_the_source_read_fails(monkeypatch) -> None:
    asyncio.run(_reparse_keeps_existing_rows_when_the_source_read_fails(monkeypatch))


async def _reparse_keeps_existing_rows_when_the_source_read_fails(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    def unavailable_source(_storage_key: str) -> bytes:
        raise RuntimeError("MinIO недоступен")

    monkeypatch.setattr(storage, "get_price_file", unavailable_source)

    try:
        async with session_factory() as session:
            supplier = Supplier(code="reparse-read-error", name="Reparse read error")
            session.add(supplier)
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="source.csv",
                storage_key="sha256/source.csv",
                sha256="read-error",
                size_bytes=1,
                mime="text/csv",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id,
                price_file_id=price_file.id,
                version_no=1,
                status=VersionStatus.validated.value,
                item_count=1,
                rows_accepted=1,
            )
            session.add(version)
            await session.flush()
            session.add(
                SupplierPriceItem(
                    price_version_id=version.id,
                    source_name="Старый товар",
                    normalized_name="старый товар",
                    source_article=None,
                    normalized_article=None,
                    unit_code=None,
                    category_path=None,
                    dimensions=None,
                    description=None,
                    source_sheet="Прайс",
                    source_row=2,
                )
            )
            await session.commit()

        outcomes = await reparse_versions(session_factory, version_ids=[version.id])

        assert outcomes[0].status == VersionStatus.validated.value
        assert outcomes[0].skipped is True
        assert outcomes[0].error_message == "Пропущено: RuntimeError: MinIO недоступен"
        async with session_factory() as session:
            refreshed = await session.get(PriceVersion, version.id)
            items = (
                await session.execute(
                    select(SupplierPriceItem).where(SupplierPriceItem.price_version_id == version.id)
                )
            ).scalars().all()
        assert refreshed.status == VersionStatus.validated.value
        assert [item.source_name for item in items] == ["Старый товар"]
    finally:
        await engine.dispose()


def test_reparse_keeps_snapshot_when_a_validated_refresh_fails_quality_gate(monkeypatch) -> None:
    asyncio.run(_reparse_keeps_snapshot_when_a_validated_refresh_fails_quality_gate(monkeypatch))


async def _reparse_keeps_snapshot_when_a_validated_refresh_fails_quality_gate(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    # One valid row keeps the ordinary parse status "validated", while the
    # malformed price in the second row fails the quality gate — precisely the
    # case a safe refresh must not commit.
    data = _xlsx(
        headers=["Наименование", "Ед.", "Цена"],
        rows=[
            ["Новый товар 1", "шт", "100"],
            ["Новый товар 2", "шт", "не цена"],
        ],
    )
    monkeypatch.setattr(storage, "get_price_file", lambda _storage_key: data)

    try:
        async with session_factory() as session:
            supplier = Supplier(code="reparse-quality", name="Reparse quality")
            session.add(supplier)
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="quality.xlsx",
                storage_key="sha256/quality.xlsx",
                sha256="quality",
                size_bytes=len(data),
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id,
                price_file_id=price_file.id,
                version_no=1,
                status=VersionStatus.validated.value,
                item_count=2,
                rows_accepted=2,
                mapping={
                    "tables": [
                        {
                            "sheet_name": "Прайс",
                                "columns": [
                                    {"index": 0, "role": "name"},
                                    {"index": 1, "role": "unit"},
                                    {"index": 2, "role": "price"},
                            ],
                        }
                    ]
                },
            )
            session.add(version)
            await session.flush()
            old_item = SupplierPriceItem(
                price_version_id=version.id,
                source_name="Старый товар",
                normalized_name="старый товар",
                source_article=None,
                normalized_article=None,
                unit_code=None,
                category_path=None,
                dimensions=None,
                description=None,
                source_sheet="Прайс",
                source_row=2,
            )
            session.add(old_item)
            await session.flush()
            session.add(
                SupplierPriceValue(
                    item_id=old_item.id,
                    price_kind="other",
                    amount=None,
                    is_on_request=True,
                )
            )
            session.add(
                PriceParseIssue(
                    price_version_id=version.id,
                    severity="warning",
                    code="OLD_ISSUE",
                    sheet="Прайс",
                    row_no=2,
                    column_ref=None,
                    source_value=None,
                    message="Старое диагностическое сообщение",
                )
            )
            await session.commit()

        outcomes = await reparse_versions(session_factory, version_ids=[version.id])

        assert outcomes[0].status == VersionStatus.validated.value
        assert outcomes[0].skipped is True
        assert outcomes[0].error_message is not None
        assert "quality gate" in outcomes[0].error_message

        async with session_factory() as session:
            refreshed = await session.get(PriceVersion, version.id)
            items = (
                await session.execute(
                    select(SupplierPriceItem).where(SupplierPriceItem.price_version_id == version.id)
                )
            ).scalars().all()
            values = (
                await session.execute(
                    select(SupplierPriceValue).join(SupplierPriceItem).where(
                        SupplierPriceItem.price_version_id == version.id
                    )
                )
            ).scalars().all()
            issues = (
                await session.execute(
                    select(PriceParseIssue).where(PriceParseIssue.price_version_id == version.id)
                )
            ).scalars().all()
            rules = (await session.execute(select(ParseRule))).scalars().all()

        assert refreshed.status == VersionStatus.validated.value
        assert refreshed.item_count == 2
        assert refreshed.mapping == {
            "tables": [
                {
                    "sheet_name": "Прайс",
                    "columns": [
                        {"index": 0, "role": "name"},
                        {"index": 1, "role": "unit"},
                        {"index": 2, "role": "price"},
                    ],
                }
            ]
        }
        assert [item.source_name for item in items] == ["Старый товар"]
        assert len(values) == 1
        assert [issue.code for issue in issues] == ["OLD_ISSUE"]
        assert rules == []
    finally:
        await engine.dispose()
