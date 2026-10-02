"""Parse-rule endpoints: CRUD, lifecycle (activate/deprecate) and dry-run
validation against a stored price version."""
from __future__ import annotations

import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.db import get_session
from app.models import (
    ParseRule,
    ParseRuleProvenance,
    ParseRuleStatus,
    PriceFile,
    PriceImport,
    PriceVersion,
    Project,
    Supplier,
    SupplierPriceItem,
)
from app.project_context import get_current_project
from app.parsing import loaders
from app.parsing.pdf_layout import (
    is_pdf_layout_recipe,
    parse_pdf_layout,
    validate_pdf_layout_recipe,
)
from app.parsing.mapping import ATTRIBUTE_VALUE_KINDS, build_auto_mapping
from app.parsing.inline_attributes import INLINE_EXTRACTOR_KINDS
from app.parsing.pipeline import ParseResult, parse_grids
from app.parsing.rules import compute_header_hash, recipe_from_decision, recipe_to_decision, validation_metrics
from app.parsing.workbook_layout import (
    is_workbook_layout_recipe,
    parse_workbook_layout,
    validate_workbook_layout_recipe,
)
from app.parsing.validation_preview import build_validation_samples, validation_sample
from app.parsing.validation_export import build_validation_export, build_validation_xlsx
from app.parsing.matrix_unfold import (
    build_matrix_section_contexts,
    parse_matrix_regions,
    rebind_matrix_regions,
    resolve_matrix_regions,
)
from app.parsing.grid import SheetGrid
from app.schemas import (
    ImportedPriceVersionOut,
    ParseRuleCreate,
    ParseRuleOut,
    ParseRulePage,
    ParseRuleStatusUpdate,
    ParseRuleSummaryOut,
    ParseRuleUsageOut,
    ParseRuleUpdate,
    RuleAutoDraftOut,
    RuleMatrixPreviewIn,
    RuleMatrixPreviewOut,
    RuleMatrixRebindIn,
    RuleValidateIn,
    RuleValidationOut,
)

router = APIRouter(tags=["parse-rules"])

KNOWN_LOADERS = {
    loaders.LOADER_XLSX,
    loaders.LOADER_XLS,
    loaders.LOADER_PDF_TEXT,
    loaders.LOADER_DOCX,
    loaders.LOADER_DOC,
    loaders.LOADER_TXT,
}


def _excel_column(index: int) -> str:
    value = index + 1
    label = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        label = chr(65 + remainder) + label
    return label


def build_matrix_preview(
    grids: list[SheetGrid], recipe: dict, *, source_section_id: str
) -> dict:
    """Return the exact source rectangle and every item from one matrix region."""

    regions = [
        region
        for region in recipe.get("matrix_regions", [])
        if isinstance(region, dict) and region.get("source_section_id") == source_section_id
    ]
    if len(regions) != 1:
        raise ValueError("Матричный фрагмент правила не найден")
    region = resolve_matrix_regions(grids, [regions[0]])[0]
    match = region.get("match") or {}
    sheet_index = match.get("or_index")
    if not isinstance(sheet_index, int) or not 0 <= sheet_index < len(grids):
        raise ValueError("Лист матричного фрагмента не найден")
    grid = grids[sheet_index]
    start = region.get("region_start_row_index", 0)
    end = region.get("region_end_row_index", len(grid.rows))
    if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end:
        raise ValueError("Некорректные границы матричного фрагмента")
    end = min(end, len(grid.rows))
    parsed = parse_matrix_regions(grids, [region])
    rows = []
    for row_index in range(start, end):
        source_row = grid.rows[row_index]
        rows.append({
            "row_index": row_index,
            "excel_row": row_index + 1,
            "cells": [
                {
                    "column_index": column_index,
                    "excel_column": _excel_column(column_index),
                    "value": None if (
                        cell.display_value is None and cell.value is None
                    ) else str(
                        cell.display_value if cell.display_value is not None else cell.value
                    ),
                    "is_merged_fill": cell.is_merged_fill,
                }
                for column_index, cell in enumerate(source_row)
            ],
        })
    return {
        "source_section_id": source_section_id,
        "sheet_index": sheet_index,
        "sheet_name": grid.name,
        "row_offset": start,
        "total_rows": end - start,
        "total_columns": grid.n_cols,
        "rows": rows,
        "total_items": len(parsed.items),
        "items": [validation_sample(item) for item in parsed.items],
    }


def rebind_matrix_recipe(
    grids: list[SheetGrid], recipe: dict
) -> tuple[dict, ParseResult]:
    """Replace only matrix bindings and validate the combined workbook rule."""

    regions = recipe.get("matrix_regions")
    if not isinstance(regions, list) or not regions:
        raise ValueError("Правило не содержит матричных секций")
    rebound = deepcopy(recipe)
    rebound["matrix_regions"] = rebind_matrix_regions(grids, regions)
    return rebound, parse_workbook_layout(grids, rebound)


def _validate_recipe_format(recipe: dict) -> None:
    """Reject unknown executable formats before a draft reaches the database."""
    format_name = recipe.get("format") if isinstance(recipe, dict) else None
    if format_name is None:
        _validate_tabular_attribute_kinds(recipe)
        return
    if is_workbook_layout_recipe(recipe):
        try:
            validate_workbook_layout_recipe(recipe)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return
    if not is_pdf_layout_recipe(recipe):
        raise HTTPException(status_code=422, detail=f"Неизвестный формат правила: {format_name}")
    try:
        validate_pdf_layout_recipe(recipe)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _validate_tabular_attribute_kinds(recipe: dict) -> None:
    """Reject non-scalar kinds before a tabular parse rule is stored."""
    if not isinstance(recipe, dict):
        raise HTTPException(status_code=422, detail="Правило должно быть объектом")
    for sheet in recipe.get("sheets", []):
        if not isinstance(sheet, dict):
            continue
        for column in sheet.get("columns", []):
            if not isinstance(column, dict):
                continue
            value_kind = column.get("attribute_value_kind")
            if value_kind is not None and value_kind not in ATTRIBUTE_VALUE_KINDS:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Недопустимый тип характеристики: "
                        f"{value_kind}. Разрешены только скалярные типы: "
                        + ", ".join(sorted(ATTRIBUTE_VALUE_KINDS))
                    ),
                )
        for attribute in sheet.get("inline_attributes", []):
            if not isinstance(attribute, dict):
                raise HTTPException(status_code=422, detail="Некорректный inline extractor")
            allowed_fields = {
                "source_column_index",
                "attribute_code",
                "display_name",
                "value_kind",
                "canonical_unit",
                "extractor_kind",
            }
            if set(attribute) - allowed_fields:
                raise HTTPException(status_code=422, detail="Некорректный inline extractor: лишние поля")
            if attribute.get("extractor_kind") not in INLINE_EXTRACTOR_KINDS:
                raise HTTPException(status_code=422, detail="Неизвестный inline extractor")
            if attribute.get("value_kind") not in ATTRIBUTE_VALUE_KINDS:
                raise HTTPException(status_code=422, detail="Некорректный тип inline extractor")
            source_index = attribute.get("source_column_index")
            if not isinstance(source_index, int) or isinstance(source_index, bool) or source_index < 0:
                raise HTTPException(status_code=422, detail="Некорректная колонка inline extractor")


async def _get_rule(
    session: AsyncSession, rule_id: uuid.UUID, *, project_id: uuid.UUID
) -> ParseRule:
    rule = (
        await session.execute(
            select(ParseRule).where(ParseRule.id == rule_id, ParseRule.project_id == project_id)
        )
    ).scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="Правило разбора не найдено")
    return rule


async def _get_rule_price_file(
    *,
    rule: ParseRule,
    price_version_id: uuid.UUID,
    project: Project,
    session: AsyncSession,
) -> tuple[PriceVersion, PriceFile]:
    """Resolve a project-local source file and enforce supplier ownership."""

    version = await session.get(PriceVersion, price_version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Версия прайса не найдена")
    price_file = await session.get(PriceFile, version.price_file_id)
    if price_file is None or price_file.project_id != project.id:
        raise HTTPException(status_code=404, detail="Версия прайса не найдена")
    if version.supplier_id != price_file.supplier_id:
        raise HTTPException(
            status_code=409,
            detail="Версия прайса и исходный файл относятся к разным поставщикам",
        )
    if rule.supplier_id is not None and version.supplier_id != rule.supplier_id:
        raise HTTPException(
            status_code=409,
            detail="Версия прайса относится к другому поставщику",
        )
    return version, price_file


async def _parse_rule_version(
    *,
    rule: ParseRule,
    price_version_id: uuid.UUID,
    project: Project,
    session: AsyncSession,
):
    """Execute a saved rule without persistence for validation and exports."""

    version, price_file = await _get_rule_price_file(
        rule=rule,
        price_version_id=price_version_id,
        project=project,
        session=session,
    )
    data = storage.get_price_file(price_file.storage_key)
    try:
        loader = loaders.detect_loader(data, price_file.original_filename)
    except loaders.UnknownFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if loader != rule.loader:
        raise HTTPException(
            status_code=409,
            detail=f"Загрузчик файла ({loader}) не совпадает с загрузчиком правила ({rule.loader})",
        )
    if is_pdf_layout_recipe(rule.recipe):
        if loader != loaders.LOADER_PDF_TEXT:
            raise HTTPException(
                status_code=422,
                detail="pdf_layout_v1 применим только к PDF с текстовым слоем",
            )
        try:
            result = parse_pdf_layout(data, rule.recipe)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return version, price_file, result
    try:
        grids = loaders.load_grids_for(loader, data, price_file.original_filename)
    except loaders.PdfNoTablesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if is_workbook_layout_recipe(rule.recipe):
        if loader not in {loaders.LOADER_XLS, loaders.LOADER_XLSX}:
            raise HTTPException(
                status_code=422,
                detail="workbook_layout_v1 применим только к XLS/XLSX",
            )
        try:
            result = parse_workbook_layout(grids, rule.recipe)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    else:
        decision = recipe_to_decision(rule.recipe, grids)
        if not decision.auto_ok:
            raise HTTPException(
                status_code=422,
                detail="Правило не применилось к файлу (не найдено листов name+price)",
            )
        result = parse_grids(grids, decision)
    return version, price_file, result


async def _usage_by_rule(
    session: AsyncSession,
    rule_ids: list[uuid.UUID],
    *,
    project_id: uuid.UUID,
) -> dict[uuid.UUID, ParseRuleUsageOut]:
    usage = {rule_id: ParseRuleUsageOut() for rule_id in rule_ids}
    if not rule_ids:
        return usage

    version_counts = dict(
        (
            await session.execute(
                select(PriceVersion.parse_rule_id, func.count(PriceVersion.id))
                .join(PriceFile, PriceFile.id == PriceVersion.price_file_id)
                .where(PriceVersion.parse_rule_id.in_(rule_ids))
                .where(PriceFile.project_id == project_id)
                .group_by(PriceVersion.parse_rule_id)
            )
        ).all()
    )
    item_counts = dict(
        (
            await session.execute(
                select(PriceVersion.parse_rule_id, func.count(SupplierPriceItem.id))
                .select_from(SupplierPriceItem)
                .join(PriceVersion, PriceVersion.id == SupplierPriceItem.price_version_id)
                .join(PriceFile, PriceFile.id == PriceVersion.price_file_id)
                .where(PriceVersion.parse_rule_id.in_(rule_ids))
                .where(PriceFile.project_id == project_id)
                .group_by(PriceVersion.parse_rule_id)
            )
        ).all()
    )
    unique_source_rows = (
        select(
            PriceVersion.parse_rule_id.label("rule_id"),
            SupplierPriceItem.price_version_id,
            SupplierPriceItem.source_sheet,
            SupplierPriceItem.source_row,
        )
        .select_from(SupplierPriceItem)
        .join(PriceVersion, PriceVersion.id == SupplierPriceItem.price_version_id)
        .join(PriceFile, PriceFile.id == PriceVersion.price_file_id)
        .where(PriceVersion.parse_rule_id.in_(rule_ids))
        .where(PriceFile.project_id == project_id)
        .group_by(
            PriceVersion.parse_rule_id,
            SupplierPriceItem.price_version_id,
            SupplierPriceItem.source_sheet,
            SupplierPriceItem.source_row,
        )
        .subquery()
    )
    source_row_counts = dict(
        (
            await session.execute(
                select(unique_source_rows.c.rule_id, func.count())
                .group_by(unique_source_rows.c.rule_id)
            )
        ).all()
    )
    for rule_id in rule_ids:
        usage[rule_id] = ParseRuleUsageOut(
            price_versions=int(version_counts.get(rule_id, 0)),
            database_items=int(item_counts.get(rule_id, 0)),
            source_rows=int(source_row_counts.get(rule_id, 0)),
        )
    return usage


async def _source_version_for_rule(
    session: AsyncSession,
    rule: ParseRule,
    *,
    project_id: uuid.UUID,
) -> ImportedPriceVersionOut | None:
    if rule.created_from_version_id is None:
        return None
    row = (
        await session.execute(
            select(PriceVersion, Supplier, PriceFile, PriceImport.id)
            .select_from(PriceVersion)
            .join(Supplier, Supplier.id == PriceVersion.supplier_id)
            .join(PriceFile, PriceFile.id == PriceVersion.price_file_id)
            .outerjoin(PriceImport, PriceImport.price_version_id == PriceVersion.id)
            .where(
                PriceVersion.id == rule.created_from_version_id,
                PriceFile.project_id == project_id,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    version, supplier, price_file, import_id = row
    return ImportedPriceVersionOut(
        id=version.id,
        supplier_id=supplier.id,
        supplier_external_id=supplier.external_id,
        supplier_name=supplier.name,
        version_no=version.version_no,
        status=version.status,
        loader=version.loader,
        filename=price_file.original_filename,
        item_count=version.item_count,
        warnings_count=version.warnings_count,
        errors_count=version.errors_count,
        created_at=version.created_at,
        import_id=import_id,
    )


# Единственный способ прочитать список правил. Прежний беспагинационный
# `GET /parse-rules` отдавал подмножество этих же данных без счётчиков
# использования и без поиска, а `/parse-rules/page` приходилось объявлять выше
# `/parse-rules/{rule_id}`, иначе слово `page` матчилось как идентификатор.
@router.get("/parse-rules", response_model=ParseRulePage)
async def list_rules(
    query: str | None = Query(default=None, max_length=512),
    supplier_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    loader: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> ParseRulePage:
    filters = [ParseRule.project_id == project.id]
    if supplier_id is not None:
        filters.append(ParseRule.supplier_id == supplier_id)
    if status is not None:
        filters.append(ParseRule.status == status)
    if loader is not None:
        filters.append(ParseRule.loader == loader)
    if query and query.strip():
        pattern = f"%{query.strip()}%"
        filters.append(or_(ParseRule.name.ilike(pattern), Supplier.name.ilike(pattern)))
    total = (
        await session.execute(
            select(func.count(ParseRule.id)).outerjoin(Supplier).where(*filters)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            select(ParseRule, Supplier.name, PriceImport.id)
            .outerjoin(Supplier, ParseRule.supplier_id == Supplier.id)
            .outerjoin(
                PriceImport,
                PriceImport.price_version_id == ParseRule.created_from_version_id,
            )
            .where(*filters)
            .order_by(ParseRule.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    usage = await _usage_by_rule(
        session, [rule.id for rule, _, _ in rows], project_id=project.id
    )
    return ParseRulePage(
        total=total,
        items=[
            ParseRuleSummaryOut(
                **ParseRuleOut.model_validate(rule).model_dump(),
                supplier_name=supplier_name,
                source_import_id=source_import_id,
                usage=usage[rule.id],
            )
            for rule, supplier_name, source_import_id in rows
        ],
    )


@router.get("/parse-rules/{rule_id}", response_model=ParseRuleSummaryOut)
async def get_rule(
    rule_id: uuid.UUID,
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> ParseRuleSummaryOut:
    rule = await _get_rule(session, rule_id, project_id=project.id)
    supplier_name = None
    if rule.supplier_id is not None:
        supplier_name = await session.scalar(select(Supplier.name).where(Supplier.id == rule.supplier_id))
    source_version = await _source_version_for_rule(
        session, rule, project_id=project.id
    )
    source_import_id = source_version.import_id if source_version is not None else None
    usage = await _usage_by_rule(session, [rule.id], project_id=project.id)
    return ParseRuleSummaryOut(
        **ParseRuleOut.model_validate(rule).model_dump(),
        supplier_name=supplier_name,
        source_import_id=source_import_id,
        source_version=source_version,
        usage=usage[rule.id],
    )


@router.post("/parse-rules", response_model=ParseRuleOut, status_code=201)
async def create_rule(
    payload: ParseRuleCreate,
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> ParseRuleOut:
    if payload.loader not in KNOWN_LOADERS:
        raise HTTPException(status_code=422, detail=f"Неизвестный загрузчик: {payload.loader}")
    _validate_recipe_format(payload.recipe)
    fingerprint = payload.fingerprint or {"loader": payload.loader, "sheet_signatures": []}
    fingerprint.setdefault("loader", payload.loader)
    header_hash = payload.header_hash or compute_header_hash(fingerprint)
    rule = ParseRule(
        project_id=project.id,
        supplier_id=payload.supplier_id,
        name=payload.name,
        loader=payload.loader,
        header_hash=header_hash,
        fingerprint=fingerprint,
        recipe=payload.recipe,
        revision=1,
        status=ParseRuleStatus.draft.value,
        provenance=ParseRuleProvenance.manual.value,
    )
    session.add(rule)
    await session.commit()
    return ParseRuleOut.model_validate(rule)


@router.put("/parse-rules/{rule_id}", response_model=ParseRuleOut)
async def update_rule(
    rule_id: uuid.UUID,
    payload: ParseRuleUpdate,
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> ParseRuleOut:
    rule = await _get_rule(session, rule_id, project_id=project.id)
    changed = False
    parsing_semantics_changed = False
    if payload.name is not None and payload.name != rule.name:
        rule.name = payload.name
        changed = True
    if payload.supplier_id is not None and payload.supplier_id != rule.supplier_id:
        rule.supplier_id = payload.supplier_id
        changed = True
        parsing_semantics_changed = True
    if payload.recipe is not None and payload.recipe != rule.recipe:
        _validate_recipe_format(payload.recipe)
        rule.recipe = payload.recipe
        changed = True
        parsing_semantics_changed = True
    if changed:
        # An edited rule is a new revision whose quality is unproven again.
        rule.revision += 1
        if parsing_semantics_changed:
            rule.validation = None
        rule.updated_at = datetime.now(timezone.utc)
        await session.commit()
    return ParseRuleOut.model_validate(rule)


# Переход правила по жизненному циклу — одна ручка вместо пары
# `activate`/`deprecate`: у сущности есть статус, а не два разных действия.
@router.put("/parse-rules/{rule_id}/status", response_model=ParseRuleOut)
async def set_rule_status(
    rule_id: uuid.UUID,
    payload: ParseRuleStatusUpdate,
    force: bool = Query(default=False),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> ParseRuleOut:
    if payload.status == ParseRuleStatus.deprecated.value:
        rule = await _get_rule(session, rule_id, project_id=project.id)
        rule.status = ParseRuleStatus.deprecated.value
        rule.updated_at = datetime.now(timezone.utc)
        await session.commit()
        return ParseRuleOut.model_validate(rule)

    rule = await _get_rule(session, rule_id, project_id=project.id)
    if rule.status == ParseRuleStatus.active.value:
        return ParseRuleOut.model_validate(rule)
    passed = bool(rule.validation and rule.validation.get("passed"))
    if not passed and not force:
        raise HTTPException(
            status_code=409,
            detail="Активация возможна только после успешной валидации (или с ?force=true)",
        )
    if not force and rule.validation:
        metrics = rule.validation.get("metrics")
        version_id = rule.validation.get("price_version_id")
        parsed_items = metrics.get("items_with_price") if isinstance(metrics, dict) else None
        try:
            validation_version_id = uuid.UUID(str(version_id))
        except (TypeError, ValueError, AttributeError):
            raise HTTPException(
                status_code=409,
                detail="Сохранённая валидация не содержит доступной версии прайса; повторите dry-run",
            ) from None
        try:
            version, _price_file = await _get_rule_price_file(
                rule=rule,
                price_version_id=validation_version_id,
                project=project,
                session=session,
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                raise HTTPException(
                    status_code=409,
                    detail="Версия прайса из сохранённой валидации недоступна; повторите dry-run",
                ) from exc
            raise
        if (
            version.item_count > 0
            and isinstance(parsed_items, int)
            and not isinstance(parsed_items, bool)
            and parsed_items < version.item_count
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Dry-run разобрал {parsed_items} из {version.item_count} позиций "
                    f"исходной версии; потеряно {version.item_count - parsed_items}. "
                    "Сохраните полное правило и повторите dry-run"
                ),
            )
    supplier_filter = (
        ParseRule.supplier_id == rule.supplier_id
        if rule.supplier_id is not None
        else ParseRule.supplier_id.is_(None)
    )
    active_peers = (
        (
            await session.execute(
                select(ParseRule).where(
                    ParseRule.id != rule.id,
                    ParseRule.project_id == rule.project_id,
                    ParseRule.header_hash == rule.header_hash,
                    ParseRule.loader == rule.loader,
                    supplier_filter,
                    ParseRule.status == ParseRuleStatus.active.value,
                )
            )
        )
        .scalars()
        .all()
    )
    for peer in active_peers:
        peer.status = ParseRuleStatus.deprecated.value
        peer.updated_at = datetime.now(timezone.utc)
    rule.status = ParseRuleStatus.active.value
    rule.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return ParseRuleOut.model_validate(rule)


@router.post("/parse-rules/{rule_id}/clone", response_model=ParseRuleOut, status_code=201)
async def clone_rule_to_draft(
    rule_id: uuid.UUID,
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> ParseRuleOut:
    """Create a new draft rule copying recipe/settings from an existing rule."""
    source = await _get_rule(session, rule_id, project_id=project.id)
    supplier_filter = (
        ParseRule.supplier_id == source.supplier_id
        if source.supplier_id is not None
        else ParseRule.supplier_id.is_(None)
    )
    max_rev = await session.scalar(
        select(func.coalesce(func.max(ParseRule.revision), 0)).where(
            ParseRule.project_id == project.id,
            ParseRule.header_hash == source.header_hash,
            supplier_filter,
        )
    )
    clone = ParseRule(
        project_id=project.id,
        supplier_id=source.supplier_id,
        name=f"{source.name} (черновик)",
        loader=source.loader,
        header_hash=source.header_hash,
        fingerprint=source.fingerprint,
        recipe=source.recipe,
        revision=(max_rev or 0) + 1,
        status=ParseRuleStatus.draft.value,
        provenance=ParseRuleProvenance.manual.value,
        created_from_version_id=source.created_from_version_id,
    )
    session.add(clone)
    await session.commit()
    return ParseRuleOut.model_validate(clone)


@router.post("/parse-rules/{rule_id}/validate", response_model=RuleValidationOut)
async def validate_rule(
    rule_id: uuid.UUID,
    payload: RuleValidateIn,
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> RuleValidationOut:
    """Dry-run: parse the version's stored file by this rule's recipe, count
    metrics, store the result on the rule. Nothing is written to items."""
    rule = await _get_rule(session, rule_id, project_id=project.id)
    version, _price_file, result = await _parse_rule_version(
        rule=rule,
        price_version_id=payload.price_version_id,
        project=project,
        session=session,
    )
    validation = validation_metrics(result)
    metrics = dict(validation["metrics"])
    passed = bool(validation["passed"])
    samples = build_validation_samples(result)
    if version.item_count > 0:
        parsed_items = int(metrics["items_with_price"])
        metrics["source_items"] = version.item_count
        metrics["items_delta"] = parsed_items - version.item_count
        passed = passed and parsed_items >= version.item_count

    at = datetime.now(timezone.utc)
    rule.validation = {
        "at": at.isoformat(),
        "price_version_id": str(version.id),
        "metrics": metrics,
        "passed": passed,
        "samples": samples,
    }
    rule.updated_at = at
    await session.commit()
    return RuleValidationOut(
        at=at,
        price_version_id=version.id,
        metrics=metrics,
        passed=passed,
        samples=samples,
    )


@router.post("/parse-rules/{rule_id}/validation-export")
async def export_rule_validation(
    rule_id: uuid.UUID,
    payload: RuleValidateIn,
    format: Literal["json", "xlsx"] = Query(default="json"),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Download every dry-run position; no items are persisted."""

    rule = await _get_rule(session, rule_id, project_id=project.id)
    _version, price_file, result = await _parse_rule_version(
        rule=rule,
        price_version_id=payload.price_version_id,
        project=project,
        session=session,
    )
    stem = price_file.original_filename.rsplit(".", 1)[0]
    download_name = f"{stem} — результат разбора.{format}"
    disposition = f"attachment; filename*=UTF-8''{quote(download_name)}"
    if format == "xlsx":
        content = build_validation_xlsx(
            result.items, filename=price_file.original_filename
        )
        media_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        payload_json = build_validation_export(
            result.items, filename=price_file.original_filename
        )
        content = json.dumps(
            payload_json, ensure_ascii=False, indent=2
        ).encode("utf-8")
        media_type = "application/json; charset=utf-8"
    return StreamingResponse(
        iter([content]),
        media_type=media_type,
        headers={"Content-Disposition": disposition},
    )


@router.post(
    "/parse-rules/{rule_id}/matrix-preview",
    response_model=RuleMatrixPreviewOut,
)
async def preview_rule_matrix(
    rule_id: uuid.UUID,
    payload: RuleMatrixPreviewIn,
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> RuleMatrixPreviewOut:
    """Preview one matrix region from the unsaved editor recipe without persistence."""

    rule = await _get_rule(session, rule_id, project_id=project.id)
    _validate_recipe_format(payload.recipe)
    if not is_workbook_layout_recipe(payload.recipe):
        raise HTTPException(status_code=422, detail="Предпросмотр доступен только для XLS/XLSX-правила")
    _version, price_file = await _get_rule_price_file(
        rule=rule,
        price_version_id=payload.price_version_id,
        project=project,
        session=session,
    )
    data = storage.get_price_file(price_file.storage_key)
    try:
        loader = loaders.detect_loader(data, price_file.original_filename)
        grids = loaders.load_grids_for(loader, data, price_file.original_filename)
    except (loaders.UnknownFormatError, loaders.PdfNoTablesError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if loader != rule.loader or loader not in {loaders.LOADER_XLS, loaders.LOADER_XLSX}:
        raise HTTPException(status_code=409, detail="Файл и правило должны быть одного XLS/XLSX-формата")
    try:
        preview = build_matrix_preview(
            grids,
            payload.recipe,
            source_section_id=payload.source_section_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RuleMatrixPreviewOut.model_validate(preview)


@router.post(
    "/parse-rules/{rule_id}/matrix-rebind",
    response_model=RuleAutoDraftOut,
)
async def rebind_rule_matrix(
    rule_id: uuid.UUID,
    payload: RuleMatrixRebindIn,
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> RuleAutoDraftOut:
    """Rebuild matrix bindings only; ordinary rule sections stay untouched."""

    rule = await _get_rule(session, rule_id, project_id=project.id)
    _validate_recipe_format(payload.recipe)
    _version, price_file = await _get_rule_price_file(
        rule=rule,
        price_version_id=payload.price_version_id,
        project=project,
        session=session,
    )
    data = storage.get_price_file(price_file.storage_key)
    try:
        loader = loaders.detect_loader(data, price_file.original_filename)
        grids = loaders.load_grids_for(loader, data, price_file.original_filename)
    except (loaders.UnknownFormatError, loaders.PdfNoTablesError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if loader != rule.loader or loader not in {loaders.LOADER_XLS, loaders.LOADER_XLSX}:
        raise HTTPException(
            status_code=409,
            detail="Файл и правило должны быть одного XLS/XLSX-формата",
        )
    try:
        recipe, result = rebind_matrix_recipe(grids, payload.recipe)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    validation = validation_metrics(result)
    return RuleAutoDraftOut(
        recipe=recipe,
        metrics=validation["metrics"],
        passed=validation["passed"],
        sheet_count=len(grids),
    )


@router.post("/parse-rules/{rule_id}/auto-draft", response_model=RuleAutoDraftOut)
async def auto_draft_recipe(
    rule_id: uuid.UUID,
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> RuleAutoDraftOut:
    """Reconstruct a draft recipe by auto-detecting the structure of the rule's
    source price file. Nothing is persisted; the caller reviews the result and
    saves it as a draft. Useful when a stored recipe is empty or broken."""
    rule = await _get_rule(session, rule_id, project_id=project.id)
    if rule.created_from_version_id is None:
        raise HTTPException(
            status_code=409, detail="У правила нет исходной версии прайса для автоподбора"
        )
    version = await session.get(PriceVersion, rule.created_from_version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Исходная версия прайса не найдена")
    price_file = await session.get(PriceFile, version.price_file_id)
    if price_file is None or price_file.project_id != project.id:
        raise HTTPException(status_code=404, detail="Исходная версия прайса не найдена")

    data = storage.get_price_file(price_file.storage_key)
    try:
        loader = loaders.detect_loader(data, price_file.original_filename)
        grids = loaders.load_grids_for(loader, data, price_file.original_filename)
    except loaders.UnknownFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except loaders.PdfNoTablesError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    recipe = None  # встроенных профилей нет: правила живут в parse_rules
    if recipe is not None:
        result = parse_workbook_layout(grids, recipe)
        sheet_count = len(grids)
    else:
        if build_matrix_section_contexts(grids):
            raise HTTPException(
                status_code=409,
                detail=(
                    "В книге есть матричные секции. Используйте «Сгенерировать правило "
                    "из прайса»: модель определит оси, затем backend проверит весь XLSX."
                ),
            )
        decision = build_auto_mapping(grids)
        if not decision.auto_ok:
            raise HTTPException(
                status_code=422,
                detail="Не удалось автоматически определить товарные таблицы в файле",
            )
        recipe = recipe_from_decision(decision, grids)
        replay = recipe_to_decision(recipe, grids)
        result = parse_grids(grids, replay)
        sheet_count = len(recipe.get("sheets", []))
    validation = validation_metrics(result)
    return RuleAutoDraftOut(
        recipe=recipe,
        metrics=validation["metrics"],
        passed=validation["passed"],
        sheet_count=sheet_count,
    )
