"""Расчёты: скидки поставщиков, позиции, ручная корректировка цены."""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.import_auth import get_import_admin, get_import_reader
from app.matching.calculations import CalculationService, LineInput
from app.matching.discounts import DiscountService
from app.matching.export import build_quote_workbook
from app.matching.physical import import_reference
from app.models import (
    Calculation,
    CalculationItem,
    DiscountRule,
    DiscountScope,
    ProductPhysicalReference,
    Project,
    Supplier,
)
from app.project_context import get_current_project

router = APIRouter(prefix="/calculations", tags=["calculations"])


def _decimal(value: object, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"поле {field} должно быть числом")


def _rule_dict(rule: DiscountRule) -> dict[str, object]:
    return {
        "id": str(rule.id),
        "supplier_id": str(rule.supplier_id),
        "scope": rule.scope,
        "category_id": str(rule.category_id) if rule.category_id else None,
        "normalized_article": rule.normalized_article,
        "min_amount": float(rule.min_amount) if rule.min_amount is not None else None,
        "percent": float(rule.percent),
        "is_active": rule.is_active,
        "note": rule.note,
    }


def _item_dict(row) -> dict[str, object]:
    def money(value) -> float | None:
        return float(value) if value is not None else None

    return {
        "id": str(row.id),
        "ordinal": row.ordinal,
        "source_line": row.source_line,
        "name": row.name,
        "supplier_id": str(row.supplier_id) if row.supplier_id else None,
        "supplier_name": row.supplier_name,
        "quantity": float(row.quantity),
        "unit_code": row.unit_code,
        # Снимок источника цены: по нему видно, из какого прайса/счёта взята
        # цена и почему именно столько — даже после смены активной версии.
        "price_version_id": str(row.price_version_id) if row.price_version_id else None,
        "price_document_id": (
            str(row.price_document_id) if row.price_document_id else None
        ),
        "price_document_type": row.price_document_type,
        "price_source": row.price_source,
        "price_kind": row.price_kind,
        "price_tier_min_quantity": (
            float(row.price_tier_min_quantity)
            if row.price_tier_min_quantity is not None
            else None
        ),
        "price_tier_max_quantity": (
            float(row.price_tier_max_quantity)
            if row.price_tier_max_quantity is not None
            else None
        ),
        "base_price": money(row.base_price),
        "currency": row.currency,
        "weight_kg": money(row.weight_kg),
        "volume_m3": money(row.volume_m3),
        "weight_source": row.weight_source,
        "volume_source": row.volume_source,
        "certificate_number": row.certificate_number,
        "certificate_valid_until": (
            row.certificate_valid_until.isoformat()
            if row.certificate_valid_until
            else None
        ),
        "certificate_confirmed_by": row.certificate_confirmed_by,
        "certificate_reference": row.certificate_reference,
        "discount_percent": float(row.discount_percent),
        "discount_amount": money(row.discount_amount),
        "discount_source": row.discount_source,
        "price_after_discount": money(row.price_after_discount),
        "vat_rate_percent": money(row.vat_rate_percent),
        "vat_mode": row.vat_mode,
        "price_without_vat": money(row.price_without_vat),
        "vat_amount": money(row.vat_amount),
        "price_with_vat": money(row.price_with_vat),
        "line_total_without_vat": money(row.line_total_without_vat),
        "line_vat_amount": money(row.line_vat_amount),
        "line_total_with_vat": money(row.line_total_with_vat),
        "manual_price": money(row.manual_price),
        "manual_reason": row.manual_reason,
        "manual_by": row.manual_by,
        "manual_at": row.manual_at.isoformat() if row.manual_at else None,
        # Цена, которая уйдёт в КП: ручная, если менеджер её задал.
        "final_price": money(
            row.manual_price if row.manual_price is not None else row.price_after_discount
        ),
    }


def _calculation_dict(calculation: Calculation, totals=None) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": str(calculation.id),
        "request_id": str(calculation.request_id)
        if calculation.request_id is not None
        else None,
        "title": calculation.title,
        "status": calculation.status,
        "total_amount": float(calculation.total_amount)
        if calculation.total_amount is not None
        else None,
        "items": [_item_dict(row) for row in sorted(calculation.items, key=lambda r: r.ordinal)],
    }
    if totals is not None:
        payload["totals"] = {
            "base": float(totals.base),
            "discounted": float(totals.discounted),
            "final": float(totals.final),
            "total_discount_amount": float(totals.total_discount_amount),
            "total_without_vat": float(totals.total_without_vat),
            "total_vat_amount": float(totals.total_vat_amount),
            "total_with_vat": float(totals.total_with_vat),
            "by_supplier": {key: float(value) for key, value in totals.by_supplier.items()},
            "weight_kg": float(totals.weight_kg),
            "volume_m3": float(totals.volume_m3),
        }
    return payload


async def _project_calculation(
    session: AsyncSession, calculation_id: uuid.UUID, project_id: uuid.UUID
) -> Calculation:
    calculation = await session.scalar(
        select(Calculation).where(
            Calculation.id == calculation_id, Calculation.project_id == project_id
        )
    )
    if calculation is None:
        raise HTTPException(status_code=404, detail="расчёт не найден")
    return calculation


@router.get("/discounts")
async def list_discounts(
    supplier_id: uuid.UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _reader=Depends(get_import_reader),
) -> dict[str, object]:
    query = select(DiscountRule)
    if supplier_id is not None:
        query = query.where(DiscountRule.supplier_id == supplier_id)
    rows = list(await session.scalars(query))
    return {"total": len(rows), "items": [_rule_dict(row) for row in rows]}


@router.post("/discounts")
async def create_discount(
    payload: dict = Body(...),
    session: AsyncSession = Depends(get_session),
    project: Project = Depends(get_current_project),
    _admin=Depends(get_import_admin),
) -> dict[str, object]:
    scope = str(payload.get("scope", "")).strip()
    if scope not in {item.value for item in DiscountScope}:
        raise HTTPException(status_code=400, detail="недопустимый уровень скидки")
    supplier_id = str(payload.get("supplier_id", "")).strip()
    if not supplier_id:
        raise HTTPException(status_code=400, detail="нужен supplier_id")
    supplier = await session.get(Supplier, uuid.UUID(supplier_id))
    if supplier is None:
        raise HTTPException(status_code=404, detail="поставщик не найден")

    percent = _decimal(payload.get("percent", 0), "percent")
    if percent < 0 or percent > 100:
        raise HTTPException(status_code=400, detail="процент скидки вне диапазона 0–100")
    if scope == DiscountScope.category.value and not payload.get("category_id"):
        raise HTTPException(status_code=400, detail="для скидки по категории нужен category_id")
    if scope == DiscountScope.article.value and not payload.get("normalized_article"):
        raise HTTPException(status_code=400, detail="для скидки по артикулу нужен артикул")

    rule = DiscountRule(
        project_id=project.id,
        supplier_id=supplier.id,
        scope=scope,
        category_id=uuid.UUID(str(payload["category_id"])) if payload.get("category_id") else None,
        normalized_article=(str(payload.get("normalized_article", "")).strip() or None),
        min_amount=_decimal(payload["min_amount"], "min_amount")
        if payload.get("min_amount") is not None
        else None,
        percent=percent,
        is_active=bool(payload.get("is_active", True)),
        note=str(payload.get("note", "")),
    )
    session.add(rule)
    await session.commit()
    return _rule_dict(rule)


@router.delete("/discounts/{rule_id}")
async def delete_discount(
    rule_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _admin=Depends(get_import_admin),
) -> dict[str, str]:
    rule = await session.get(DiscountRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="правило не найдено")
    await session.delete(rule)
    await session.commit()
    return {"deleted": str(rule_id)}


@router.post("/discounts/preview")
async def preview_discount(
    payload: dict = Body(...),
    session: AsyncSession = Depends(get_session),
    _reader=Depends(get_import_reader),
) -> dict[str, object]:
    """Какая скидка сработает на позицию — без создания расчёта.

    Нужно менеджеру, чтобы понять, почему цена именно такая, до того как он
    соберёт КП из двух сотен строк.
    """
    from app.models import SupplierPriceItem, PriceVersion

    item_id = str(payload.get("price_item_id", "")).strip()
    if not item_id:
        raise HTTPException(status_code=400, detail="нужен price_item_id")
    item = await session.get(SupplierPriceItem, uuid.UUID(item_id))
    if item is None:
        raise HTTPException(status_code=404, detail="позиция прайса не найдена")
    version = await session.get(PriceVersion, item.price_version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="версия прайса не найдена")

    service = DiscountService(session)
    rules = await service.rules_for([version.supplier_id])
    key = await service.position_key(item, version.supplier_id)
    match = service.match(key, rules)
    return {
        "price_item_id": str(item.id),
        "supplier_id": str(version.supplier_id),
        "category_id": str(key.category_id) if key.category_id else None,
        "percent": float(match.percent),
        "source": match.source,
        "rule_id": str(match.rule_id) if match.rule_id else None,
    }


@router.post("")
async def create_calculation(
    payload: dict = Body(...),
    session: AsyncSession = Depends(get_session),
    project: Project = Depends(get_current_project),
    _admin=Depends(get_import_admin),
) -> dict[str, object]:
    """Собрать расчёт из выбранных менеджером позиций прайсов."""
    raw_lines = payload.get("lines") or []
    if not isinstance(raw_lines, list) or not raw_lines:
        raise HTTPException(status_code=400, detail="нужен непустой список lines")
    lines: list[LineInput] = []
    for entry in raw_lines:
        price_item_id = str(entry.get("price_item_id", "")).strip()
        if not price_item_id:
            raise HTTPException(status_code=400, detail="в строке нужен price_item_id")
        canonical_id = str(entry.get("canonical_id", "")).strip()
        lines.append(
            LineInput(
                price_item_id=uuid.UUID(price_item_id),
                quantity=_decimal(entry.get("quantity", 1), "quantity"),
                source_line=str(entry.get("source_line", "")),
                canonical_id=uuid.UUID(canonical_id) if canonical_id else None,
            )
        )

    request_id = str(payload.get("request_id", "")).strip()
    check_batch_id = str(payload.get("check_batch_id", "")).strip()
    service = CalculationService(session)
    calculation = await service.create(
        project_id=project.id,
        lines=lines,
        title=str(payload.get("title", "")),
        request_id=uuid.UUID(request_id) if request_id else None,
        check_batch_id=uuid.UUID(check_batch_id) if check_batch_id else None,
    )
    totals = await service.recalculate(calculation)
    await session.commit()
    return _calculation_dict(calculation, totals)


@router.post("/physical-reference/import")
async def upload_physical_reference(
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    project: Project = Depends(get_current_project),
    _admin=Depends(get_import_admin),
) -> dict[str, int]:
    filename = file.filename or "reference.csv"
    if not filename.casefold().endswith((".csv", ".xlsx")):
        raise HTTPException(status_code=400, detail="поддерживаются CSV и XLSX")
    try:
        result = await import_reference(
            session, project_id=project.id, payload=await file.read(), filename=filename
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    await session.commit()
    return result


@router.get("/physical-reference/stats")
async def physical_reference_stats(
    session: AsyncSession = Depends(get_session),
    project: Project = Depends(get_current_project),
    _reader=Depends(get_import_reader),
) -> dict[str, int]:
    counts = (
        await session.execute(
            select(
                func.count(ProductPhysicalReference.id),
                func.sum(case((ProductPhysicalReference.weight_kg.is_not(None), 1), else_=0)),
                func.sum(case((ProductPhysicalReference.volume_m3.is_not(None), 1), else_=0)),
            ).where(ProductPhysicalReference.project_id == project.id)
        )
    ).one()
    return {
        "rows": int(counts[0] or 0),
        "with_weight": int(counts[1] or 0),
        "with_volume": int(counts[2] or 0),
    }


@router.get("/{calculation_id}/export.xlsx")
async def export_calculation(
    calculation_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    project: Project = Depends(get_current_project),
    _reader=Depends(get_import_reader),
) -> Response:
    calculation = await _project_calculation(session, calculation_id, project.id)
    return Response(
        content=build_quote_workbook(calculation),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="quote-{calculation.id}.xlsx"'},
    )


@router.get("/{calculation_id}")
async def get_calculation(
    calculation_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    project: Project = Depends(get_current_project),
    _reader=Depends(get_import_reader),
) -> dict[str, object]:
    calculation = await _project_calculation(session, calculation_id, project.id)
    return _calculation_dict(calculation)


@router.post("/{calculation_id}/recalculate")
async def recalculate(
    calculation_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    project: Project = Depends(get_current_project),
    _admin=Depends(get_import_admin),
) -> dict[str, object]:
    calculation = await _project_calculation(session, calculation_id, project.id)
    totals = await CalculationService(session).recalculate(calculation)
    await session.commit()
    return _calculation_dict(calculation, totals)


@router.post("/items/{item_id}/price")
async def override_price(
    item_id: uuid.UUID,
    payload: dict = Body(...),
    session: AsyncSession = Depends(get_session),
    project: Project = Depends(get_current_project),
    admin=Depends(get_import_admin),
) -> dict[str, object]:
    """Ручная корректировка цены. Без причины правка не принимается."""
    reason = str(payload.get("reason", "")).strip()
    if not reason:
        raise HTTPException(status_code=400, detail="нужна причина корректировки")
    raw_price = payload.get("price")
    price = _decimal(raw_price, "price") if raw_price is not None else None
    if price is not None and price < 0:
        raise HTTPException(status_code=400, detail="цена не может быть отрицательной")
    changed_by = str(payload.get("changed_by", "")).strip() or str(
        getattr(admin, "username", "") or getattr(admin, "name", "") or "оператор"
    )
    owned = await session.scalar(
        select(CalculationItem.id)
        .join(Calculation, Calculation.id == CalculationItem.calculation_id)
        .where(CalculationItem.id == item_id, Calculation.project_id == project.id)
    )
    if owned is None:
        raise HTTPException(status_code=404, detail="строка расчёта не найдена")
    row = await CalculationService(session).override_price(
        item_id, price=price, reason=reason, changed_by=changed_by
    )
    if row is None:
        raise HTTPException(status_code=404, detail="строка расчёта не найдена")
    await session.commit()
    return _item_dict(row)


@router.get("/items/{item_id}/price-history")
async def price_history(
    item_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    project: Project = Depends(get_current_project),
    _reader=Depends(get_import_reader),
) -> dict[str, object]:
    owned = await session.scalar(
        select(CalculationItem.id)
        .join(Calculation, Calculation.id == CalculationItem.calculation_id)
        .where(CalculationItem.id == item_id, Calculation.project_id == project.id)
    )
    if owned is None:
        raise HTTPException(status_code=404, detail="строка расчёта не найдена")
    rows = await CalculationService(session).history(item_id)
    return {
        "total": len(rows),
        "items": [
            {
                "previous_price": float(row.previous_price)
                if row.previous_price is not None
                else None,
                "new_price": float(row.new_price) if row.new_price is not None else None,
                "reason": row.reason,
                "changed_by": row.changed_by,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
    }
