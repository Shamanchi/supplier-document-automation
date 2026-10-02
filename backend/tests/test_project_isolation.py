from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.category_service import bind_supplier_categories, bootstrap_global_category_directory
from app.models import (
    ARCHIVE_PROJECT_ID,
    ORIGINAL_PROJECT_ID,
    Base,
    GlobalCategory,
    PriceFile,
    PriceVersion,
    Project,
    Supplier,
    SupplierCategory,
)


def test_price_categories_are_isolated_by_project() -> None:
    async def exercise() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with factory() as session:
            original = Project(
                id=ORIGINAL_PROJECT_ID,
                slug="original-prices",
                name="Оригинальные прайсы",
                is_default=True,
            )
            archive = Project(
                id=ARCHIVE_PROJECT_ID,
                slug="legacy-archive",
                name="Архив",
                is_archived=True,
            )
            supplier = Supplier(code="same-supplier", name="Один поставщик")
            session.add_all([original, archive, supplier])
            await session.flush()
            original_file = PriceFile(
                project_id=original.id,
                supplier_id=supplier.id,
                original_filename="original.xlsx",
                storage_key="sha256/original",
                sha256="a" * 64,
                size_bytes=1,
                mime="application/octet-stream",
            )
            archive_file = PriceFile(
                project_id=archive.id,
                supplier_id=supplier.id,
                original_filename="archive.xlsx",
                storage_key="sha256/archive",
                sha256="b" * 64,
                size_bytes=1,
                mime="application/octet-stream",
            )
            session.add_all([original_file, archive_file])
            await session.flush()
            original_version = PriceVersion(
                supplier_id=supplier.id,
                price_file_id=original_file.id,
                version_no=1,
                status="validated",
            )
            archive_version = PriceVersion(
                supplier_id=supplier.id,
                price_file_id=archive_file.id,
                version_no=2,
                status="validated",
            )
            session.add_all([original_version, archive_version])
            await session.flush()

            original_categories = await bind_supplier_categories(
                session,
                supplier_id=supplier.id,
                version_id=original_version.id,
                project_id=original.id,
                source_paths=["Оборудование / Фильтры"],
            )
            archive_categories = await bind_supplier_categories(
                session,
                supplier_id=supplier.id,
                version_id=archive_version.id,
                project_id=archive.id,
                source_paths=["Оборудование / Фильтры"],
            )
            assert original_categories["оборудование / фильтры"].id != archive_categories[
                "оборудование / фильтры"
            ].id

            await bootstrap_global_category_directory(
                session, created_by="test", project_id=original.id, commit=False
            )
            await bootstrap_global_category_directory(
                session, created_by="test", project_id=archive.id, commit=False
            )
            assert await session.scalar(select(func.count(SupplierCategory.id))) == 4
            assert await session.scalar(select(func.count(GlobalCategory.id))) == 4
            assert await session.scalar(
                select(func.count(GlobalCategory.id)).where(
                    GlobalCategory.project_id == original.id
                )
            ) == 2
            await session.rollback()

        await engine.dispose()

    asyncio.run(exercise())
