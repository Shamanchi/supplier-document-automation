"""Общий классификатор товаров и явные соответствия категориям поставщиков.

Справочник живёт на собственном префиксе `/catalog`, а не внутри
`/price-imports`: он переживает любой отдельный импорт и читается независимо от
него. Прежнее вложение было следствием запрета фронтенду ходить куда-либо, кроме
price-import контракта.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased, selectinload

from app.auth import AuthUser
from app.category_service import (
    bootstrap_global_category_directory,
    global_category_paths,
    normalize_category_name,
)
from app.db import get_session
from app.import_auth import get_import_admin, get_import_reader
from app.models import (
    GlobalCategory,
    PriceFile,
    PriceVersion,
    Project,
    ServiceClient,
    Supplier,
    SupplierCategory,
    SupplierCategoryAlias,
    SupplierCategoryMapping,
    SupplierPriceItem,
)
from app.project_context import get_current_project
from app.schemas import (
    CategoryPriceVersionOut,
    GlobalCategoryCreate,
    GlobalCategoryMergeIn,
    GlobalCategoryMergeOut,
    GlobalCategoryOut,
    GlobalCategoryPage,
    GlobalCategorySourceAliasOut,
    GlobalCategorySourceItemOut,
    GlobalCategorySourceOut,
    GlobalCategorySourcePage,
    GlobalCategorySummaryOut,
    GlobalCategoryUpdate,
    CategoryBootstrapOut,
    CategoryDirectoryStats,
    SupplierCategoryDirectorySummary,
    SupplierCategoryMappingUpdate,
    SupplierCategoryOut,
    SupplierCategoryPage,
)


router = APIRouter(prefix="/catalog", tags=["catalog"])


def _global_out(category: GlobalCategory) -> GlobalCategoryOut:
    return GlobalCategoryOut.model_validate(category)


def _supplier_out(category: SupplierCategory) -> SupplierCategoryOut:
    mapping = category.mapping
    return SupplierCategoryOut(
        id=category.id,
        supplier_id=category.supplier_id,
        supplier_name=category.supplier.name,
        parent_id=category.parent_id,
        source_name=category.source_name,
        source_path=category.source_path,
        normalized_path=category.normalized_path,
        aliases=category.aliases,
        global_category=(
            _global_out(mapping.global_category) if mapping is not None else None
        ),
        mapping_source=mapping.mapping_source if mapping is not None else None,
    )


def _supplier_scope(principal: ServiceClient | AuthUser):
    return (
        Supplier.service_client_id == principal.id
        if isinstance(principal, ServiceClient)
        else None
    )


async def _global_category_rollups(
    session: AsyncSession,
    principal: ServiceClient | AuthUser,
    category_ids: list[uuid.UUID],
    *,
    include_deprecated: bool,
) -> dict[uuid.UUID, dict[str, int]]:
    """Calculate totals only for requested nodes and their descendants."""

    if not category_ids:
        return {}
    scope = _supplier_scope(principal)
    child_count_stmt = (
        select(GlobalCategory.parent_id, func.count(GlobalCategory.id))
        .where(GlobalCategory.parent_id.in_(category_ids))
        .group_by(GlobalCategory.parent_id)
    )
    if not include_deprecated:
        child_count_stmt = child_count_stmt.where(GlobalCategory.status == "active")
    child_counts = dict(
        (
            await session.execute(child_count_stmt)
        ).all()
    )
    direct_mapping_stmt = (
        select(
            SupplierCategoryMapping.global_category_id,
            func.count(SupplierCategoryMapping.id),
        )
        .join(SupplierCategory)
        .join(Supplier)
        .where(SupplierCategoryMapping.global_category_id.in_(category_ids))
        .group_by(SupplierCategoryMapping.global_category_id)
    )
    direct_product_stmt = (
        select(
            SupplierCategoryMapping.global_category_id,
            func.count(SupplierPriceItem.id),
        )
        .select_from(SupplierCategoryMapping)
        .join(SupplierCategory)
        .join(
            SupplierPriceItem,
            SupplierPriceItem.supplier_category_id == SupplierCategory.id,
        )
        .join(Supplier)
        .where(SupplierCategoryMapping.global_category_id.in_(category_ids))
        .group_by(SupplierCategoryMapping.global_category_id)
    )
    if scope is not None:
        direct_mapping_stmt = direct_mapping_stmt.where(scope)
        direct_product_stmt = direct_product_stmt.where(scope)
    direct_mappings = dict((await session.execute(direct_mapping_stmt)).all())
    direct_products = dict((await session.execute(direct_product_stmt)).all())

    descendants = select(
        GlobalCategory.id.label("ancestor_id"),
        GlobalCategory.id.label("descendant_id"),
    ).where(GlobalCategory.id.in_(category_ids)).cte(
        "global_category_descendants", recursive=True
    )
    child = aliased(GlobalCategory)
    descendants_stmt = select(descendants.c.ancestor_id, child.id).join(
        child, child.parent_id == descendants.c.descendant_id
    )
    if not include_deprecated:
        descendants_stmt = descendants_stmt.where(child.status == "active")
    descendants = descendants.union_all(descendants_stmt)
    rollup_mapping_stmt = (
        select(descendants.c.ancestor_id, func.count(SupplierCategoryMapping.id))
        .select_from(descendants)
        .join(
            SupplierCategoryMapping,
            SupplierCategoryMapping.global_category_id == descendants.c.descendant_id,
        )
        .join(SupplierCategory)
        .join(Supplier)
        .group_by(descendants.c.ancestor_id)
    )
    rollup_product_stmt = (
        select(descendants.c.ancestor_id, func.count(SupplierPriceItem.id))
        .select_from(descendants)
        .join(
            SupplierCategoryMapping,
            SupplierCategoryMapping.global_category_id == descendants.c.descendant_id,
        )
        .join(SupplierCategory)
        .join(
            SupplierPriceItem,
            SupplierPriceItem.supplier_category_id == SupplierCategory.id,
        )
        .join(Supplier)
        .group_by(descendants.c.ancestor_id)
    )
    if scope is not None:
        rollup_mapping_stmt = rollup_mapping_stmt.where(scope)
        rollup_product_stmt = rollup_product_stmt.where(scope)
    rollup_mappings = dict((await session.execute(rollup_mapping_stmt)).all())
    rollup_products = dict((await session.execute(rollup_product_stmt)).all())

    return {
        category_id: {
            "children_count": child_counts.get(category_id, 0),
            "direct_supplier_mappings_count": direct_mappings.get(category_id, 0),
            "supplier_mappings_count": rollup_mappings.get(category_id, 0),
            "direct_product_count": direct_products.get(category_id, 0),
            "product_count": rollup_products.get(category_id, 0),
        }
        for category_id in category_ids
    }


async def _global_category_parent_page(
    session: AsyncSession,
    *,
    principal: ServiceClient | AuthUser,
    project_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    include_deprecated: bool,
    limit: int,
    offset: int,
) -> GlobalCategoryPage:
    """Return one direct tree level without loading sibling branches."""

    filters = [
        GlobalCategory.project_id == project_id,
        GlobalCategory.parent_id == parent_id
        if parent_id is not None
        else GlobalCategory.parent_id.is_(None)
    ]
    if not include_deprecated:
        filters.append(GlobalCategory.status == "active")
    total = (
        await session.execute(select(func.count(GlobalCategory.id)).where(*filters))
    ).scalar_one()
    rows = (
        await session.execute(
            select(GlobalCategory)
            .where(*filters)
            .order_by(GlobalCategory.name, GlobalCategory.id)
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    rollups = await _global_category_rollups(
        session,
        principal,
        [category.id for category in rows],
        include_deprecated=include_deprecated,
    )
    return GlobalCategoryPage(
        total=total,
        items=[
            GlobalCategorySummaryOut(
                **_global_out(category).model_dump(),
                depth=category.normalized_path.count(" / "),
                **rollups[category.id],
            )
            for category in rows
        ],
    )


async def _parent_or_404(
    session: AsyncSession, parent_id: uuid.UUID | None, *, project_id: uuid.UUID
) -> GlobalCategory | None:
    if parent_id is None:
        return None
    parent = await session.scalar(
        select(GlobalCategory).where(
            GlobalCategory.id == parent_id,
            GlobalCategory.project_id == project_id,
        )
    )
    if parent is None:
        raise HTTPException(status_code=422, detail="Родительская категория не найдена")
    if parent.status != "active":
        raise HTTPException(status_code=422, detail="Нельзя добавить узел в устаревшую категорию")
    return parent


async def _ensure_unique_sibling(
    session: AsyncSession,
    *,
    parent_id: uuid.UUID | None,
    normalized_name: str,
    project_id: uuid.UUID,
    exclude_id: uuid.UUID | None = None,
) -> None:
    conditions = [
        GlobalCategory.project_id == project_id,
        GlobalCategory.normalized_name == normalized_name,
    ]
    conditions.append(
        GlobalCategory.parent_id == parent_id
        if parent_id is not None
        else GlobalCategory.parent_id.is_(None)
    )
    if exclude_id is not None:
        conditions.append(GlobalCategory.id != exclude_id)
    duplicate = (
        await session.execute(select(GlobalCategory.id).where(*conditions))
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail="Категория с таким нормализованным именем уже есть у этого родителя",
        )


# Единственный способ прочитать общее дерево. Раньше на один запрос
# приходилось четыре ручки, отличавшиеся только фильтром по родителю:
# `/global` (всё без пагинации), `/global/page` (плоский поиск),
# `/global/roots` (parent IS NULL) и `/global/{id}/children` (parent = id).
@router.get("/categories", response_model=GlobalCategoryPage)
async def list_categories(
    parent: str | None = Query(
        default=None,
        description="`root` — только корни, UUID — прямые потомки узла, пусто — всё дерево",
    ),
    query: str | None = Query(default=None, max_length=512),
    include_deprecated: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    principal: ServiceClient | AuthUser = Depends(get_import_reader),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> GlobalCategoryPage:
    if parent is not None:
        if query:
            raise HTTPException(
                status_code=422,
                detail="Поиск идёт по всему дереву; уберите parent или query",
            )
        parent_id: uuid.UUID | None = None
        if parent != "root":
            try:
                parent_id = uuid.UUID(parent)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422, detail="parent должен быть `root` или UUID"
                ) from exc
            if await session.scalar(
                select(GlobalCategory.id).where(
                    GlobalCategory.id == parent_id,
                    GlobalCategory.project_id == project.id,
                )
            ) is None:
                raise HTTPException(status_code=404, detail="Общая категория не найдена")
        return await _global_category_parent_page(
            session,
            principal=principal,
            project_id=project.id,
            parent_id=parent_id,
            include_deprecated=include_deprecated,
            limit=limit,
            offset=offset,
        )

    filters = [GlobalCategory.project_id == project.id]
    if not include_deprecated:
        filters.append(GlobalCategory.status == "active")
    if query and query.strip():
        filters.append(GlobalCategory.path.ilike(f"%{query.strip()}%"))
    total = (
        await session.execute(select(func.count(GlobalCategory.id)).where(*filters))
    ).scalar_one()
    rows = (
        await session.execute(
            select(GlobalCategory)
            .where(*filters)
            .order_by(GlobalCategory.path)
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    rollups = await _global_category_rollups(
        session,
        principal,
        [category.id for category in rows],
        include_deprecated=include_deprecated,
    )
    return GlobalCategoryPage(
        total=total,
        items=[
            GlobalCategorySummaryOut(
                **_global_out(category).model_dump(),
                depth=category.normalized_path.count(" / "),
                **rollups[category.id],
            )
            for category in rows
        ],
    )


@router.get("/stats", response_model=CategoryDirectoryStats)
async def category_directory_stats(
    principal: ServiceClient | AuthUser = Depends(get_import_reader),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> CategoryDirectoryStats:
    scope = _supplier_scope(principal)
    base = (
        select(func.count(SupplierCategory.id))
        .join(Supplier)
        .where(SupplierCategory.project_id == project.id)
    )
    mapped = (
        select(func.count(SupplierCategoryMapping.id))
        .join(SupplierCategory)
        .join(Supplier)
        .where(SupplierCategory.project_id == project.id)
    )
    if scope is not None:
        base = base.where(scope)
        mapped = mapped.where(scope)
    total_count = (await session.execute(base)).scalar_one()
    mapped_count = (await session.execute(mapped)).scalar_one()
    global_count = (
        await session.execute(
            select(func.count(GlobalCategory.id)).where(GlobalCategory.status == "active")
            .where(GlobalCategory.project_id == project.id)
        )
    ).scalar_one()
    return CategoryDirectoryStats(
        supplier_categories_count=total_count,
        mapped_count=mapped_count,
        unmapped_count=total_count - mapped_count,
        global_categories_count=global_count,
    )


@router.get("/categories/{category_id}/sources", response_model=GlobalCategorySourcePage)
async def global_category_sources(
    category_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: ServiceClient | AuthUser = Depends(get_import_reader),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> GlobalCategorySourcePage:
    """Return bounded, exact source provenance for one global category."""

    category = await session.scalar(
        select(GlobalCategory).where(
            GlobalCategory.id == category_id,
            GlobalCategory.project_id == project.id,
        )
    )
    if category is None:
        raise HTTPException(status_code=404, detail="Общая категория не найдена")

    scope = _supplier_scope(principal)
    source_filters = [SupplierCategoryMapping.global_category_id == category_id]
    if scope is not None:
        source_filters.append(scope)
    total = (
        await session.execute(
            select(func.count(SupplierCategoryMapping.id))
            .join(SupplierCategory)
            .join(Supplier)
            .where(*source_filters)
        )
    ).scalar_one()
    product_count = (
        select(func.count(SupplierPriceItem.id))
        .where(SupplierPriceItem.supplier_category_id == SupplierCategory.id)
        .correlate(SupplierCategory)
        .scalar_subquery()
    )
    source_rows = (
        await session.execute(
            select(
                SupplierCategoryMapping,
                SupplierCategory,
                Supplier,
                product_count.label("product_count"),
            )
            .join(
                SupplierCategory,
                SupplierCategory.id == SupplierCategoryMapping.supplier_category_id,
            )
            .join(Supplier, Supplier.id == SupplierCategory.supplier_id)
            .where(*source_filters)
            .order_by(Supplier.name, SupplierCategory.source_path, SupplierCategory.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    supplier_category_ids = [
        source.id for _mapping, source, _supplier, _count in source_rows
    ]

    aliases_by_category: dict[uuid.UUID, list[SupplierCategoryAlias]] = {
        source_id: [] for source_id in supplier_category_ids
    }
    aliases: list[SupplierCategoryAlias] = []
    if supplier_category_ids:
        aliases = list(
            (
                await session.execute(
                    select(SupplierCategoryAlias)
                    .where(
                        SupplierCategoryAlias.supplier_category_id.in_(
                            supplier_category_ids
                        )
                    )
                    .order_by(
                        SupplierCategoryAlias.supplier_category_id,
                        SupplierCategoryAlias.source_path,
                    )
                )
            ).scalars()
        )
        for alias in aliases:
            aliases_by_category[alias.supplier_category_id].append(alias)

    version_ids = {
        version_id
        for _mapping, source, _supplier, _count in source_rows
        for version_id in (source.first_seen_version_id, source.last_seen_version_id)
        if version_id is not None
    }
    version_ids.update(
        version_id
        for alias in aliases
        for version_id in (alias.first_seen_version_id, alias.last_seen_version_id)
        if version_id is not None
    )
    versions: dict[uuid.UUID, CategoryPriceVersionOut] = {}
    if version_ids:
        version_rows = (
            await session.execute(
                select(PriceVersion, PriceFile)
                .join(PriceFile, PriceFile.id == PriceVersion.price_file_id)
                .where(PriceVersion.id.in_(version_ids))
            )
        ).all()
        versions = {
            version.id: CategoryPriceVersionOut(
                id=version.id,
                version_no=version.version_no,
                filename=price_file.original_filename,
                imported_at=version.created_at,
            )
            for version, price_file in version_rows
        }

    samples_by_category: dict[uuid.UUID, list[GlobalCategorySourceItemOut]] = {
        source_id: [] for source_id in supplier_category_ids
    }
    if supplier_category_ids:
        ranked_items = (
            select(
                SupplierPriceItem.supplier_category_id.label("supplier_category_id"),
                SupplierPriceItem.source_name.label("source_name"),
                SupplierPriceItem.source_article.label("source_article"),
                SupplierPriceItem.source_sheet.label("source_sheet"),
                SupplierPriceItem.source_row.label("source_row"),
                SupplierPriceItem.price_version_id.label("price_version_id"),
                func.row_number()
                .over(
                    partition_by=SupplierPriceItem.supplier_category_id,
                    order_by=(
                        SupplierPriceItem.source_sheet,
                        SupplierPriceItem.source_row,
                        SupplierPriceItem.id,
                    ),
                )
                .label("sample_rank"),
            )
            .where(SupplierPriceItem.supplier_category_id.in_(supplier_category_ids))
            .subquery()
        )
        sample_rows = (
            await session.execute(
                select(
                    ranked_items.c.supplier_category_id,
                    ranked_items.c.source_name,
                    ranked_items.c.source_article,
                    ranked_items.c.source_sheet,
                    ranked_items.c.source_row,
                    PriceVersion,
                    PriceFile,
                )
                .join(PriceVersion, PriceVersion.id == ranked_items.c.price_version_id)
                .join(PriceFile, PriceFile.id == PriceVersion.price_file_id)
                .where(ranked_items.c.sample_rank <= 3)
                .order_by(
                    ranked_items.c.supplier_category_id,
                    ranked_items.c.sample_rank,
                )
            )
        ).all()
        for (
            source_id,
            source_name,
            source_article,
            source_sheet,
            source_row,
            version,
            price_file,
        ) in sample_rows:
            samples_by_category[source_id].append(
                GlobalCategorySourceItemOut(
                    source_name=source_name,
                    source_article=source_article,
                    source_sheet=source_sheet,
                    source_row=source_row,
                    price=CategoryPriceVersionOut(
                        id=version.id,
                        version_no=version.version_no,
                        filename=price_file.original_filename,
                        imported_at=version.created_at,
                    ),
                )
            )

    return GlobalCategorySourcePage(
        global_category=_global_out(category),
        total=total,
        items=[
            GlobalCategorySourceOut(
                supplier_category_id=source.id,
                supplier_id=supplier.id,
                supplier_code=supplier.code,
                supplier_name=supplier.name,
                mapping_source=mapping.mapping_source,
                raw_name=source.source_name,
                raw_path=source.source_path,
                normalized_path=source.normalized_path,
                product_count=count,
                first_seen=versions.get(source.first_seen_version_id),
                last_seen=versions.get(source.last_seen_version_id),
                aliases=[
                    GlobalCategorySourceAliasOut(
                        raw_path=alias.source_path,
                        first_seen=versions.get(alias.first_seen_version_id),
                        last_seen=versions.get(alias.last_seen_version_id),
                    )
                    for alias in aliases_by_category[source.id]
                ],
                item_samples=samples_by_category[source.id],
            )
            for mapping, source, supplier, count in source_rows
        ],
    )


@router.post("/bootstrap", response_model=CategoryBootstrapOut)
async def bootstrap_category_directory(
    admin: AuthUser = Depends(get_import_admin),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> CategoryBootstrapOut:
    result = await bootstrap_global_category_directory(
        session, created_by=f"bootstrap:{admin.username}", project_id=project.id
    )
    return CategoryBootstrapOut(**result)


@router.post("/categories", response_model=GlobalCategoryOut, status_code=201)
async def create_global_category(
    payload: GlobalCategoryCreate,
    _admin: AuthUser = Depends(get_import_admin),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> GlobalCategoryOut:
    parent = await _parent_or_404(session, payload.parent_id, project_id=project.id)
    name, path, normalized_path = global_category_paths(parent=parent, name=payload.name)
    normalized_name = normalize_category_name(name)
    await _ensure_unique_sibling(
        session,
        parent_id=payload.parent_id,
        normalized_name=normalized_name,
        project_id=project.id,
    )
    category = GlobalCategory(
        project_id=project.id,
        parent_id=payload.parent_id,
        name=name,
        normalized_name=normalized_name,
        path=path,
        normalized_path=normalized_path,
    )
    session.add(category)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Дубликат общей категории") from exc
    await session.refresh(category)
    return _global_out(category)


@router.put("/categories/{category_id}", response_model=GlobalCategoryOut)
async def update_global_category(
    category_id: uuid.UUID,
    payload: GlobalCategoryUpdate,
    _admin: AuthUser = Depends(get_import_admin),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> GlobalCategoryOut:
    category = await session.scalar(
        select(GlobalCategory).where(
            GlobalCategory.id == category_id,
            GlobalCategory.project_id == project.id,
        )
    )
    if category is None:
        raise HTTPException(status_code=404, detail="Общая категория не найдена")
    all_categories = (
        await session.execute(
            select(GlobalCategory).where(GlobalCategory.project_id == project.id)
        )
    ).scalars().all()
    by_parent: dict[uuid.UUID | None, list[GlobalCategory]] = {}
    for row in all_categories:
        by_parent.setdefault(row.parent_id, []).append(row)

    descendants: set[uuid.UUID] = set()

    def collect(parent_id: uuid.UUID) -> None:
        for child in by_parent.get(parent_id, []):
            descendants.add(child.id)
            collect(child.id)

    collect(category.id)
    if payload.parent_id == category.id or payload.parent_id in descendants:
        raise HTTPException(status_code=422, detail="Цикл в дереве категорий запрещён")
    parent = await _parent_or_404(session, payload.parent_id, project_id=project.id)
    name, path, normalized_path = global_category_paths(parent=parent, name=payload.name)
    normalized_name = normalize_category_name(name)
    await _ensure_unique_sibling(
        session,
        parent_id=payload.parent_id,
        normalized_name=normalized_name,
        project_id=project.id,
        exclude_id=category.id,
    )
    category.parent_id = payload.parent_id
    category.name = name
    category.normalized_name = normalized_name
    category.path = path
    category.normalized_path = normalized_path
    category.status = payload.status

    def rebuild_children(parent_row: GlobalCategory) -> None:
        for child in by_parent.get(parent_row.id, []):
            child.path = f"{parent_row.path} / {child.name}"
            child.normalized_path = (
                f"{parent_row.normalized_path} / {child.normalized_name}"
            )
            rebuild_children(child)

    rebuild_children(category)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Дубликат общей категории") from exc
    return _global_out(category)


@router.post("/categories/{category_id}/merge", response_model=GlobalCategoryMergeOut)
async def merge_global_category(
    category_id: uuid.UUID,
    payload: GlobalCategoryMergeIn,
    admin: AuthUser = Depends(get_import_admin),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> GlobalCategoryMergeOut:
    if category_id == payload.target_category_id:
        raise HTTPException(status_code=422, detail="Нельзя объединить категорию с самой собой")
    source = await session.scalar(
        select(GlobalCategory).where(
            GlobalCategory.id == category_id,
            GlobalCategory.project_id == project.id,
        )
    )
    target = await session.scalar(
        select(GlobalCategory).where(
            GlobalCategory.id == payload.target_category_id,
            GlobalCategory.project_id == project.id,
        )
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Исходная общая категория не найдена")
    if target is None or target.status != "active":
        raise HTTPException(status_code=422, detail="Активная целевая категория не найдена")
    active_children = (
        await session.execute(
            select(func.count(GlobalCategory.id)).where(
                GlobalCategory.parent_id == source.id,
                GlobalCategory.status == "active",
            )
        )
    ).scalar_one()
    if active_children:
        raise HTTPException(
            status_code=409,
            detail="Сначала перенесите или объедините дочерние категории",
        )
    moved = (
        await session.execute(
            update(SupplierCategoryMapping)
            .where(SupplierCategoryMapping.global_category_id == source.id)
            .values(
                global_category_id=target.id,
                mapping_source="manual",
                created_by=admin.username,
            )
        )
    ).rowcount
    source.status = "deprecated"
    await session.commit()
    return GlobalCategoryMergeOut(
        source_category_id=source.id,
        target_category=_global_out(target),
        moved_mappings=moved or 0,
    )


@router.get("/suppliers", response_model=list[SupplierCategoryDirectorySummary])
async def list_category_suppliers(
    principal: ServiceClient | AuthUser = Depends(get_import_reader),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> list[SupplierCategoryDirectorySummary]:
    scope = _supplier_scope(principal)
    stmt = (
        select(Supplier)
        .join(SupplierCategory)
        .where(SupplierCategory.project_id == project.id)
        .distinct()
        .order_by(Supplier.name)
    )
    if scope is not None:
        stmt = stmt.where(scope)
    suppliers = (await session.execute(stmt)).scalars().all()
    result = []
    for supplier in suppliers:
        total = (
            await session.execute(
                select(func.count(SupplierCategory.id)).where(
                    SupplierCategory.supplier_id == supplier.id,
                    SupplierCategory.project_id == project.id,
                )
            )
        ).scalar_one()
        mapped = (
            await session.execute(
                select(func.count(SupplierCategoryMapping.id))
                .join(SupplierCategory)
                .where(SupplierCategory.supplier_id == supplier.id)
                .where(SupplierCategory.project_id == project.id)
            )
        ).scalar_one()
        result.append(
            SupplierCategoryDirectorySummary(
                supplier_id=supplier.id,
                supplier_name=supplier.name,
                supplier_code=supplier.code,
                categories_count=total,
                unmapped_count=total - mapped,
            )
        )
    return result


# Беспагинационный близнец этой ручки отдавал те же строки без счётчика и без
# поиска; на поставщике с тысячей категорий он выгружал всё разом.
@router.get("/supplier-categories", response_model=SupplierCategoryPage)
async def list_supplier_categories(
    supplier_id: uuid.UUID | None = Query(default=None),
    mapped: bool | None = Query(default=None),
    query: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: ServiceClient | AuthUser = Depends(get_import_reader),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> SupplierCategoryPage:
    filters = [SupplierCategory.project_id == project.id]
    scope = _supplier_scope(principal)
    if scope is not None:
        filters.append(scope)
    if supplier_id is not None:
        filters.append(SupplierCategory.supplier_id == supplier_id)
    if query and query.strip():
        pattern = f"%{query.strip()}%"
        filters.append(
            or_(
                SupplierCategory.source_path.ilike(pattern),
                SupplierCategory.source_name.ilike(pattern),
                Supplier.name.ilike(pattern),
            )
        )

    base = select(SupplierCategory).join(Supplier)
    count_stmt = select(func.count(SupplierCategory.id)).join(Supplier)
    if mapped is True:
        base = base.join(SupplierCategoryMapping)
        count_stmt = count_stmt.join(SupplierCategoryMapping)
    elif mapped is False:
        base = base.outerjoin(SupplierCategoryMapping)
        count_stmt = count_stmt.outerjoin(SupplierCategoryMapping)
        filters.append(SupplierCategoryMapping.id.is_(None))
    total = (await session.execute(count_stmt.where(*filters))).scalar_one()
    rows = (
        await session.execute(
            base.where(*filters)
            .options(
                selectinload(SupplierCategory.supplier),
                selectinload(SupplierCategory.aliases),
                selectinload(SupplierCategory.mapping).selectinload(
                    SupplierCategoryMapping.global_category
                ),
            )
            .order_by(Supplier.name, SupplierCategory.source_path)
            .limit(limit)
            .offset(offset)
        )
    ).scalars().unique().all()
    return SupplierCategoryPage(total=total, items=[_supplier_out(row) for row in rows])


@router.put(
    "/supplier-categories/{category_id}/mapping", response_model=SupplierCategoryOut
)
async def update_supplier_category_mapping(
    category_id: uuid.UUID,
    payload: SupplierCategoryMappingUpdate,
    admin: AuthUser = Depends(get_import_admin),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> SupplierCategoryOut:
    category = (
        await session.execute(
            select(SupplierCategory)
            .where(
                SupplierCategory.id == category_id,
                SupplierCategory.project_id == project.id,
            )
            .options(
                selectinload(SupplierCategory.supplier),
                selectinload(SupplierCategory.aliases),
                selectinload(SupplierCategory.mapping).selectinload(
                    SupplierCategoryMapping.global_category
                ),
            )
        )
    ).scalar_one_or_none()
    if category is None:
        raise HTTPException(status_code=404, detail="Категория поставщика не найдена")
    mapping = category.mapping
    if payload.global_category_id is None:
        if mapping is not None:
            await session.delete(mapping)
            await session.flush()
            category.mapping = None
    else:
        global_category = await session.scalar(
            select(GlobalCategory).where(
                GlobalCategory.id == payload.global_category_id,
                GlobalCategory.project_id == project.id,
            )
        )
        if global_category is None or global_category.status != "active":
            raise HTTPException(status_code=422, detail="Активная общая категория не найдена")
        if mapping is None:
            mapping = SupplierCategoryMapping(
                supplier_category_id=category.id,
                global_category_id=global_category.id,
                mapping_source="manual",
                created_by=admin.username,
            )
            session.add(mapping)
            category.mapping = mapping
        else:
            mapping.global_category_id = global_category.id
            mapping.mapping_source = "manual"
            mapping.created_by = admin.username
        mapping.global_category = global_category
    await session.commit()
    return _supplier_out(category)
