"""Разобранные строки прайсов поверх всех версий.

Позиция живёт дольше задания, которое её породило, поэтому ресурс отдельный:
поиск идёт по всем версиям сразу, а категория присваивается конкретной строке.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.versions import item_load_options, item_to_dto
from app.auth import AuthUser
from app.category_service import suggest_category_for_name
from app.db import get_session
from app.import_auth import get_import_admin, get_import_reader
from app.models import (
    GlobalCategory,
    PriceFile,
    PriceVersion,
    Project,
    ServiceClient,
    Supplier,
    SupplierPriceItem,
)
from app.project_context import get_current_project
from app.schemas import (
    AutoClassifyIn,
    AutoClassifyOut,
    AutoClassifySuggestion,
    ImportedItemOut,
    ImportedItemsPage,
    ItemCategoryAssignIn,
    ItemOut,
)


router = APIRouter(prefix="/price-items", tags=["price-items"])


@router.get("", response_model=ImportedItemsPage)
async def list_imported_items(
    query: str | None = Query(default=None, max_length=255),
    supplier_id: uuid.UUID | None = Query(default=None),
    price_version_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: ServiceClient | AuthUser = Depends(get_import_reader),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> ImportedItemsPage:
    """List parsed data even when it predates the PriceImport job workflow."""

    joins = (
        SupplierPriceItem.__table__
        .join(PriceVersion, PriceVersion.id == SupplierPriceItem.price_version_id)
        .join(Supplier, Supplier.id == PriceVersion.supplier_id)
        .join(PriceFile, PriceFile.id == PriceVersion.price_file_id)
    )
    conditions = [PriceFile.project_id == project.id]
    if isinstance(principal, ServiceClient):
        conditions.append(Supplier.service_client_id == principal.id)
    if supplier_id is not None:
        conditions.append(Supplier.id == supplier_id)
    if price_version_id is not None:
        conditions.append(PriceVersion.id == price_version_id)
    if query and (search := query.strip()):
        pattern = f"%{search}%"
        conditions.append(
            or_(
                SupplierPriceItem.source_name.ilike(pattern),
                SupplierPriceItem.source_article.ilike(pattern),
                Supplier.name.ilike(pattern),
                PriceFile.original_filename.ilike(pattern),
            )
        )

    total = int(
        await session.scalar(
            select(func.count(SupplierPriceItem.id)).select_from(joins).where(*conditions)
        )
        or 0
    )
    rows = (
        await session.execute(
            select(SupplierPriceItem, PriceVersion, Supplier, PriceFile)
            .select_from(joins)
            .where(*conditions)
            .order_by(
                PriceVersion.created_at.desc(),
                Supplier.name,
                SupplierPriceItem.source_sheet,
                SupplierPriceItem.source_row,
                func.coalesce(SupplierPriceItem.source_panel_index, -1),
                SupplierPriceItem.id,
            )
            .limit(limit)
            .offset(offset)
            .options(*item_load_options())
        )
    ).all()

    items = []
    for item, version, supplier, price_file in rows:
        dto = item_to_dto(item)
        items.append(
            ImportedItemOut(
                **dto.model_dump(),
                supplier_id=supplier.id,
                supplier_name=supplier.name,
                price_version_id=version.id,
                version_no=version.version_no,
                version_status=version.status,
                filename=price_file.original_filename,
                imported_at=version.created_at,
            )
        )
    return ImportedItemsPage(total=total, items=items)


@router.put("/{item_id}", response_model=ItemOut)
async def assign_item_category(
    item_id: uuid.UUID,
    payload: ItemCategoryAssignIn,
    admin: AuthUser = Depends(get_import_admin),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> ItemOut:
    """Set or clear the operator-assigned global category for one position."""

    item = (
        await session.execute(
            select(SupplierPriceItem)
            .join(PriceVersion, PriceVersion.id == SupplierPriceItem.price_version_id)
            .join(PriceFile, PriceFile.id == PriceVersion.price_file_id)
            .where(SupplierPriceItem.id == item_id, PriceFile.project_id == project.id)
            .options(*item_load_options())
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")

    if payload.global_category_id is not None:
        category = await session.get(GlobalCategory, payload.global_category_id)
        if category is None or category.project_id != project.id:
            raise HTTPException(status_code=404, detail="Global category not found")
    item.manual_category_id = payload.global_category_id
    await session.commit()
    await session.refresh(item)
    return item_to_dto(item)


@router.post("/auto-classify", response_model=AutoClassifyOut)
async def auto_classify_items(
    payload: AutoClassifyIn,
    admin: AuthUser = Depends(get_import_admin),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> AutoClassifyOut:
    """Suggest (and optionally apply) global categories for uncategorized items.

    Deterministic prototype: token-overlap scoring against active global
    category names.  Only items without any resolved category are considered.
    """

    version = (
        await session.execute(
            select(PriceVersion)
            .join(PriceFile, PriceFile.id == PriceVersion.price_file_id)
            .where(PriceVersion.id == payload.price_version_id)
            .where(PriceFile.project_id == project.id)
        )
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="Price version not found")

    categories = list(
        (
            await session.execute(
                select(GlobalCategory).where(
                    GlobalCategory.project_id == project.id,
                    GlobalCategory.status == "active",
                )
            )
        ).scalars()
    )
    items = list(
        (
            await session.execute(
                select(SupplierPriceItem)
                .where(
                    SupplierPriceItem.price_version_id == version.id,
                    SupplierPriceItem.manual_category_id.is_(None),
                )
                .options(*item_load_options())
            )
        ).scalars()
    )
    uncategorized = [
        item
        for item in items
        if item.supplier_category is None or item.supplier_category.mapping is None
    ]

    suggestions: list[AutoClassifySuggestion] = []
    scored: list[tuple[SupplierPriceItem, GlobalCategory, float]] = []
    for item in uncategorized:
        guess = suggest_category_for_name(item.source_name, categories)
        if guess is None or guess[1] < payload.min_score:
            continue
        category, score = guess
        scored.append((item, category, score))
    scored.sort(key=lambda entry: (-entry[2], entry[0].source_name))
    for item, category, score in scored[: payload.limit]:
        suggestions.append(
            AutoClassifySuggestion(
                item_id=item.id,
                source_name=item.source_name,
                suggested_category_id=category.id,
                suggested_path=category.path,
                score=score,
            )
        )
        if payload.apply:
            item.manual_category_id = category.id
    if payload.apply:
        await session.commit()
    return AutoClassifyOut(
        considered=len(uncategorized),
        suggestions=suggestions,
        applied=len(suggestions) if payload.apply else 0,
    )
