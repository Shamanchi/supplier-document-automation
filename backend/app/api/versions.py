"""Price-version endpoints: dto, mapping preview/update, items, issues, activate."""
from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import storage
from app.attribute_service import attribute_scalar_value
from app.api.suppliers import version_to_dto
from app.category_service import item_category_context
from app.auth import AuthUser, access_user_from_request, require_admin
from app.db import get_session
from app.models import (
    PriceDocumentType,
    PriceFile,
    PriceParseIssue,
    PriceVersion,
    SupplierPriceItem,
    SupplierPriceItemAttributeValue,
    SupplierCategory,
    SupplierCategoryMapping,
    VersionStatus,
)
from app.parsing import loaders
from app.parsing.grid import SheetGrid
from app.parsing.headers import detect_header, detect_header_candidates, profile_price_columns
from app.parsing.mapping import ATTRIBUTE_ROLE_DEFAULTS, ROLES, valid_attribute_value_kind
from app.schemas import (
    IssueOut,
    IssuesPage,
    ItemCategoryContextOut,
    ItemOut,
    ItemAttributeOut,
    ItemPriceOut,
    ItemsPage,
    JobAccepted,
    MappingIn,
    MappingPreview,
    PreviewColumn,
    PreviewTable,
    PriceDescriptor,
    SourceEvidenceOut,
    VersionOut,
)

router = APIRouter(tags=["price-versions"])

SAMPLE_ROWS_LIMIT = 15

def _calculated_vat_prices(value) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    amount = value.amount
    rate = value.vat_rate_percent
    if amount is None or rate is None:
        return None, None, None

    coefficient = Decimal("1") + (rate / Decimal("100"))

    if value.vat_mode == "included":
        price_with_vat = amount
        price_without_vat = amount / coefficient
    elif value.vat_mode == "excluded":
        price_without_vat = amount
        price_with_vat = amount * coefficient
    else:
        return None, None, None

    vat_amount = price_with_vat - price_without_vat
    return (
        price_without_vat.quantize(Decimal("0.01")),
        price_with_vat.quantize(Decimal("0.01")),
        vat_amount.quantize(Decimal("0.01")),
    )


def _item_attribute_to_dto(value: SupplierPriceItemAttributeValue) -> ItemAttributeOut:
    definition = value.attribute_definition
    return ItemAttributeOut(
        id=value.id,
        code=definition.code,
        name=value.source_key.source_header,
        value_kind=value.value_kind,
        value=attribute_scalar_value(value),
        raw_value=value.raw_value,
        unit=definition.canonical_unit,
        parse_status=value.parse_status,
    )


def item_to_dto(item: SupplierPriceItem, *, provenance: dict | None = None) -> ItemOut:
    """Build the single versioned item contract used by internal and service APIs."""

    return ItemOut(
        id=item.id,
        source_name=item.source_name,
        normalized_name=item.normalized_name,
        article=item.source_article,
        unit=item.unit_code,
        category_path=item.category_path,
        category=item_category_context(item),
        dimensions=item.dimensions,
        description=item.description,
        attributes=[_item_attribute_to_dto(value) for value in item.attribute_values],
        source_sheet=item.source_sheet,
        source_row=item.source_row,
        source_section_id=item.source_section_id,
        source_panel_index=item.source_panel_index,
        source_column_start=item.source_column_start,
        source_column_end=item.source_column_end,
        provenance=provenance,
        source_evidence=(
            SourceEvidenceOut(
                header=item.source_header_cells,
                data=item.source_data_cells,
                categories=item.source_category_cells,
            )
            if (
                item.source_header_cells
                or item.source_data_cells
                or item.source_category_cells
            )
            else None
        ),
        prices=[
            ItemPriceOut(
                kind=value.price_kind,
                amount=value.amount,
                qualifier=value.qualifier,
                amount_from=value.amount_from,
                amount_to=value.amount_to,
                currency=value.currency,
                currency_symbol=(
                    value.currency_directory.symbol if value.currency_directory else None
                ),
                currency_source=value.currency_source,
                min_quantity=value.min_quantity,
                max_quantity=value.max_quantity,
                basis_quantity=value.basis_quantity,
                basis_unit=value.basis_unit,
                raw_value=value.raw_value,
                source_column_index=value.source_column_index,
                vat_mode=value.vat_mode,
                vat_rate_percent=value.vat_rate_percent,
                price_without_vat=(calculated_vat := _calculated_vat_prices(value))[0],
                price_with_vat=calculated_vat[1],
                vat_amount=calculated_vat[2],
                is_on_request=value.is_on_request,
                source_label=value.source_label,
            )
            for value in item.values
        ],
    )
     

def item_load_options():
    """Загрузка позиции со всем, что нужно `item_to_dto`, одним набором join'ов.

    Живёт рядом с `item_to_dto`: набор обязан меняться вместе с ним, иначе
    сериализация упрётся в ленивую подгрузку внутри async-сессии.
    """

    return (
        selectinload(SupplierPriceItem.values),
        selectinload(SupplierPriceItem.attribute_values).selectinload(
            SupplierPriceItemAttributeValue.attribute_definition
        ),
        selectinload(SupplierPriceItem.attribute_values).selectinload(
            SupplierPriceItemAttributeValue.source_key
        ),
        selectinload(SupplierPriceItem.attribute_values).selectinload(
            SupplierPriceItemAttributeValue.option
        ),
        selectinload(SupplierPriceItem.supplier_category)
        .selectinload(SupplierCategory.mapping)
        .selectinload(SupplierCategoryMapping.global_category),
    )


async def _get_version_with_file(
    session: AsyncSession, version_id: uuid.UUID
) -> tuple[PriceVersion, PriceFile]:
    version = await session.get(PriceVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Версия прайса не найдена")
    price_file = await session.get(PriceFile, version.price_file_id)
    return version, price_file


@router.get("/price-versions/{version_id}", response_model=VersionOut)
async def get_version(
    version_id: uuid.UUID,
    _user: AuthUser = Depends(access_user_from_request),
    session: AsyncSession = Depends(get_session),
) -> VersionOut:
    version, price_file = await _get_version_with_file(session, version_id)
    return version_to_dto(version, price_file.original_filename)


def _load_grids_for_preview(data: bytes, filename: str) -> list[SheetGrid]:
    try:
        loader = loaders.detect_loader(data, filename)
    except loaders.UnknownFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if loader == loaders.LOADER_PDF_SCAN:
        raise HTTPException(status_code=409, detail="Требуется OCR — предпросмотр маппинга недоступен")
    try:
        return loaders.load_grids_for(loader, data, filename)
    except (loaders.DocumentConversionError, loaders.PdfNoTablesError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/price-versions/{version_id}/mapping-preview", response_model=MappingPreview)
async def mapping_preview(
    version_id: uuid.UUID,
    _user: AuthUser = Depends(access_user_from_request),
    session: AsyncSession = Depends(get_session),
) -> MappingPreview:
    version, price_file = await _get_version_with_file(session, version_id)
    data = storage.get_price_file(price_file.storage_key)
    grids = _load_grids_for_preview(data, price_file.original_filename)

    stored_by_section: dict[tuple[str, int], dict[int, dict]] = {}
    legacy_stored_by_sheet: dict[str, dict[int, dict]] = {}
    if version.mapping:
        for tbl in version.mapping.get("tables", []):
            sheet_name = tbl.get("sheet_name", "")
            columns = {
                int(c["index"]): c for c in tbl.get("columns", [])
            }
            header_row_index = tbl.get("header_row_index")
            if isinstance(header_row_index, int) and not isinstance(header_row_index, bool):
                stored_by_section[(sheet_name, header_row_index)] = columns
            else:
                legacy_stored_by_sheet[sheet_name] = columns

    tables: list[PreviewTable] = []
    for grid in grids:
        detections = detect_header_candidates(grid)
        # Keep the legacy preview usable for a wholly unrecognised sheet: an
        # operator can still assign mandatory roles manually. Recognised
        # sheets use every distinct vertical section instead of overwriting
        # their state by sheet name.
        if not detections:
            detections = [detect_header(grid)]
        legacy_header_row = detect_header(grid).header_row_index

        for detection_index, detection in enumerate(detections):
            header_row_index = detection.header_row_index
            section_end = (
                detections[detection_index + 1].header_row_index
                if detection_index + 1 < len(detections)
                else len(grid.rows)
            )
            if header_row_index is not None:
                profile_price_columns(
                    SheetGrid(name=grid.name, rows=grid.rows[:section_end]),
                    detection,
                )
            stored_cols = (
                stored_by_section.get((grid.name, header_row_index))
                if header_row_index is not None
                else None
            )
            if stored_cols is None and header_row_index == legacy_header_row:
                stored_cols = legacy_stored_by_sheet.get(grid.name)

            n_cols = grid.n_cols
            columns: list[PreviewColumn] = []
            suggestions = {suggestion.index: suggestion for suggestion in detection.columns}
            for idx in range(n_cols):
                suggestion = suggestions.get(idx)
                role = suggestion.role if suggestion else "ignore"
                confidence = suggestion.confidence if suggestion else 0.0
                descriptor = suggestion.price_descriptor if suggestion else None
                header = suggestion.header if suggestion else ""
                if stored_cols and idx in stored_cols:
                    stored = stored_cols[idx]
                    role = stored.get("role", role)
                    descriptor = stored.get("price_descriptor")
                    confidence = 1.0
                attribute_defaults = ATTRIBUTE_ROLE_DEFAULTS.get(role, (None, None, None))
                stored_attribute = stored_cols.get(idx) if stored_cols and idx in stored_cols else {}
                columns.append(
                    PreviewColumn(
                        index=idx,
                        header=header,
                        suggested_role=role,
                        confidence=confidence,
                        price_descriptor=PriceDescriptor(**descriptor) if descriptor else None,
                        attribute_code=stored_attribute.get("attribute_code", attribute_defaults[0]),
                        attribute_value_kind=(
                            valid_attribute_value_kind(stored_attribute.get("attribute_value_kind"))
                            or attribute_defaults[1]
                        ),
                        canonical_unit=stored_attribute.get("canonical_unit", attribute_defaults[2]),
                    )
                )

            start = header_row_index + 1 if header_row_index is not None else 0
            sample_rows: list[list[str | None]] = []
            for row in grid.rows[start : min(start + SAMPLE_ROWS_LIMIT, section_end)]:
                sample_rows.append(
                    [None if cell.value is None else str(cell.value) for cell in row[:n_cols]]
                    + [None] * (n_cols - len(row))
                )

            tables.append(
                PreviewTable(
                    sheet_name=grid.name,
                    header_row_index=header_row_index,
                    columns=columns,
                    sample_rows=sample_rows,
                )
            )
    return MappingPreview(tables=tables)


@router.put("/price-versions/{version_id}/mapping", response_model=JobAccepted, status_code=202)
async def update_mapping(
    version_id: uuid.UUID,
    payload: MappingIn,
    _admin: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> JobAccepted:
    version, _ = await _get_version_with_file(session, version_id)
    for tbl in payload.tables:
        roles = {column.role for column in tbl.columns}
        if "name" not in roles or "price" not in roles:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"В секции листа «{tbl.sheet_name}» должны быть назначены "
                    "обе обязательные роли: «Наименование» и «Цена»."
                ),
            )
        for col in tbl.columns:
            if col.role not in ROLES:
                raise HTTPException(status_code=422, detail=f"Неизвестная роль: {col.role}")
    version.mapping = payload.model_dump(exclude_none=True)
    await session.commit()

    from app.tasks import parse_price_version

    async_result = parse_price_version.delay(str(version.id))
    return JobAccepted(job_id=async_result.id)


@router.get("/price-versions/{version_id}/items", response_model=ItemsPage)
async def list_items(
    version_id: uuid.UUID,
    query: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _user: AuthUser = Depends(access_user_from_request),
    session: AsyncSession = Depends(get_session),
) -> ItemsPage:
    version = await session.get(PriceVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Версия прайса не найдена")

    stmt = select(SupplierPriceItem).where(SupplierPriceItem.price_version_id == version_id)
    if query:
        pattern = f"%{query.strip().lower()}%"
        stmt = stmt.where(
            or_(
                SupplierPriceItem.normalized_name.ilike(pattern),
                SupplierPriceItem.normalized_article.ilike(pattern),
            )
        )
    total = (
        await session.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    items = (
        (
            await session.execute(
                stmt.order_by(SupplierPriceItem.source_sheet, SupplierPriceItem.source_row)
                .limit(limit)
                .offset(offset)
                .options(
                    selectinload(SupplierPriceItem.values),
                    selectinload(SupplierPriceItem.attribute_values).selectinload(
                        SupplierPriceItemAttributeValue.attribute_definition
                    ),
                    selectinload(SupplierPriceItem.attribute_values).selectinload(
                        SupplierPriceItemAttributeValue.source_key
                    ),
                    selectinload(SupplierPriceItem.attribute_values).selectinload(
                        SupplierPriceItemAttributeValue.option
                    ),
                    selectinload(SupplierPriceItem.supplier_category)
                    .selectinload(SupplierCategory.mapping)
                    .selectinload(SupplierCategoryMapping.global_category),
                )
            )
        )
        .scalars()
        .all()
    )
    return ItemsPage(
        total=total,
        items=[item_to_dto(item) for item in items],
    )


@router.get("/price-versions/{version_id}/issues", response_model=IssuesPage)
async def list_issues(
    version_id: uuid.UUID,
    severity: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _user: AuthUser = Depends(access_user_from_request),
    session: AsyncSession = Depends(get_session),
) -> IssuesPage:
    version = await session.get(PriceVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Версия прайса не найдена")

    stmt = select(PriceParseIssue).where(PriceParseIssue.price_version_id == version_id)
    if severity:
        stmt = stmt.where(PriceParseIssue.severity == severity)
    total = (
        await session.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    issues = (
        (
            await session.execute(
                stmt.order_by(PriceParseIssue.severity, PriceParseIssue.row_no)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return IssuesPage(total=total, issues=[IssueOut.model_validate(i) for i in issues])


def ensure_price_version_activatable(version: PriceVersion) -> None:
    """Проверяет, можно ли сделать версию действующим общим прайсом.

    Разовый счёт/КП (`SUPPLIER_QUOTE`) живёт в своей заявке и участвует в
    подборе уже в статусе `validated`. Активация сделала бы его действующим
    прайсом поставщика и вытеснила настоящий общий прайс, поэтому она
    запрещена: общий каталог активных прайсов такие документы не меняют.
    """
    if version.document_type == PriceDocumentType.supplier_quote.value:
        raise HTTPException(
            status_code=409,
            detail=(
                "Разовый счёт/КП используется только в своей заявке "
                "и не активируется как общий прайс"
            ),
        )
    if version.check_batch_id is not None:
        raise HTTPException(
            status_code=409,
            detail="PRICE_LIST не может быть привязан к заявке",
        )
    if version.status != VersionStatus.validated.value:
        raise HTTPException(
            status_code=409,
            detail=(
                "Активировать можно только версию в статусе validated "
                f"(текущий: {version.status})"
            ),
        )


# Переход версии по жизненному циклу — статус ресурса, а не отдельное действие.
@router.put("/price-versions/{version_id}/status", response_model=VersionOut)
async def set_version_status(
    version_id: uuid.UUID,
    _admin: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> VersionOut:
    version, price_file = await _get_version_with_file(session, version_id)
    ensure_price_version_activatable(version)
    current_active = (
        (
            await session.execute(
                select(PriceVersion).where(
                    PriceVersion.supplier_id == version.supplier_id,
                    PriceVersion.status == VersionStatus.active.value,
                )
            )
        )
        .scalars()
        .all()
    )
    for old in current_active:
        old.status = VersionStatus.superseded.value
    version.status = VersionStatus.active.value
    version.activated_at = datetime.now(timezone.utc)
    await session.commit()

    # Индекс подбора расширяется новой версией и очищается от вытесненной:
    # позиции снятого прайса не должны отвечать на заявку.
    from app.tasks import build_item_embeddings, drop_version_embeddings

    for old in current_active:
        drop_version_embeddings.delay(str(old.id))
    build_item_embeddings.delay(str(version.id), False)
    return version_to_dto(version, price_file.original_filename)


def _content_disposition(filename: str) -> str:
    # RFC 5987: имена файлов кириллицей должны уходить в filename*
    from urllib.parse import quote

    return f"attachment; filename*=UTF-8''{quote(filename)}"


CSV_COLUMNS = [
    "sheet", "row", "name", "article", "unit", "category", "attributes",
    "price_kind", "vat_mode", "vat_rate_percent", "amount", "currency",
    "min_quantity", "max_quantity", "source_column_index", "on_request", "price_label",
]


def _parsed_rows(items) -> list[list[object]]:
    """Разбор в плоском виде: одна строка на цену, а не на позицию.

    Позиция со ступенями количества даёт столько строк, сколько у неё цен, и
    границы `min_quantity`/`max_quantity` видны колонками. Позиция без цен всё
    равно попадает в выгрузку — иначе её отсутствие пришлось бы искать
    сравнением с оригиналом.
    """

    rows: list[list[object]] = []
    for item in items:
        attributes = []
        for attribute in item.attribute_values:
            display_value = attribute_scalar_value(attribute)
            value_text = attribute.raw_value or (
                "" if display_value is None else str(display_value)
            )
            unit = attribute.attribute_definition.canonical_unit
            attributes.append(
                f"{attribute.source_key.source_header}: {value_text}{f' {unit}' if unit else ''}"
            )

        base = [
            item.source_sheet,
            item.source_row,
            item.source_name,
            item.source_article or "",
            item.unit_code or "",
            item.category_path or "",
            "; ".join(attributes),
        ]
        if not item.values:
            rows.append(base + ["", "", "", "", "", "", "", "", "", ""])
            continue
        for value in item.values:
            rows.append(
                base
                + [
                    value.price_kind,
                    value.vat_mode or "",
                    "" if value.vat_rate_percent is None else str(value.vat_rate_percent),
                    "" if value.amount is None else str(value.amount),
                    value.currency or "",
                    "" if value.min_quantity is None else str(value.min_quantity),
                    "" if value.max_quantity is None else str(value.max_quantity),
                    "" if value.source_column_index is None else str(value.source_column_index),
                    "да" if value.is_on_request else "",
                    value.source_label or "",
                ]
            )
    return rows


@router.get("/price-versions/{version_id}/export")
async def export_parsed(
    version_id: uuid.UUID,
    format: Literal["csv", "json"] = Query(default="csv"),
    _user: AuthUser = Depends(access_user_from_request),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Выгрузка разобранной версии. Формат выбирается параметром, а не путём:
    `export.csv` и `export.json` были одним ресурсом в двух представлениях."""
    if format == "json":
        return await _export_json(version_id, session)
    return await _export_csv(version_id, session)


async def _export_csv(version_id: uuid.UUID, session: AsyncSession) -> Response:
    """Разобранная версия таблицей — то, что открывается в Excel.

    `export.json` отдаёт всё, включая атрибуты и замечания, но читать его
    глазами нельзя. Здесь одна строка на цену: позиция со ступенями количества
    видна как несколько строк с границами партии.
    """
    version, price_file = await _get_version_with_file(session, version_id)
    items = (
        (
            await session.execute(
                select(SupplierPriceItem)
                .where(SupplierPriceItem.price_version_id == version.id)
                .order_by(SupplierPriceItem.source_sheet, SupplierPriceItem.source_row)
                .options(
                    selectinload(SupplierPriceItem.values),
                    selectinload(SupplierPriceItem.attribute_values).selectinload(
                        SupplierPriceItemAttributeValue.attribute_definition
                    ),
                    selectinload(SupplierPriceItem.attribute_values).selectinload(
                        SupplierPriceItemAttributeValue.source_key
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    buffer = io.StringIO()
    # Разделитель «;» и BOM: русский Excel открывает такой файл сразу, без
    # мастера импорта, и не ломает кириллицу.
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    writer.writerows(_parsed_rows(items))
    base = (price_file.original_filename if price_file else str(version.id)).rsplit(".", 1)[0]
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": _content_disposition(f"{base}_v{version.version_no}.csv")},
    )


async def _export_json(version_id: uuid.UUID, session: AsyncSession) -> Response:
    """Полный результат разбора версии одним JSON — для проверки распарсенных
    данных против оригинального файла."""
    version, price_file = await _get_version_with_file(session, version_id)
    items = (
        (
            await session.execute(
                select(SupplierPriceItem)
                .where(SupplierPriceItem.price_version_id == version.id)
                .order_by(SupplierPriceItem.source_sheet, SupplierPriceItem.source_row)
                .options(
                    selectinload(SupplierPriceItem.values),
                    selectinload(SupplierPriceItem.attribute_values).selectinload(
                        SupplierPriceItemAttributeValue.attribute_definition
                    ),
                    selectinload(SupplierPriceItem.attribute_values).selectinload(
                        SupplierPriceItemAttributeValue.source_key
                    ),
                    selectinload(SupplierPriceItem.attribute_values).selectinload(
                        SupplierPriceItemAttributeValue.option
                    ),
                    selectinload(SupplierPriceItem.supplier_category)
                    .selectinload(SupplierCategory.mapping)
                    .selectinload(SupplierCategoryMapping.global_category),
                )
            )
        )
        .scalars()
        .all()
    )
    issues = (
        (
            await session.execute(
                select(PriceParseIssue).where(PriceParseIssue.price_version_id == version.id)
            )
        )
        .scalars()
        .all()
    )
    payload = {
        "version": {
            "id": str(version.id),
            "supplier_id": str(version.supplier_id),
            "version_no": version.version_no,
            "status": version.status,
            "loader": version.loader,
            "rule_match": version.rule_match,
            "parse_rule_id": str(version.parse_rule_id) if version.parse_rule_id else None,
            "original_filename": price_file.original_filename if price_file else None,
            "sha256": price_file.sha256 if price_file else None,
            "item_count": version.item_count,
            "rows_accepted": version.rows_accepted,
            "warnings_count": version.warnings_count,
            "errors_count": version.errors_count,
            "quality_score": version.quality_score,
            "created_at": version.created_at.isoformat() if version.created_at else None,
            "activated_at": version.activated_at.isoformat() if version.activated_at else None,
        },
        "mapping": version.mapping,
        "items": [
            {
                "source_name": it.source_name,
                "normalized_name": it.normalized_name,
                "source_article": it.source_article,
                "normalized_article": it.normalized_article,
                "unit": it.unit_code,
                "category_path": it.category_path,
                "category": ItemCategoryContextOut(
                    **item_category_context(it)
                ).model_dump(mode="json"),
                "dimensions": it.dimensions,
                "description": it.description,
                "attributes": [
                    _item_attribute_to_dto(value).model_dump(mode="json")
                    for value in it.attribute_values
                ],
                "source": {
                    "sheet": it.source_sheet,
                    "row": it.source_row,
                    "section_id": it.source_section_id,
                    "panel_index": it.source_panel_index,
                    "column_start": it.source_column_start,
                    "column_end": it.source_column_end,
                },
                "prices": [
                    {
                        "kind": v.price_kind,
                        "amount": str(v.amount) if v.amount is not None else None,
                        "qualifier": v.qualifier,
                        "amount_from": str(v.amount_from) if v.amount_from is not None else None,
                        "amount_to": str(v.amount_to) if v.amount_to is not None else None,
                        "currency": v.currency,
                        "currency_source": v.currency_source,
                        "vat_mode": v.vat_mode,
                        "vat_rate_percent": str(v.vat_rate_percent) if v.vat_rate_percent is not None else None,
                        "min_quantity": str(v.min_quantity) if v.min_quantity is not None else None,
                        "max_quantity": str(v.max_quantity) if v.max_quantity is not None else None,
                        "basis_quantity": (
                            str(v.basis_quantity) if v.basis_quantity is not None else None
                        ),
                        "basis_unit": v.basis_unit,
                        "raw_value": v.raw_value,
                        "source_column_index": v.source_column_index,
                        "is_on_request": v.is_on_request,
                        "source_label": v.source_label,
                    }
                    for v in it.values
                ],
            }
            for it in items
        ],
        "issues": [
            {
                "severity": i.severity,
                "code": i.code,
                "sheet": i.sheet,
                "row_no": i.row_no,
                "column_ref": i.column_ref,
                "source_value": i.source_value,
                "message": i.message,
            }
            for i in issues
        ],
    }
    body = json.dumps(payload, ensure_ascii=False, indent=1)
    base = (price_file.original_filename if price_file else str(version.id)).rsplit(".", 1)[0]
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": _content_disposition(f"{base}_v{version.version_no}.json")},
    )
