"""Версии прайса, доступные вызывающему: список, оригинал, предпросмотр.

Обработчики scoped: сервисный клиент видит только версии собственных импортов.
Остальные ручки `/price-versions` — в `versions.py`; общий у них только
префикс, поэтому оба модуля монтируют свои роутеры отдельно.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.auth import AuthUser
from app.db import get_session
from app.import_auth import get_import_reader
from app.models import (
    PriceFile,
    PriceImport,
    PriceVersion,
    Project,
    ServiceClient,
    Supplier,
)
from app.project_context import get_current_project
from app.schemas import (
    ImportedPriceVersionOut,
    ImportedPriceVersionsPage,
    PriceVersionPreview,
    PriceVersionPreviewSheet,
    ReviewSourceCell,
    ReviewSourceRow,
)


router = APIRouter(prefix="/price-versions", tags=["price-versions"])


def _original_file_disposition(filename: str) -> str:
    return f"attachment; filename*=UTF-8''{quote(filename)}"


async def _scoped_price_version_file(
    session: AsyncSession,
    principal: ServiceClient | AuthUser,
    version_id: uuid.UUID,
) -> tuple[PriceVersion, PriceFile]:
    conditions = [PriceVersion.id == version_id]
    if isinstance(principal, ServiceClient):
        conditions.append(Supplier.service_client_id == principal.id)
    row = (
        await session.execute(
            select(PriceVersion, PriceFile)
            .join(PriceFile, PriceFile.id == PriceVersion.price_file_id)
            .join(Supplier, Supplier.id == PriceVersion.supplier_id)
            .where(*conditions)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Оригинальный файл не найден")
    version, price_file = row
    if not price_file.storage_key:
        raise HTTPException(status_code=404, detail="Оригинальный файл не найден")
    return version, price_file


def _preview_cell_value(value: object | None) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    return str(value)


def _preview_excel_column(index: int) -> str:
    value = index + 1
    label = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        label = chr(65 + remainder) + label
    return label

@router.get("", response_model=ImportedPriceVersionsPage)
async def list_imported_versions(
    query: str | None = Query(default=None, max_length=255),
    supplier_id: uuid.UUID | None = None,
    has_items: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: ServiceClient | AuthUser = Depends(get_import_reader),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> ImportedPriceVersionsPage:
    """List every stored price version, not only versions created by import jobs."""

    joins = (
        PriceVersion.__table__
        .join(Supplier, Supplier.id == PriceVersion.supplier_id)
        .join(PriceFile, PriceFile.id == PriceVersion.price_file_id)
        .outerjoin(PriceImport, PriceImport.price_version_id == PriceVersion.id)
    )
    conditions = [PriceFile.project_id == project.id]
    if isinstance(principal, ServiceClient):
        conditions.append(Supplier.service_client_id == principal.id)
    if supplier_id is not None:
        conditions.append(PriceVersion.supplier_id == supplier_id)
    if has_items is True:
        conditions.append(PriceVersion.item_count > 0)
    elif has_items is False:
        conditions.append(PriceVersion.item_count == 0)
    if query and (search := query.strip()):
        pattern = f"%{search}%"
        conditions.append(
            or_(
                Supplier.name.ilike(pattern),
                Supplier.code.ilike(pattern),
                Supplier.external_id.ilike(pattern),
                PriceFile.original_filename.ilike(pattern),
            )
        )

    total = int(
        await session.scalar(select(func.count(PriceVersion.id)).select_from(joins).where(*conditions))
        or 0
    )
    rows = (
        await session.execute(
            select(PriceVersion, Supplier, PriceFile, PriceImport.id)
            .select_from(joins)
            .where(*conditions)
            .order_by(PriceVersion.created_at.desc(), PriceVersion.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return ImportedPriceVersionsPage(
        total=total,
        items=[
            ImportedPriceVersionOut(
                id=version.id,
                supplier_id=supplier.id,
                supplier_external_id=supplier.external_id,
                supplier_name=supplier.name,
                version_no=version.version_no,
                status=version.status,
                document_type=version.document_type,
                check_batch_id=version.check_batch_id,
                loader=version.loader,
                filename=price_file.original_filename,
                item_count=version.item_count,
                warnings_count=version.warnings_count,
                errors_count=version.errors_count,
                created_at=version.created_at,
                activated_at=version.activated_at,
                import_id=import_id,
            )
            for version, supplier, price_file, import_id in rows
        ],
    )


@router.get("/{version_id}/original")
async def download_price_version_original(
    version_id: uuid.UUID,
    principal: ServiceClient | AuthUser = Depends(get_import_reader),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Stream one source file through the scoped import API."""

    _version, price_file = await _scoped_price_version_file(
        session, principal, version_id
    )
    data = storage.get_price_file(price_file.storage_key)
    return Response(
        content=data,
        media_type=price_file.mime or "application/octet-stream",
        headers={
            "Content-Disposition": _original_file_disposition(
                price_file.original_filename
            )
        },
    )


@router.get("/{version_id}/preview", response_model=PriceVersionPreview)
async def preview_price_version(
    version_id: uuid.UUID,
    sheet_index: int = Query(default=0, ge=0),
    row_offset: int = Query(default=0, ge=0),
    row_limit: int = Query(default=50, ge=1, le=200),
    principal: ServiceClient | AuthUser = Depends(get_import_reader),
    session: AsyncSession = Depends(get_session),
) -> PriceVersionPreview:
    """Return a bounded source-grid page for the in-app workbook viewer."""

    from app.review_validation import load_source

    version, price_file = await _scoped_price_version_file(
        session, principal, version_id
    )
    data = storage.get_price_file(price_file.storage_key)
    _loader, grids = await asyncio.to_thread(
        load_source, data, price_file.original_filename
    )
    if not grids:
        raise HTTPException(status_code=422, detail="В файле нет доступных листов")
    if sheet_index >= len(grids):
        raise HTTPException(status_code=422, detail="Лист прайса не найден")

    grid = grids[sheet_index]
    rows = []
    for row_index, source_row in enumerate(
        grid.rows[row_offset : row_offset + row_limit], start=row_offset
    ):
        rows.append(
            ReviewSourceRow(
                row_index=row_index,
                excel_row=row_index + 1,
                kind="source",
                cells=[
                    ReviewSourceCell(
                        column_index=column_index,
                        excel_column=_preview_excel_column(column_index),
                        value=_preview_cell_value(
                            cell.display_value
                            if cell.display_value is not None
                            else cell.value
                        ),
                        is_bold=cell.is_bold,
                        is_merged_fill=cell.is_merged_fill,
                    )
                    for column_index, cell in enumerate(source_row)
                ],
            )
        )
    return PriceVersionPreview(
        version_id=version.id,
        filename=price_file.original_filename,
        sheets=[
            PriceVersionPreviewSheet(
                index=index,
                name=sheet.name,
                row_count=len(sheet.rows),
                column_count=sheet.n_cols,
            )
            for index, sheet in enumerate(grids)
        ],
        selected_sheet_index=sheet_index,
        selected_sheet_name=grid.name,
        total_rows=len(grid.rows),
        total_columns=grid.n_cols,
        row_offset=row_offset,
        row_limit=row_limit,
        rows=rows,
    )
