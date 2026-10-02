from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import AuthUser
from app.models import (
    Base,
    GlobalCategory,
    PriceFile,
    PriceVersion,
    ORIGINAL_PROJECT_ID,
    ARCHIVE_PROJECT_ID,
    Project,
    Supplier,
    SupplierPriceItem,
    SupplierPriceValue,
)


def test_imported_items_include_versions_without_price_import_jobs(tmp_path) -> None:
    from app.api.price_items import list_imported_items
    from app.api.price_versions import list_imported_versions

    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'items.sqlite'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with factory() as session:
            project = Project(
                id=ORIGINAL_PROJECT_ID,
                slug="original-prices",
                name="Оригинальные прайсы",
                is_default=True,
            )
            archive_project = Project(
                id=ARCHIVE_PROJECT_ID,
                slug="legacy-archive",
                name="Архив",
                is_archived=True,
            )
            session.add_all([project, archive_project])
            supplier = Supplier(code="legacy", name="Старый импорт")
            other_supplier = Supplier(code="other", name="Другой поставщик")
            session.add_all([supplier, other_supplier])
            await session.flush()

            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="legacy-price.xlsx",
                storage_key="prices/legacy-price.xlsx",
                sha256="a" * 64,
                size_bytes=128,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            other_file = PriceFile(
                project_id=ARCHIVE_PROJECT_ID,
                supplier_id=other_supplier.id,
                original_filename="other-price.xlsx",
                storage_key="prices/other-price.xlsx",
                sha256="b" * 64,
                size_bytes=128,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            session.add_all([price_file, other_file])
            await session.flush()

            version = PriceVersion(
                supplier_id=supplier.id,
                price_file_id=price_file.id,
                version_no=3,
                status="parsed",
                item_count=1,
            )
            other_version = PriceVersion(
                supplier_id=other_supplier.id,
                price_file_id=other_file.id,
                version_no=1,
                status="parsed",
                item_count=1,
            )
            session.add_all([version, other_version])
            await session.flush()

            item = SupplierPriceItem(
                price_version_id=version.id,
                source_name="Огнетушитель ОП-5",
                normalized_name="огнетушитель оп 5",
                source_article="ОП-5",
                unit_code="шт",
                source_sheet="Лист1",
                source_row=7,
            )
            other_item = SupplierPriceItem(
                price_version_id=other_version.id,
                source_name="Клапан",
                normalized_name="клапан",
                source_article="К-1",
                unit_code="шт",
                source_sheet="Прайс",
                source_row=2,
            )
            session.add_all([item, other_item])
            await session.flush()
            session.add(
                SupplierPriceValue(
                    item_id=item.id,
                    price_kind="retail",
                    amount=Decimal("1250.50"),
                    currency="RUB",
                    vat_mode="unknown",
                )
            )
            await session.commit()

            page = await list_imported_items(
                query="ОП-5",
                supplier_id=None,
                price_version_id=None,
                limit=50,
                offset=0,
                principal=AuthUser(username="viewer", role="user"),
                project=project,
                session=session,
            )

            assert page.total == 1
            assert len(page.items) == 1
            result = page.items[0]
            assert result.source_name == "Огнетушитель ОП-5"
            assert result.supplier_name == "Старый импорт"
            assert result.filename == "legacy-price.xlsx"
            assert result.version_no == 3
            assert result.prices[0].amount == Decimal("1250.50")

            versions = await list_imported_versions(
                query="legacy-price",
                has_items=True,
                limit=50,
                offset=0,
                principal=AuthUser(username="viewer", role="user"),
                project=project,
                session=session,
            )
            assert versions.total == 1
            assert len(versions.items) == 1
            parsed_price = versions.items[0]
            assert parsed_price.id == version.id
            assert parsed_price.supplier_name == "Старый импорт"
            assert parsed_price.filename == "legacy-price.xlsx"
            assert parsed_price.item_count == 1
            assert parsed_price.import_id is None

            filtered_items = await list_imported_items(
                query=None,
                supplier_id=None,
                price_version_id=other_version.id,
                limit=50,
                offset=0,
                principal=AuthUser(username="viewer", role="user"),
                project=project,
                session=session,
            )
            assert filtered_items.total == 0

            archived_items = await list_imported_items(
                query=None,
                supplier_id=None,
                price_version_id=other_version.id,
                limit=50,
                offset=0,
                principal=AuthUser(username="viewer", role="user"),
                project=archive_project,
                session=session,
            )
            assert archived_items.total == 1
            assert archived_items.items[0].source_name == "Клапан"

        await engine.dispose()

    asyncio.run(exercise())


def test_manual_category_assignment_and_auto_classify(tmp_path) -> None:
    from app.api.price_items import assign_item_category, auto_classify_items
    from app.schemas import AutoClassifyIn, ItemCategoryAssignIn

    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'classify.sqlite'}")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with factory() as session:
            project = Project(
                id=ORIGINAL_PROJECT_ID,
                slug="original-prices",
                name="Оригинальные прайсы",
                is_default=True,
            )
            session.add(project)
            supplier = Supplier(code="flat", name="Плоский прайс")
            session.add(supplier)
            await session.flush()

            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="flat-price.xlsx",
                storage_key="prices/flat-price.xlsx",
                sha256="c" * 64,
                size_bytes=128,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id,
                price_file_id=price_file.id,
                version_no=1,
                status="parsed",
                item_count=2,
            )
            session.add(version)
            await session.flush()

            extinguisher = GlobalCategory(
                project_id=project.id,
                name="Огнетушители",
                normalized_name="огнетушители",
                path="Пожарное оборудование / Огнетушители",
                normalized_path="пожарное оборудование / огнетушители",
            )
            session.add(extinguisher)
            await session.flush()

            item = SupplierPriceItem(
                price_version_id=version.id,
                source_name="Огнетушитель порошковый ОП-5",
                normalized_name="огнетушитель порошковый оп 5",
                source_article="ОП-5",
                unit_code="шт",
                source_sheet="Лист1",
                source_row=3,
            )
            session.add(item)
            await session.commit()

            admin = AuthUser(username="admin", role="admin")

            assigned = await assign_item_category(
                item_id=item.id,
                payload=ItemCategoryAssignIn(global_category_id=extinguisher.id),
                admin=admin,
                project=project,
                session=session,
            )
            assert assigned.category.mapping_status == "manual"
            assert assigned.category.global_category_id == extinguisher.id
            assert assigned.category.global_path == extinguisher.path

            # Auto-classify must skip the item now that it has a manual category.
            skipped = await auto_classify_items(
                payload=AutoClassifyIn(price_version_id=version.id, apply=False),
                admin=admin,
                project=project,
                session=session,
            )
            assert skipped.considered == 0
            assert skipped.suggestions == []

            # Clearing the manual category returns the item to "unmapped".
            cleared = await assign_item_category(
                item_id=item.id,
                payload=ItemCategoryAssignIn(global_category_id=None),
                admin=admin,
                project=project,
                session=session,
            )
            assert cleared.category.mapping_status == "unmapped"
            assert cleared.category.global_category_id is None

            # Now the scorer should suggest the extinguisher category and apply it.
            result = await auto_classify_items(
                payload=AutoClassifyIn(price_version_id=version.id, apply=True, min_score=0.1),
                admin=admin,
                project=project,
                session=session,
            )
            assert result.considered == 1
            assert result.applied == 1
            assert result.suggestions[0].suggested_category_id == extinguisher.id

            refreshed = await session.get(SupplierPriceItem, item.id)
            assert refreshed is not None
            assert refreshed.manual_category_id == extinguisher.id

        await engine.dispose()

    asyncio.run(exercise())
