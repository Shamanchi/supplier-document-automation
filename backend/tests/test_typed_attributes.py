"""Regression tests for relational, typed supplier-item attributes.

The source column is retained verbatim, while each actual cell becomes one
atomic fact.  These tests intentionally cover the contract rather than a
particular persistence implementation.
"""
from __future__ import annotations

import asyncio
import io
import json
from decimal import Decimal

import openpyxl
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.versions import _export_json, list_items
from app.api.reference import list_attribute_definitions
from app.models import (
    AttributeDefinition,
    Base,
    PriceFile,
    PriceVersion,
    Supplier,
    SupplierPriceItem,
    SupplierPriceItemAttributeValue,
    VersionStatus,
)
from app.parse_service import parse_and_persist
from app.parsing.grid import Cell, SheetGrid
from app.parsing.mapping import ColumnMapping, MappingDecision, TableMapping, build_auto_mapping
from app.parsing.normalize import is_absent_numeric_marker
from app.parsing.pipeline import parse_grids
from app.parsing.rules import recipe_from_decision, recipe_prefill_mapping, recipe_to_decision
from app.schemas import MappingColumnIn


def _xlsx(headers: list[str], rows: list[list[object]]) -> bytes:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "Прайс"
    worksheet.append(headers)
    for row in rows:
        worksheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_parser_keeps_each_attribute_cell_as_an_ordered_fact() -> None:
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [
                Cell(value="Наименование"),
                Cell(value="Материал"),
                Cell(value="Материал"),
                Cell(value="Вес, кг"),
                Cell(value="Цена"),
            ],
            [
                Cell(value="Шкаф"),
                Cell(value="сталь"),
                Cell(value="оцинкованная"),
                Cell(value="42"),
                Cell(value="1000"),
            ],
        ],
    )
    decision = MappingDecision(
        auto_ok=True,
        tables=[
            TableMapping(
                sheet_name="Прайс",
                header_row_index=0,
                columns=[
                    ColumnMapping(index=0, role="name", header="Наименование"),
                    ColumnMapping(
                        index=1,
                        role="attribute",
                        header="Материал",
                        attribute_code="material",
                    ),
                    ColumnMapping(
                        index=2,
                        role="attribute",
                        header="Материал",
                        attribute_code="material",
                    ),
                    ColumnMapping(
                        index=3,
                        role="weight",
                        header="Вес, кг",
                        attribute_code="weight",
                        attribute_value_kind="integer",
                        canonical_unit="kg",
                    ),
                    ColumnMapping(index=4, role="price", header="Цена"),
                ],
            )
        ],
    )

    result = parse_grids([grid], decision)

    assert [
        (
            value.source_header,
            value.raw_value,
            value.attribute_code,
            value.value_kind,
            value.ordinal,
            value.canonical_unit,
        )
        for value in result.items[0].attribute_values
    ] == [
        ("Материал", "сталь", "material", "text", 1, None),
        ("Материал", "оцинкованная", "material", "text", 2, None),
        ("Вес, кг", "42", "weight", "integer", 1, "kg"),
    ]


def test_mapping_api_rejects_json_as_an_attribute_value_kind() -> None:
    with pytest.raises(ValidationError):
        MappingColumnIn(index=1, role="attribute", attribute_value_kind="jsonb")


def test_numeric_absence_markers_are_not_invalid_numbers() -> None:
    assert is_absent_numeric_marker("нет")
    assert is_absent_numeric_marker("По запросу")
    assert is_absent_numeric_marker("—")
    assert not is_absent_numeric_marker("не число")


def test_parser_skips_absent_numeric_attribute_marker() -> None:
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [Cell(value="Наименование"), Cell(value="Вес, кг"), Cell(value="Цена")],
            [Cell(value="Шкаф"), Cell(value="По запросу"), Cell(value="1000")],
        ],
    )

    result = parse_grids([grid], build_auto_mapping([grid]))

    assert result.items[0].attribute_values == []


def test_parser_uses_the_same_header_normalization_for_duplicate_ordinals() -> None:
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [
                Cell(value="Наименование"),
                Cell(value="Материал"),
                Cell(value="Материал."),
                Cell(value="Цена"),
            ],
            [Cell(value="Шкаф"), Cell(value="сталь"), Cell(value="оцинкованная"), Cell(value="1000")],
        ],
    )
    decision = MappingDecision(
        auto_ok=True,
        tables=[
            TableMapping(
                sheet_name="Прайс",
                header_row_index=0,
                columns=[
                    ColumnMapping(index=0, role="name", header="Наименование"),
                    ColumnMapping(index=1, role="attribute", header="Материал"),
                    ColumnMapping(index=2, role="attribute", header="Материал."),
                    ColumnMapping(index=3, role="price", header="Цена"),
                ],
            )
        ],
    )

    result = parse_grids([grid], decision)

    assert [(value.source_header, value.ordinal) for value in result.items[0].attribute_values] == [
        ("Материал", 1),
        ("Материал.", 2),
    ]


def test_typed_attribute_columns_survive_rule_round_trip() -> None:
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [Cell(value="Наименование"), Cell(value="Вес, кг"), Cell(value="Цена")],
            [Cell(value="Шкаф"), Cell(value="42"), Cell(value="1000")],
        ],
    )
    decision = MappingDecision(
        auto_ok=True,
        tables=[
            TableMapping(
                sheet_name="Прайс",
                header_row_index=0,
                columns=[
                    ColumnMapping(index=0, role="name", header="Наименование"),
                    ColumnMapping(
                        index=1,
                        role="weight",
                        header="Вес, кг",
                        attribute_code="weight",
                        attribute_value_kind="integer",
                        canonical_unit="kg",
                    ),
                    ColumnMapping(index=2, role="price", header="Цена"),
                ],
            )
        ],
    )

    recipe = recipe_from_decision(decision, [grid])
    restored = recipe_to_decision(recipe, [grid])
    prefilled = recipe_prefill_mapping(recipe, [grid])

    restored_weight = next(c for c in restored.tables[0].columns if c.role == "weight")
    assert restored_weight.attribute_code == "weight"
    assert restored_weight.attribute_value_kind == "integer"
    assert restored_weight.canonical_unit == "kg"
    assert prefilled["tables"][0]["columns"][1] == {
        "index": 1,
        "role": "weight",
        "attribute_code": "weight",
        "attribute_value_kind": "integer",
        "canonical_unit": "kg",
    }


def test_auto_rule_records_stable_metadata_for_known_attribute_roles() -> None:
    grid = SheetGrid(
        name="Прайс",
        rows=[
            [Cell(value="Наименование"), Cell(value="Вес, кг"), Cell(value="Цена")],
            [Cell(value="Шкаф"), Cell(value="42"), Cell(value="1000")],
        ],
    )

    recipe = recipe_from_decision(build_auto_mapping([grid]), [grid])

    assert recipe["sheets"][0]["columns"][1] == {
        "index": 1,
        "role": "weight",
        "attribute_code": "weight",
        "attribute_value_kind": "decimal",
        "canonical_unit": "kg",
        "header": "вес кг",
    }


def test_relational_attributes_are_typed_and_exposed_as_a_list() -> None:
    asyncio.run(_relational_attributes_are_typed_and_exposed_as_a_list())


async def _relational_attributes_are_typed_and_exposed_as_a_list() -> None:
    engine = create_async_engine("sqlite+aiosqlite://")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    try:
        async with session_factory() as session:
            supplier = Supplier(code="typed-attributes", name="Typed attributes")
            session.add(supplier)
            await session.flush()
            data = _xlsx(
                ["Наименование", "Вес, грамм", "Толщина металла", "Цена"],
                [["Шкаф", "42", "Лист 0.8 мм", "1000"]],
            )
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="typed.xlsx",
                storage_key="sha256/typed.xlsx",
                sha256="typed",
                size_bytes=len(data),
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
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

            status = await parse_and_persist(session, version, price_file, data)
            await session.commit()

            assert status == VersionStatus.validated.value
            item = (
                await session.execute(
                    select(SupplierPriceItem).where(
                        SupplierPriceItem.price_version_id == version.id
                    )
                )
            ).scalars().one()
            values = (
                await session.execute(
                    select(SupplierPriceItemAttributeValue)
                    .where(SupplierPriceItemAttributeValue.item_id == item.id)
                )
            ).scalars().all()
            definitions = (
                await session.execute(select(AttributeDefinition).order_by(AttributeDefinition.code))
            ).scalars().all()

            assert not hasattr(item, "attributes")
            values_by_raw = {value.raw_value: value for value in values}
            assert (
                values_by_raw["42"].value_kind,
                values_by_raw["42"].value_integer,
                values_by_raw["42"].value_decimal,
                values_by_raw["42"].value_text,
            ) == ("decimal", None, Decimal("0.042"), None)
            assert (
                values_by_raw["Лист 0.8 мм"].value_kind,
                values_by_raw["Лист 0.8 мм"].value_integer,
                values_by_raw["Лист 0.8 мм"].value_text,
            ) == ("text", None, "Лист 0.8 мм")
            definitions_by_code = {definition.code: definition for definition in definitions}
            assert definitions_by_code["weight"].display_name == "Вес"
            assert definitions_by_code["weight"].value_kind == "decimal"
            dictionary = await list_attribute_definitions(
                query="weight", limit=10, session=session
            )
            assert [(definition.code, definition.value_kind) for definition in dictionary] == [
                ("weight", "decimal")
            ]

            page = await list_items(version.id, query=None, limit=50, offset=0, session=session)
            page_attributes = {attribute.name: attribute for attribute in page.items[0].attributes}
            weight = page_attributes["Вес, грамм"]
            assert (
                weight.code,
                weight.name,
                weight.value_kind,
                weight.value,
                weight.raw_value,
                weight.unit,
                weight.parse_status,
            ) == ("weight", "Вес, грамм", "decimal", "0.042", "42", "kg", "parsed")
            assert page_attributes["Толщина металла"].value == "Лист 0.8 мм"
            assert page_attributes["Толщина металла"].raw_value == "Лист 0.8 мм"

            response = await _export_json(version.id, session)
            exported = json.loads(response.body)["items"][0]
            exported_by_name = {attribute["name"]: attribute for attribute in exported["attributes"]}
            assert exported_by_name["Вес, грамм"]["value"] == "0.042"
            assert exported_by_name["Толщина металла"]["raw_value"] == "Лист 0.8 мм"
    finally:
        await engine.dispose()
