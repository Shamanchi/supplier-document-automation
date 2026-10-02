"""End-to-end routing for deterministic workbook layout profiles."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import rules_service
from app.models import Base, ParseRule, PriceFile, PriceVersion, Supplier, VersionStatus
from app.parse_service import parse_and_persist


SOURCE = (
    Path(__file__).parents[4]
    / "original"
    / "datasets"
    / "прайсы"
    / "ПРАЙС Supplier B (3) (3).xls"
)

PRICE_NEW_SOURCE = (
    Path(__file__).parents[4]
    / "original"
    / "datasets"
    / "прайсы"
    / "price_list_sample.xlsx"
)


def test_multi_panel_workbook_profile_is_applied_before_auto_mapping() -> None:
    asyncio.run(_multi_panel_workbook_profile_is_applied_before_auto_mapping())


async def _multi_panel_workbook_profile_is_applied_before_auto_mapping() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with session_factory() as session:
            supplier = Supplier(code="workbook-layout", name="Workbook layout")
            session.add(supplier)
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="renamed-upload.xls",
                storage_key="sha256/test/workbook-layout",
                sha256="workbook-layout",
                size_bytes=SOURCE.stat().st_size,
                mime="application/vnd.ms-excel",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id,
                price_file_id=price_file.id,
                version_no=1,
                status=VersionStatus.parsing.value,
            )
            session.add(version)
            await session.flush()

            # The workbook profile is a normal `parse_rules` row now, not a
            # constant in the code: seed it exactly as a deployment would.
            seed = json.loads(
                (Path(__file__).resolve().parents[1] / "seeds" / "parse_rules_builtin.json")
                .read_text("utf-8")
            )[0]
            session.add(
                ParseRule(
                    supplier_id=supplier.id,
                    project_id=price_file.project_id,
                    name=seed["name"],
                    loader=seed["loader"],
                    header_hash=seed["header_hash"],
                    fingerprint=seed["fingerprint"],
                    recipe=seed["recipe"],
                    revision=seed["revision"],
                    status=seed["status"],
                    provenance=seed["provenance"],
                    validation=seed["validation"],
                )
            )
            await session.flush()

            status = await parse_and_persist(session, version, price_file, SOURCE.read_bytes())

            assert status == VersionStatus.validated.value
            assert version.loader == "xls"
            assert version.rule_match == "exact"
            assert version.item_count == 1744
            assert version.errors_count == 0
    finally:
        await engine.dispose()


def test_matrix_xlsx_requires_generated_rule_instead_of_builtin_profile(monkeypatch) -> None:
    async def unexpected_rule_lookup(*_args, **_kwargs):
        raise AssertionError("A structurally matched workbook profile must run first")

    monkeypatch.setattr(rules_service, "find_exact_rule", unexpected_rule_lookup)
    asyncio.run(_matrix_xlsx_waits_for_generated_rule())


async def _matrix_xlsx_waits_for_generated_rule() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with session_factory() as session:
            supplier = Supplier(code="parallel-xlsx", name="Parallel XLSX")
            session.add(supplier)
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="renamed-upload.xlsx",
                storage_key="sha256/test/parallel-xlsx",
                sha256="parallel-xlsx",
                size_bytes=PRICE_NEW_SOURCE.stat().st_size,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            session.add(price_file)
            await session.flush()
            stale_rule = ParseRule(
                supplier_id=supplier.id,
                name="Stale column rule",
                loader="xlsx",
                header_hash="a" * 64,
                fingerprint={},
                recipe={"sheets": []},
                revision=1,
                status="draft",
                provenance="manual",
            )
            session.add(stale_rule)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id,
                price_file_id=price_file.id,
                version_no=1,
                status=VersionStatus.parsing.value,
                parse_rule_id=stale_rule.id,
            )
            session.add(version)
            await session.flush()

            status = await parse_and_persist(
                session, version, price_file, PRICE_NEW_SOURCE.read_bytes()
            )

            assert status == VersionStatus.needs_mapping.value
            assert version.loader == "xlsx"
            assert version.rule_match == "none"
            assert version.parse_rule_id is None
            assert version.rule_generation["mode"] == "section_proposals"
            assert version.rule_generation["matrix_sections_count"] > 0
    finally:
        await engine.dispose()
