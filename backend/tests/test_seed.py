"""Regression tests for idempotent parse-rule seed imports."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.seed as seed
from app.models import (
    ARCHIVE_PROJECT_ID,
    ORIGINAL_PROJECT_ID,
    Base,
    ParseRule,
    Project,
    ServiceClient,
    Supplier,
)


def test_seed_import_tolerates_a_legacy_duplicate_and_promotes_the_usable_rule(
    monkeypatch, tmp_path: Path
) -> None:
    asyncio.run(_seed_import_tolerates_a_legacy_duplicate_and_promotes_the_usable_rule(monkeypatch, tmp_path))


async def _seed_import_tolerates_a_legacy_duplicate_and_promotes_the_usable_rule(
    monkeypatch, tmp_path: Path
) -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    seed_file = tmp_path / "rules.json"
    seed_file.write_text(
        json.dumps(
            [
                {
                    "supplier_code": "seed-duplicate",
                    "supplier_name": "Поставщик",
                    "name": "Правило из сида",
                    "loader": "xlsx",
                    "header_hash": "duplicate-header",
                    "fingerprint": {"headers": ["Наименование", "Цена"]},
                    "recipe": {"sheets": []},
                    "revision": 1,
                    "status": "draft",
                    "provenance": "auto",
                    "validation": {"passed": True},
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(seed, "session_factory", session_factory)

    try:
        async with session_factory() as session:
            supplier = Supplier(code="seed-duplicate", name="Поставщик")
            session.add(supplier)
            await session.flush()
            for status, name in (("deprecated", "Старое правило"), ("draft", "Рабочее правило")):
                session.add(
                    ParseRule(
                        supplier_id=supplier.id,
                        name=name,
                        loader="xlsx",
                        header_hash="duplicate-header",
                        fingerprint={"headers": ["Наименование", "Цена"]},
                        recipe={"sheets": []},
                        revision=1,
                        status=status,
                        provenance="auto",
                    )
                )
            await session.commit()

        created, skipped, updated = await seed.import_rules(str(seed_file))

        assert (created, skipped, updated) == (0, 0, 1)
        async with session_factory() as session:
            rules = (
                await session.execute(
                    select(ParseRule)
                    .where(ParseRule.header_hash == "duplicate-header")
                    .order_by(ParseRule.name)
                )
            ).scalars().all()
        assert [(rule.name, rule.status) for rule in rules] == [
            ("Рабочее правило", "active"),
            ("Старое правило", "deprecated"),
        ]
    finally:
        await engine.dispose()


def test_seed_import_puts_unprojected_rules_into_original_prices(
    monkeypatch, tmp_path: Path
) -> None:
    asyncio.run(_seed_import_puts_unprojected_rules_into_original_prices(monkeypatch, tmp_path))


async def _seed_import_puts_unprojected_rules_into_original_prices(
    monkeypatch, tmp_path: Path
) -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        session.add_all(
            [
                Project(
                    id=ORIGINAL_PROJECT_ID,
                    slug="original-prices",
                    name="Original prices",
                    is_default=True,
                ),
                Project(
                    id=ARCHIVE_PROJECT_ID,
                    slug="legacy-archive",
                    name="Legacy archive",
                    is_archived=True,
                ),
            ]
        )
        custom_project = Project(slug="custom-workspace", name="Custom workspace")
        session.add(custom_project)
        await session.flush()
        custom_project_id = custom_project.id
        await session.commit()

    seed_file = tmp_path / "rules.json"
    seed_file.write_text(
        json.dumps(
            [
                {
                    "supplier_code": "seed-unprojected",
                    "supplier_name": "Supplier without project",
                    "name": "Rule without project_slug",
                    "loader": "xlsx",
                    "header_hash": "unprojected-header",
                    "fingerprint": {"headers": ["Name", "Price"]},
                    "recipe": {"sheets": []},
                    "revision": 1,
                    "status": "draft",
                    "provenance": "auto",
                },
                {
                    "supplier_code": "seed-custom-workspace",
                    "supplier_name": "Supplier with project",
                    "name": "Rule with project_slug",
                    "loader": "xlsx",
                    "header_hash": "custom-workspace-header",
                    "fingerprint": {"headers": ["Name", "Article", "Price"]},
                    "recipe": {"sheets": []},
                    "revision": 1,
                    "status": "draft",
                    "provenance": "auto",
                    "project_slug": "custom-workspace",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(seed, "session_factory", session_factory)

    try:
        created, skipped, updated = await seed.import_rules(str(seed_file))

        assert (created, skipped, updated) == (2, 0, 0)
        async with session_factory() as session:
            unprojected_rule = (
                await session.execute(
                    select(ParseRule).where(ParseRule.header_hash == "unprojected-header")
                )
            ).scalar_one()
            custom_rule = (
                await session.execute(
                    select(ParseRule).where(ParseRule.header_hash == "custom-workspace-header")
                )
            ).scalar_one()
        assert unprojected_rule.project_id == ORIGINAL_PROJECT_ID
        assert custom_rule.project_id == custom_project_id
    finally:
        await engine.dispose()


def test_seed_supplier_matches_engineering_upload_and_rule_auto_found(
    monkeypatch, tmp_path: Path
) -> None:
    asyncio.run(
        _seed_supplier_matches_engineering_upload_and_rule_auto_found(
            monkeypatch, tmp_path
        )
    )


async def _seed_supplier_matches_engineering_upload_and_rule_auto_found(
    monkeypatch, tmp_path: Path
) -> None:
    """Scenario D: fresh DB -> seed import -> engineering upload same supplier.

    Engineering upload looks suppliers up by (service_client_id, external_id);
    the seed must create the same service-scoped supplier so the saved rule is
    found automatically and no LLM proposal is ever started.
    """
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    seed_file = tmp_path / "rules.json"
    seed_file.write_text(
        json.dumps(
            {
                "format": "docprice.parse-rules",
                "version": 1,
                "project": {"slug": "original-prices", "name": "Original prices"},
                "service_clients": [
                    {
                        "ref": "internal-ui",
                        "kind": "internal_ui",
                        "name": "Внутренний интерфейс",
                    }
                ],
                "suppliers": [
                    {
                        "code": "internal-ui-526b0853804caefdf235a3d7",
                        "name": "Supplier F",
                        "external_id": "original-price-new",
                        "service_client_ref": "internal-ui",
                    }
                ],
                "rules": [
                    {
                        "supplier_code": "internal-ui-526b0853804caefdf235a3d7",
                        "name": "Review: price_list_sample.xlsx",
                        "loader": "xlsx",
                        "header_hash": "seed-header-hash",
                        "fingerprint": {"loader": "xlsx", "sheet_signatures": []},
                        "recipe": {"sheets": []},
                        "revision": 17,
                        "status": "active",
                        "provenance": "manual",
                        "validation": {"passed": True},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(seed, "session_factory", session_factory)

    try:
        created, skipped, updated = await seed.import_rules(str(seed_file))
        assert (created, skipped, updated) == (1, 0, 0)

        async with session_factory() as session:
            client = (
                await session.execute(
                    select(ServiceClient).where(ServiceClient.is_internal.is_(True))
                )
            ).scalar_one()
            # The identity query engineering upload runs before it creates a
            # supplier; a missing row here would mean a second supplier is
            # created and the seed rule is never matched automatically.
            upload_supplier = (
                await session.execute(
                    select(Supplier).where(
                        Supplier.service_client_id == client.id,
                        Supplier.external_id == "original-price-new",
                    )
                )
            ).scalar_one()
            rule = (
                await session.execute(
                    select(ParseRule).where(ParseRule.header_hash == "seed-header-hash")
                )
            ).scalar_one()

        assert upload_supplier.id == rule.supplier_id
        assert upload_supplier.service_client_id == client.id
        assert rule.project_id == ORIGINAL_PROJECT_ID
        assert rule.status == "active"
        assert rule.revision == 17

        # Exact-rule detection runs before any LLM attempt: matching here
        # means the import parses deterministically with zero LLM calls.
        from app.rules_service import find_exact_rule

        async with session_factory() as session:
            matched = await find_exact_rule(
                session,
                rule.header_hash,
                rule.loader,
                upload_supplier.id,
                project_id=rule.project_id,
            )
        assert matched is not None
        assert matched.id == rule.id

        # Re-import stays idempotent and keeps the supplier identity stable.
        created, skipped, updated = await seed.import_rules(str(seed_file))
        assert (created, skipped, updated) == (0, 1, 0)
        async with session_factory() as session:
            suppliers = (await session.execute(select(Supplier))).scalars().all()
            rules = (await session.execute(select(ParseRule))).scalars().all()
        assert len(suppliers) == 1
        assert len(rules) == 1
        assert suppliers[0].service_client_id == client.id
    finally:
        await engine.dispose()
