"""End-to-end routing for native layout PDFs, complex files and scans."""
from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, ParseRule, PriceFile, PriceVersion, Supplier, VersionStatus
from app.parsing.pdf_layout import find_builtin_pdf_layout_recipe
from app.parsing.grid import Cell, SheetGrid
from app.parsing import loaders
from app.parse_service import parse_and_persist


ROOT = Path(__file__).parents[4]
ORIGINAL = ROOT / "original" / "datasets" / "прайсы"
EXTERNAL_PDF = ROOT / "external_price_corpus" / "pdf"


async def _make_version(session, filename: str, version_no: int = 1):
    supplier = Supplier(code=f"pdf-routing-{version_no}", name="PDF routing")
    session.add(supplier)
    await session.flush()
    price_file = PriceFile(
        supplier_id=supplier.id,
        original_filename=filename,
        storage_key=f"sha256/test/{version_no}",
        sha256=f"test-{version_no}",
        size_bytes=1,
        mime="application/pdf",
    )
    session.add(price_file)
    await session.flush()
    version = PriceVersion(
        supplier_id=supplier.id,
        price_file_id=price_file.id,
        version_no=version_no,
        status=VersionStatus.parsing.value,
    )
    session.add(version)
    await session.flush()
    return version, price_file


def test_native_layout_profile_is_parsed_and_validated() -> None:
    asyncio.run(_native_layout_profile_is_parsed_and_validated())


async def _native_layout_profile_is_parsed_and_validated() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with session_factory() as session:
            path = ORIGINAL / "КП опт (3).pdf"
            version, price_file = await _make_version(session, path.name)

            status = await parse_and_persist(session, version, price_file, path.read_bytes())

            assert status == VersionStatus.validated.value
            assert version.item_count == 21
            assert version.loader == "pdf_text"
            assert version.rule_match == "exact"
    finally:
        await engine.dispose()


def test_unmatched_text_pdf_is_queued_as_complex_with_one_bounded_prompt() -> None:
    asyncio.run(_unmatched_text_pdf_is_queued_as_complex_with_one_bounded_prompt())


async def _unmatched_text_pdf_is_queued_as_complex_with_one_bounded_prompt() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with session_factory() as session:
            path = EXTERNAL_PDF / "270_LPT_moduli_gazovogo.pdf"
            version, price_file = await _make_version(session, path.name)

            status = await parse_and_persist(session, version, price_file, path.read_bytes())

            assert status == VersionStatus.complex.value
            assert version.item_count == 0
            assert version.rule_generation is not None
            assert version.rule_generation["status"] == "prepared"
            assert len(version.rule_generation["prompt"]) < 30_000
    finally:
        await engine.dispose()


def test_matching_saved_layout_rule_is_reused_before_builtin_profile() -> None:
    asyncio.run(_matching_saved_layout_rule_is_reused_before_builtin_profile())


async def _matching_saved_layout_rule_is_reused_before_builtin_profile() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with session_factory() as session:
            path = ORIGINAL / "КП опт (3).pdf"
            data = path.read_bytes()
            recipe = find_builtin_pdf_layout_recipe(data, "renamed.pdf")
            assert recipe is not None
            version, price_file = await _make_version(session, path.name)
            rule = ParseRule(
                supplier_id=version.supplier_id,
                name="Подтверждённый layout",
                loader="pdf_text",
                header_hash="saved-layout-rule",
                fingerprint={"loader": "pdf_text", "layout": "огнеборец"},
                recipe=recipe,
                revision=1,
                status="active",
                provenance="manual",
            )
            session.add(rule)
            await session.flush()

            status = await parse_and_persist(session, version, price_file, data)

            assert status == VersionStatus.validated.value
            assert version.parse_rule_id == rule.id
            assert version.rule_match == "exact"
    finally:
        await engine.dispose()


def test_confirmed_version_mapping_precedes_builtin_layout_profile(monkeypatch) -> None:
    asyncio.run(_confirmed_version_mapping_precedes_builtin_layout_profile(monkeypatch))


async def _confirmed_version_mapping_precedes_builtin_layout_profile(monkeypatch) -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with session_factory() as session:
            path = ORIGINAL / "КП опт (3).pdf"
            version, price_file = await _make_version(session, path.name)
            version.mapping = {
                "tables": [
                    {
                        "sheet_name": "manual",
                        "columns": [
                            {"index": 0, "role": "name"},
                            {"index": 1, "role": "price"},
                        ],
                    }
                ]
            }
            manual_grid = SheetGrid(
                name="manual",
                rows=[
                    [Cell(value="Наименование"), Cell(value="Цена")],
                    [Cell(value="Ручное правило",), Cell(value="123,45")],
                ],
            )
            monkeypatch.setattr(loaders, "load_grids_for", lambda *_args: [manual_grid])

            status = await parse_and_persist(session, version, price_file, path.read_bytes())

            assert status == VersionStatus.validated.value
            assert version.item_count == 1
            assert version.rule_match == "exact"
    finally:
        await engine.dispose()


def test_scanned_pdf_is_sent_to_needs_ocr_without_model_prompt() -> None:
    asyncio.run(_scanned_pdf_is_sent_to_needs_ocr_without_model_prompt())


async def _scanned_pdf_is_sent_to_needs_ocr_without_model_prompt() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with session_factory() as session:
            path = EXTERNAL_PDF / "285_ScannedDoc_79.pdf"
            version, price_file = await _make_version(session, path.name)

            status = await parse_and_persist(session, version, price_file, path.read_bytes())

            assert status == VersionStatus.needs_ocr.value
            assert version.rule_generation is None
            assert "prompt" not in (version.error_message or "").lower()
    finally:
        await engine.dispose()
