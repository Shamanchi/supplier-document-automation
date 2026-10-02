"""Category directory, mapping and matching-context contracts."""

from __future__ import annotations

import asyncio
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from app.category_service import (
    apply_category_context_gate,
    bind_supplier_categories,
    bootstrap_global_category_directory,
    canonical_global_category_name,
    item_category_context,
    normalize_category_path,
    refresh_category_directory,
)
from app.db import get_session
from app.main import app
from app.models import (
    Base,
    GlobalCategory,
    PriceFile,
    PriceVersion,
    Supplier,
    SupplierCategory,
    SupplierCategoryAlias,
    SupplierCategoryMapping,
    SupplierPriceItem,
)


def test_category_normalization_is_conservative() -> None:
    assert normalize_category_path(" Огнетушители  /  Порошковые ") == (
        "огнетушители / порошковые"
    )
    assert canonical_global_category_name("3.6 Оптические кабели связи") == (
        "Оптические кабели связи"
    )
    assert canonical_global_category_name("03.05.04. Аксессуары.") == "Аксессуары"
    assert canonical_global_category_name("Шкафы, щиты, боксы.") == (
        "Шкафы, щиты, боксы"
    )
    assert canonical_global_category_name("12 В") == "12 В"
    assert canonical_global_category_name("16 портовые") == "16 портовые"


def test_bootstrap_reuses_exact_paths_and_keeps_equal_leaves_in_separate_branches() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            first = Supplier(code="bootstrap-a", name="A")
            second = Supplier(code="bootstrap-b", name="B")
            session.add_all([first, second])
            await session.flush()
            session.add_all(
                [
                    SupplierCategory(
                        supplier_id=first.id,
                        source_name="Огнетушители",
                        normalized_name="огнетушители",
                        source_path="Пожарное оборудование / Огнетушители",
                        normalized_path="пожарное оборудование / огнетушители",
                    ),
                    SupplierCategory(
                        supplier_id=second.id,
                        source_name="ОГНЕТУШИТЕЛИ",
                        normalized_name="огнетушители",
                        source_path="Садовая техника / ОГНЕТУШИТЕЛИ",
                        normalized_path="садовая техника / огнетушители",
                    ),
                    SupplierCategory(
                        supplier_id=second.id,
                        source_name="Т" * 600,
                        normalized_name="т" * 600,
                        source_path="Т" * 600,
                        normalized_path="т" * 600,
                    ),
                ]
            )
            await session.commit()

            first_run = await bootstrap_global_category_directory(
                session, created_by="bootstrap:test"
            )
            assert first_run == {
                "created_global_categories": 5,
                "created_mappings": 3,
                "reused_global_categories": 0,
                "blocked_deprecated_paths": 0,
                "remaining_unmapped": 0,
            }
            mappings = (
                await session.execute(
                    select(SupplierCategoryMapping).join(SupplierCategory)
                )
            ).scalars().all()
            assert len(mappings) == 3
            assert mappings[0].global_category_id != mappings[1].global_category_id
            globals_ = (await session.execute(select(GlobalCategory))).scalars().all()
            assert {row.path for row in globals_} == {
                "Пожарное оборудование",
                "Пожарное оборудование / Огнетушители",
                "Садовая техника",
                "Садовая техника / ОГНЕТУШИТЕЛИ",
                "Требует разбора",
            }

            second_run = await bootstrap_global_category_directory(
                session, created_by="bootstrap:test"
            )
            assert second_run["created_global_categories"] == 0
            assert second_run["created_mappings"] == 0
            assert second_run["remaining_unmapped"] == 0
        await engine.dispose()

    asyncio.run(scenario())
    # An unspaced slash can be part of the supplier's own label and is not an
    # invented hierarchy boundary.
    assert normalize_category_path("Пожарная/охранная автоматика") == (
        "пожарная/охранная автоматика"
    )


def test_supplier_category_upsert_keeps_exact_aliases_and_global_mapping() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            supplier = Supplier(code="category-supplier", name="Поставщик")
            session.add(supplier)
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="categories.xlsx",
                storage_key="categories.xlsx",
                sha256="c" * 64,
                size_bytes=1,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id, price_file_id=price_file.id, version_no=1
            )
            session.add(version)
            await session.flush()
            bindings = await bind_supplier_categories(
                session,
                supplier_id=supplier.id,
                version_id=version.id,
                source_paths=[
                    "Огнетушители / Порошковые",
                    "огнетушители / Порошковые",
                ],
            )
            leaf = bindings["огнетушители / порошковые"]
            rows = (
                await session.execute(
                    select(SupplierCategory).where(
                        SupplierCategory.supplier_id == supplier.id
                    )
                )
            ).scalars().all()
            assert len(rows) == 2
            aliases = (
                await session.execute(
                    select(SupplierCategoryAlias).where(
                        SupplierCategoryAlias.supplier_category_id == leaf.id
                    )
                )
            ).scalars().all()
            assert {alias.source_path for alias in aliases} == {
                "Огнетушители / Порошковые",
                "огнетушители / Порошковые",
            }

            global_root = GlobalCategory(
                name="Пожарное оборудование",
                normalized_name="пожарное оборудование",
                path="Пожарное оборудование",
                normalized_path="пожарное оборудование",
            )
            session.add(global_root)
            await session.flush()
            mapping = SupplierCategoryMapping(
                supplier_category_id=leaf.id,
                global_category_id=global_root.id,
                mapping_source="manual",
                created_by="admin",
            )
            session.add(mapping)
            item = SupplierPriceItem(
                price_version_id=version.id,
                source_name="ОП-5",
                normalized_name="оп-5",
                source_article="ОП-5",
                normalized_article="ОП-5",
                category_path="Огнетушители / Порошковые",
                supplier_category_id=leaf.id,
                source_sheet="Прайс",
                source_row=4,
            )
            session.add(item)
            await session.commit()

            loaded = (
                await session.execute(
                    select(SupplierPriceItem)
                    .where(SupplierPriceItem.id == item.id)
                    .options(
                        selectinload(SupplierPriceItem.supplier_category)
                        .selectinload(SupplierCategory.mapping)
                        .selectinload(SupplierCategoryMapping.global_category)
                    )
                )
            ).scalar_one()
            # Relationships are already present in this identity map; API
            # queries additionally eager-load the same chain.
            context = item_category_context(loaded)
            assert context["source_path"] == "Огнетушители / Порошковые"
            assert context["global_path"] == "Пожарное оборудование"
            assert context["matching_scope"] == f"global:{global_root.id}"
            assert context["matching_identity"] == "ОП-5"

            long_source_path = "Методические материалы " + ("А" * 7_000)
            long_bindings = await bind_supplier_categories(
                session,
                supplier_id=supplier.id,
                version_id=version.id,
                source_paths=[long_source_path],
            )
            long_category = long_bindings[normalize_category_path(long_source_path)]
            await session.flush()
            long_alias = (
                await session.execute(
                    select(SupplierCategoryAlias).where(
                        SupplierCategoryAlias.supplier_category_id == long_category.id
                    )
                )
            ).scalar_one()
            assert long_alias.source_path == long_source_path
        await engine.dispose()

    asyncio.run(scenario())


def test_refresh_category_directory_prunes_only_stale_parsed_paths() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            supplier = Supplier(code="refresh-supplier", name="Refresh Supplier")
            session.add(supplier)
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="categories.xlsx",
                storage_key="categories.xlsx",
                sha256="r" * 64,
                size_bytes=1,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id, price_file_id=price_file.id, version_no=1
            )
            session.add(version)
            await session.flush()
            bindings = await bind_supplier_categories(
                session,
                supplier_id=supplier.id,
                version_id=version.id,
                source_paths=[
                    "Пожарное оборудование / Огнетушители",
                    "Цена / Ложная категория",
                ],
            )
            live_leaf = bindings["пожарное оборудование / огнетушители"]
            session.add(
                SupplierPriceItem(
                    price_version_id=version.id,
                    source_name="ОП-5",
                    normalized_name="оп-5",
                    category_path="Пожарное оборудование / Огнетушители",
                    supplier_category_id=live_leaf.id,
                    source_sheet="Прайс",
                    source_row=2,
                )
            )
            manual_empty = GlobalCategory(
                name="Ручной справочник",
                normalized_name="ручной справочник",
                path="Ручной справочник",
                normalized_path="ручной справочник",
            )
            session.add(manual_empty)
            await session.commit()
            await bootstrap_global_category_directory(
                session, created_by="bootstrap:test"
            )

            result = await refresh_category_directory(
                session, created_by="bootstrap:refresh-test"
            )

            assert result["removed_supplier_categories"] == 2
            assert result["removed_global_categories"] == 2
            remaining_paths = set(
                (await session.execute(select(SupplierCategory.source_path))).scalars()
            )
            assert remaining_paths == {
                "Пожарное оборудование",
                "Пожарное оборудование / Огнетушители",
            }
            remaining_globals = set(
                (await session.execute(select(GlobalCategory.name))).scalars()
            )
            assert remaining_globals == {
                "Пожарное оборудование",
                "Огнетушители",
                "Ручной справочник",
            }
            hierarchy = {
                row.path: row.parent_id
                for row in (await session.execute(select(GlobalCategory))).scalars()
            }
            assert hierarchy["Пожарное оборудование"] is None
            assert hierarchy["Пожарное оборудование / Огнетушители"] is not None
        await engine.dispose()

    asyncio.run(scenario())


def test_refresh_canonicalizes_import_mapping_but_preserves_manual_mapping() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            supplier = Supplier(code="canonical-supplier", name="Canonical")
            session.add(supplier)
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="canonical.xlsx",
                storage_key="canonical.xlsx",
                sha256="n" * 64,
                size_bytes=1,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id, price_file_id=price_file.id, version_no=1
            )
            session.add(version)
            await session.flush()
            numbered = SupplierCategory(
                supplier_id=supplier.id,
                source_name="3.6 Оптические кабели связи",
                normalized_name="3.6 оптические кабели связи",
                source_path="3.6 Оптические кабели связи",
                normalized_path="3.6 оптические кабели связи",
            )
            manual_source = SupplierCategory(
                supplier_id=supplier.id,
                source_name="Шкафы.",
                normalized_name="шкафы.",
                source_path="Шкафы.",
                normalized_path="шкафы.",
            )
            legacy_nested_manual = SupplierCategory(
                supplier_id=supplier.id,
                source_name="Хомуты",
                normalized_name="хомуты",
                source_path="Вентиляционное оборудование / Хомуты",
                normalized_path="вентиляционное оборудование / хомуты",
            )
            legacy = GlobalCategory(
                name=numbered.source_name,
                normalized_name=numbered.normalized_name,
                path=numbered.source_name,
                normalized_path=numbered.normalized_name,
            )
            reviewed = GlobalCategory(
                name="Корпусное оборудование",
                normalized_name="корпусное оборудование",
                path="Корпусное оборудование",
                normalized_path="корпусное оборудование",
            )
            flat_clamps = GlobalCategory(
                name="Хомуты",
                normalized_name="хомуты",
                path="Хомуты",
                normalized_path="хомуты",
            )
            session.add_all(
                [
                    numbered,
                    manual_source,
                    legacy_nested_manual,
                    legacy,
                    reviewed,
                    flat_clamps,
                ]
            )
            await session.flush()
            session.add_all(
                [
                    SupplierCategoryMapping(
                        supplier_category_id=numbered.id,
                        global_category_id=legacy.id,
                        mapping_source="import",
                        created_by="bootstrap:legacy",
                    ),
                    SupplierCategoryMapping(
                        supplier_category_id=manual_source.id,
                        global_category_id=reviewed.id,
                        mapping_source="manual",
                        created_by="admin",
                    ),
                    SupplierCategoryMapping(
                        supplier_category_id=legacy_nested_manual.id,
                        global_category_id=flat_clamps.id,
                        mapping_source="manual",
                        created_by="admin",
                    ),
                    SupplierPriceItem(
                        price_version_id=version.id,
                        source_name="Кабель",
                        normalized_name="кабель",
                        category_path=numbered.source_path,
                        supplier_category_id=numbered.id,
                        source_sheet="Прайс",
                        source_row=2,
                    ),
                    SupplierPriceItem(
                        price_version_id=version.id,
                        source_name="Шкаф",
                        normalized_name="шкаф",
                        category_path=manual_source.source_path,
                        supplier_category_id=manual_source.id,
                        source_sheet="Прайс",
                        source_row=3,
                    ),
                    SupplierPriceItem(
                        price_version_id=version.id,
                        source_name="Хомут",
                        normalized_name="хомут",
                        category_path=legacy_nested_manual.source_path,
                        supplier_category_id=legacy_nested_manual.id,
                        source_sheet="Прайс",
                        source_row=4,
                    ),
                ]
            )
            await session.commit()

            result = await refresh_category_directory(
                session, created_by="bootstrap:canonical-test"
            )

            assert result["remapped_import_mappings"] == 1
            assert result["remapped_legacy_flat_manual_mappings"] == 1
            mappings = (
                await session.execute(select(SupplierCategoryMapping))
            ).scalars().all()
            targets = {
                mapping.supplier_category_id: await session.get(
                    GlobalCategory, mapping.global_category_id
                )
                for mapping in mappings
            }
            assert targets[numbered.id].name == "Оптические кабели связи"
            assert targets[manual_source.id].name == "Корпусное оборудование"
            assert targets[legacy_nested_manual.id].path == (
                "Вентиляционное оборудование / Хомуты"
            )
            assert next(
                mapping
                for mapping in mappings
                if mapping.supplier_category_id == legacy_nested_manual.id
            ).mapping_source == "manual"
            assert {row.name for row in (await session.execute(select(GlobalCategory))).scalars()} == {
                "Оптические кабели связи",
                "Корпусное оборудование",
                "Вентиляционное оборудование",
                "Хомуты",
            }
        await engine.dispose()

    asyncio.run(scenario())


def test_article_only_without_category_fails_quality_gate() -> None:
    async def scenario() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            supplier = Supplier(code="gate-supplier", name="Поставщик")
            session.add(supplier)
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="gate.xlsx",
                storage_key="gate.xlsx",
                sha256="g" * 64,
                size_bytes=1,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id, price_file_id=price_file.id, version_no=1
            )
            session.add(version)
            await session.flush()
            session.add(
                SupplierPriceItem(
                    price_version_id=version.id,
                    source_name="ОП-5",
                    normalized_name="оп-5",
                    source_sheet="Прайс",
                    source_row=2,
                )
            )
            await session.flush()
            result = await apply_category_context_gate(session, version=version)
            assert result["passed"] is False
            assert result["failures"] == 1
            assert version.errors_count == 1
        await engine.dispose()

    asyncio.run(scenario())


def test_category_api_is_import_scoped_and_admin_maps_supplier_category(
    tmp_path, monkeypatch
) -> None:
    from app.config import settings

    database_path = tmp_path / "categories.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    for key, value in {
        "app_admin_username": "category-admin",
        "app_admin_password": "category-admin-password-long",
        "app_user_username": "category-viewer",
        "app_user_password": "category-viewer-password-long",
        "app_auth_secret": "category-auth-secret-that-is-long-enough-12345",
        "app_auth_secure_cookies": False,
    }.items():
        monkeypatch.setattr(settings, key, value)

    async def session_override():
        async with factory() as session:
            yield session

    async def prepare() -> uuid.UUID:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as session:
            supplier = Supplier(code="api-supplier", name="API Supplier")
            session.add(supplier)
            await session.flush()
            price_file = PriceFile(
                supplier_id=supplier.id,
                original_filename="supplier-catalog.xlsx",
                storage_key="supplier-catalog.xlsx",
                sha256="p" * 64,
                size_bytes=1,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            session.add(price_file)
            await session.flush()
            version = PriceVersion(
                supplier_id=supplier.id,
                price_file_id=price_file.id,
                version_no=7,
            )
            session.add(version)
            await session.flush()
            category = SupplierCategory(
                supplier_id=supplier.id,
                source_name="Порошковые",
                normalized_name="порошковые",
                source_path="Огнетушители / Порошковые",
                normalized_path="огнетушители / порошковые",
                first_seen_version_id=version.id,
                last_seen_version_id=version.id,
            )
            session.add(category)
            await session.flush()
            session.add_all(
                [
                    SupplierCategoryAlias(
                        supplier_category_id=category.id,
                        source_path="Огнетушители / Порошковые",
                        first_seen_version_id=version.id,
                        last_seen_version_id=version.id,
                    ),
                    SupplierPriceItem(
                        price_version_id=version.id,
                        source_name="ОП-5",
                        normalized_name="оп-5",
                        source_article="ОП-5",
                        category_path="Огнетушители / Порошковые",
                        supplier_category_id=category.id,
                        source_sheet="Прайс",
                        source_row=12,
                    ),
                ]
            )
            await session.commit()
            return category.id

    supplier_category_id = asyncio.run(prepare())
    app.dependency_overrides[get_session] = session_override
    try:
        client = TestClient(app)
        admin_token = client.post(
            "/api/v1/auth/login",
            json={
                "username": "category-admin",
                "password": "category-admin-password-long",
            },
        ).json()["access_token"]
        viewer_token = client.post(
            "/api/v1/auth/login",
            json={
                "username": "category-viewer",
                "password": "category-viewer-password-long",
            },
        ).json()["access_token"]
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        viewer_headers = {"Authorization": f"Bearer {viewer_token}"}

        denied = client.post(
            "/api/v1/catalog/categories",
            headers=viewer_headers,
            json={"name": "Огнетушители"},
        )
        assert denied.status_code == 403
        created = client.post(
            "/api/v1/catalog/categories",
            headers=admin_headers,
            json={"name": "Огнетушители"},
        )
        assert created.status_code == 201
        duplicate = client.post(
            "/api/v1/catalog/categories",
            headers=admin_headers,
            json={"name": " огнетушители "},
        )
        assert duplicate.status_code == 409
        mapped = client.put(
            f"/api/v1/catalog/supplier-categories/{supplier_category_id}/mapping",
            headers=admin_headers,
            json={"global_category_id": created.json()["id"]},
        )
        assert mapped.status_code == 200
        assert mapped.json()["global_category"]["path"] == "Огнетушители"
        provenance = client.get(
            f"/api/v1/catalog/categories/{created.json()['id']}/sources",
            headers=viewer_headers,
        )
        assert provenance.status_code == 200
        provenance_payload = provenance.json()
        assert provenance_payload["total"] == 1
        assert provenance_payload["global_category"]["path"] == "Огнетушители"
        provenance_source = provenance_payload["items"][0]
        assert provenance_source["supplier_name"] == "API Supplier"
        assert provenance_source["raw_path"] == "Огнетушители / Порошковые"
        assert provenance_source["first_seen"]["filename"] == "supplier-catalog.xlsx"
        assert provenance_source["first_seen"]["version_no"] == 7
        assert provenance_source["aliases"][0]["raw_path"] == (
            "Огнетушители / Порошковые"
        )
        assert provenance_source["item_samples"][0] == {
            "source_name": "ОП-5",
            "source_article": "ОП-5",
            "source_sheet": "Прайс",
            "source_row": 12,
            "price": provenance_source["first_seen"],
        }
        listing = client.get(
            "/api/v1/catalog/supplier-categories",
            headers=viewer_headers,
        )
        assert listing.status_code == 200
        assert listing.json()["items"][0]["mapping_source"] == "manual"
        page = client.get(
            "/api/v1/catalog/supplier-categories",
            headers=viewer_headers,
            params={"mapped": True, "query": "Порошковые", "limit": 10},
        )
        assert page.status_code == 200
        assert page.json()["total"] == 1
        assert page.json()["items"][0]["id"] == str(supplier_category_id)
        stats = client.get(
            "/api/v1/catalog/stats", headers=viewer_headers
        )
        assert stats.status_code == 200
        assert stats.json() == {
            "supplier_categories_count": 1,
            "mapped_count": 1,
            "unmapped_count": 0,
            "global_categories_count": 1,
        }
        target = client.post(
            "/api/v1/catalog/categories",
            headers=admin_headers,
            json={"name": "Пожарная безопасность"},
        )
        child = client.post(
            "/api/v1/catalog/categories",
            headers=admin_headers,
            json={
                "name": "Порошковые огнетушители",
                "parent_id": target.json()["id"],
            },
        )
        assert child.status_code == 201
        roots = client.get(
            "/api/v1/catalog/categories",
            headers=viewer_headers,
            params={"parent": "root", "limit": 10},
        )
        assert roots.status_code == 200
        assert roots.json()["total"] == 2
        assert {row["path"] for row in roots.json()["items"]} == {
            "Огнетушители",
            "Пожарная безопасность",
        }
        assert all(row["parent_id"] is None for row in roots.json()["items"])
        children = client.get(
            "/api/v1/catalog/categories",
            headers=viewer_headers,
            params={"parent": target.json()["id"]},
        )
        assert children.status_code == 200
        assert children.json()["total"] == 1
        assert children.json()["items"][0]["id"] == child.json()["id"]
        assert children.json()["items"][0]["depth"] == 1
        missing_children = client.get(
            "/api/v1/catalog/categories",
            headers=viewer_headers,
            params={"parent": str(uuid.uuid4())},
        )
        assert missing_children.status_code == 404
        merged = client.post(
            f"/api/v1/catalog/categories/{created.json()['id']}/merge",
            headers=admin_headers,
            json={"target_category_id": target.json()["id"]},
        )
        assert merged.status_code == 200
        assert merged.json()["moved_mappings"] == 1
        remapped_listing = client.get(
            "/api/v1/catalog/supplier-categories",
            headers=viewer_headers,
        )
        assert remapped_listing.json()["items"][0]["mapping_source"] == "manual"
        assert remapped_listing.json()["items"][0]["global_category"]["path"] == (
            "Пожарная безопасность"
        )
        global_page = client.get(
            "/api/v1/catalog/categories",
            headers=viewer_headers,
            params={"query": "Пожарная безопасность"},
        )
        assert global_page.status_code == 200
        assert global_page.json()["items"][0]["supplier_mappings_count"] == 1
        assert global_page.json()["items"][0]["direct_supplier_mappings_count"] == 1
        assert global_page.json()["items"][0]["product_count"] == 1
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())
