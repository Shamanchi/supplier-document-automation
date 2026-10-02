"""Supplier endpoints: list/create suppliers, upload price files, list versions."""
from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.db import get_session
from app.models import PriceFile, PriceVersion, Supplier, VersionStatus
from app.schemas import (
    ActiveVersionBrief,
    SupplierCreate,
    SupplierOut,
    UploadAccepted,
    VersionOut,
)

router = APIRouter(tags=["suppliers"])

ALLOWED_EXTENSIONS = (".xlsx", ".xlsm", ".xls", ".pdf", ".docx", ".doc", ".txt", ".csv")


def version_to_dto(version: PriceVersion, original_filename: str) -> VersionOut:
    return VersionOut(
        id=version.id,
        supplier_id=version.supplier_id,
        version_no=version.version_no,
        status=version.status,
        item_count=version.item_count,
        rows_accepted=version.rows_accepted,
        warnings_count=version.warnings_count,
        errors_count=version.errors_count,
        quality_score=version.quality_score,
        original_filename=original_filename,
        created_at=version.created_at,
        activated_at=version.activated_at,
        error_message=version.error_message,
        loader=version.loader,
        rule_match=version.rule_match,
        parse_rule_id=version.parse_rule_id,
        rule_generation=version.rule_generation,
    )


@router.get("/suppliers", response_model=list[SupplierOut])
async def list_suppliers(session: AsyncSession = Depends(get_session)) -> list[SupplierOut]:
    suppliers = (await session.execute(select(Supplier).order_by(Supplier.code))).scalars().all()
    counts = dict(
        (
            await session.execute(
                select(PriceVersion.supplier_id, func.count(PriceVersion.id)).group_by(
                    PriceVersion.supplier_id
                )
            )
        ).all()
    )
    active_versions = {
        v.supplier_id: v
        for v in (
            await session.execute(
                select(PriceVersion).where(PriceVersion.status == VersionStatus.active.value)
            )
        ).scalars()
    }
    out: list[SupplierOut] = []
    for s in suppliers:
        active = active_versions.get(s.id)
        out.append(
            SupplierOut(
                id=s.id,
                code=s.code,
                name=s.name,
                versions_count=counts.get(s.id, 0),
                active_version=ActiveVersionBrief(
                    id=active.id,
                    version_no=active.version_no,
                    item_count=active.item_count,
                    activated_at=active.activated_at,
                )
                if active
                else None,
            )
        )
    return out


@router.post("/suppliers", response_model=SupplierOut, status_code=201)
async def create_supplier(
    payload: SupplierCreate, session: AsyncSession = Depends(get_session)
) -> SupplierOut:
    existing = (
        await session.execute(select(Supplier).where(Supplier.code == payload.code))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Поставщик с таким кодом уже существует")
    supplier = Supplier(code=payload.code, name=payload.name)
    session.add(supplier)
    await session.commit()
    return SupplierOut(id=supplier.id, code=supplier.code, name=supplier.name)


@router.post(
    "/suppliers/{supplier_id}/price-uploads",
    response_model=UploadAccepted,
    status_code=202,
)
async def upload_price(
    supplier_id: uuid.UUID,
    file: UploadFile,
    session: AsyncSession = Depends(get_session),
) -> UploadAccepted:
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(status_code=404, detail="Поставщик не найден")

    filename = file.filename or "price.xlsx"
    if not filename.lower().endswith(ALLOWED_EXTENSIONS):
        raise HTTPException(
            status_code=422,
            detail="Поддерживаются файлы .xlsx/.xlsm/.xls/.pdf/.docx/.doc/.txt/.csv",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Пустой файл")
    sha256 = hashlib.sha256(data).hexdigest()

    duplicate = (
        await session.execute(
            select(PriceVersion.id)
            .join(PriceFile, PriceVersion.price_file_id == PriceFile.id)
            .where(
                PriceVersion.supplier_id == supplier_id,
                PriceVersion.status == VersionStatus.active.value,
                PriceFile.sha256 == sha256,
            )
        )
    ).first()
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail="Такой файл уже загружен и является активной версией этого поставщика",
        )

    storage_key = storage.put_price_file(
        data, sha256, filename, file.content_type or "application/octet-stream"
    )

    price_file = PriceFile(
        supplier_id=supplier_id,
        original_filename=filename,
        storage_key=storage_key,
        sha256=sha256,
        size_bytes=len(data),
        mime=file.content_type or "application/octet-stream",
    )
    session.add(price_file)
    await session.flush()

    max_no = (
        await session.execute(
            select(func.coalesce(func.max(PriceVersion.version_no), 0)).where(
                PriceVersion.supplier_id == supplier_id
            )
        )
    ).scalar_one()
    version = PriceVersion(
        supplier_id=supplier_id,
        price_file_id=price_file.id,
        version_no=max_no + 1,
        status=VersionStatus.uploaded.value,
    )
    session.add(version)
    await session.commit()

    from app.tasks import parse_price_version

    async_result = parse_price_version.delay(str(version.id))
    return UploadAccepted(price_version_id=version.id, job_id=async_result.id)


# Список версий поставщика — это `GET /price-versions?supplier_id=…`: тот же
# набор строк, но с пагинацией, поиском и scoping по сервисному клиенту.
