"""A model-proposed pdf_layout_v1 rule must be dry-runnable before activation."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import storage
from app.api.parse_rules import validate_rule
from app.models import (
    ORIGINAL_PROJECT_ID,
    Base,
    ParseRule,
    PriceFile,
    PriceVersion,
    Project,
    Supplier,
    VersionStatus,
)
from app.parsing.pdf_layout import find_builtin_pdf_layout_recipe
from app.schemas import RuleValidateIn


SOURCE = (
    Path(__file__).parents[4]
    / "original"
    / "datasets"
    / "прайсы"
    / "КП опт (3).pdf"
)


def test_pdf_layout_rule_validate_dry_runs_without_table_extractor(monkeypatch) -> None:
    asyncio.run(_pdf_layout_rule_validate_dry_runs_without_table_extractor(monkeypatch))


async def _pdf_layout_rule_validate_dry_runs_without_table_extractor(monkeypatch) -> None:
    data = SOURCE.read_bytes()
    recipe = find_builtin_pdf_layout_recipe(data, "renamed.pdf")
    assert recipe is not None
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with session_factory() as session:
            project = Project(
                id=ORIGINAL_PROJECT_ID,
                slug="original-prices",
                name="Оригинальные прайсы",
                is_default=True,
            )
            supplier = Supplier(code="layout-api", name="Layout API")
            session.add_all([project, supplier])
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename=SOURCE.name,
                storage_key="sha256/layout-api",
                sha256="layout-api",
                size_bytes=len(data),
                mime="application/pdf",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id,
                price_file_id=price_file.id,
                version_no=1,
                status=VersionStatus.validated.value,
            )
            session.add(version)
            rule = ParseRule(
                supplier_id=supplier.id,
                name="Проверяемое layout-правило",
                loader="pdf_text",
                header_hash="layout-api-rule",
                fingerprint={"loader": "pdf_text", "layout": "огнеборец"},
                recipe=recipe,
                revision=1,
                status="draft",
                provenance="manual",
            )
            session.add(rule)
            await session.flush()
            monkeypatch.setattr(storage, "get_price_file", lambda _key: data)

            outcome = await validate_rule(
                rule.id,
                RuleValidateIn(price_version_id=version.id),
                project,
                session,
            )

            assert outcome.passed is True
            assert outcome.metrics["rows_accepted"] == 21
            assert rule.validation and rule.validation["passed"] is True
    finally:
        await engine.dispose()
