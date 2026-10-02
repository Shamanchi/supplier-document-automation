"""Applying a reviewed model draft to the source version is a real reparse."""
from __future__ import annotations

import asyncio
import io

import openpyxl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import (
    Base,
    ParseRule,
    ParseRuleProvenance,
    ParseRuleStatus,
    PriceFile,
    PriceVersion,
    Supplier,
    SupplierPriceItem,
    VersionStatus,
)
from app.parse_service import apply_parse_rule_to_version
from app.parsing import loaders
from app.parsing.mapping import ColumnMapping, MappingDecision, TableMapping
from app.parsing.rules import compute_fingerprint, compute_header_hash, recipe_from_decision


def _xlsx() -> bytes:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "Прайс"
    worksheet.append(["Наименование", "Цена"])
    worksheet.append(["Насос", "3400"])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_model_draft_is_applied_to_source_version_and_persists_items() -> None:
    asyncio.run(_model_draft_is_applied_to_source_version_and_persists_items())


async def _model_draft_is_applied_to_source_version_and_persists_items() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    data = _xlsx()
    grids = loaders.load_grids_for("xlsx", data, "model.xlsx")
    decision = MappingDecision(
        auto_ok=True,
        tables=[
            TableMapping(
                sheet_name="Прайс",
                header_row_index=0,
                columns=[
                    ColumnMapping(index=0, role="name"),
                    ColumnMapping(
                        index=1,
                        role="price",
                        price_descriptor={"kind": "retail", "vat_mode": "unknown"},
                    ),
                ],
            )
        ],
    )

    try:
        async with session_factory() as session:
            supplier = Supplier(code="model-apply", name="Model apply")
            session.add(supplier)
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="model.xlsx",
                storage_key="sha256/model.xlsx",
                sha256="source-hash",
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
            )
            session.add(version)
            fingerprint = compute_fingerprint("xlsx", grids)
            rule = ParseRule(
                supplier_id=supplier.id,
                name="Qwen draft",
                loader="xlsx",
                header_hash=compute_header_hash(fingerprint),
                fingerprint=fingerprint,
                recipe=recipe_from_decision(decision, grids),
                revision=1,
                status=ParseRuleStatus.draft.value,
                provenance=ParseRuleProvenance.model_proposed.value,
                created_from_version_id=version.id,
                validation={"passed": True},
            )
            session.add(rule)
            await session.flush()

            result = await apply_parse_rule_to_version(
                session,
                version=version,
                price_file=price_file,
                data=data,
                rule=rule,
            )
            await session.commit()

            items = list(
                (
                    await session.execute(
                        select(SupplierPriceItem).where(
                            SupplierPriceItem.price_version_id == version.id
                        )
                    )
                ).scalars()
            )
            assert result.rows_accepted == 1
            assert version.status == VersionStatus.validated.value
            assert version.parse_rule_id == rule.id
            assert version.rule_match == "exact"
            assert [item.source_name for item in items] == ["Насос"]
    finally:
        await engine.dispose()

