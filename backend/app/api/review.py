"""Ручная проверка импорта: очередь, черновик, dry-run и применение.

Отдельный ресурс, а не часть `/price-imports`: потребитель другой (браузерный
администратор), жизненный цикл свой, а изменения черновика идут под собственной
ревизией.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.auth import AuthUser
from app.db import get_session
from app.import_auth import get_import_admin, get_import_reader, scoped_import
from app.models import (
    PriceFile,
    PriceImport,
    PriceImportReview,
    PriceImportReviewSection,
    PriceImportStatus,
    Project,
    ServiceClient,
    Supplier,
)
from app.project_context import get_current_project
from app.schemas import (
    PriceImportReviewOut,
    ReviewActionAccepted,
    ReviewActivateIn,
    ReviewApplyIn,
    ReviewDraftPatchIn,
    ReviewQueuePage,
    ReviewQueueSectionSummary,
    ReviewSectionDetail,
    ReviewValidateIn,
    ReviewValidationOut,
)


router = APIRouter(prefix="/review", tags=["review"])


@router.get("/queue", response_model=ReviewQueuePage)
async def get_price_import_review_queue(
    status: str | None = Query(default=None),
    reason: str | None = Query(default=None, max_length=64),
    query: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: ServiceClient | AuthUser = Depends(get_import_reader),
    project: Project = Depends(get_current_project),
    session: AsyncSession = Depends(get_session),
) -> ReviewQueuePage:
    from app.review_service import ensure_review_required_invariant

    actor, _ = _review_actor(principal)
    await ensure_review_required_invariant(
        session,
        project_id=project.id,
        actor=actor,
        service_client_id=principal.id if isinstance(principal, ServiceClient) else None,
    )
    joins = (
        PriceImportReviewSection.__table__
        .join(
            PriceImportReview,
            PriceImportReview.id == PriceImportReviewSection.price_import_review_id,
        )
        .join(PriceImport, PriceImport.id == PriceImportReviewSection.price_import_id)
        .join(Supplier, Supplier.id == PriceImport.supplier_id)
        .join(PriceFile, PriceFile.id == PriceImport.price_file_id)
    )
    filters = [PriceFile.project_id == project.id]
    if isinstance(principal, ServiceClient):
        filters.append(PriceImport.service_client_id == principal.id)
    if reason:
        filters.append(PriceImportReviewSection.reason_code == reason)
    if query:
        pattern = f"%{query.strip()}%"
        filters.append(
            or_(
                Supplier.name.ilike(pattern),
                PriceFile.original_filename.ilike(pattern),
                PriceImportReviewSection.sheet_name.ilike(pattern),
                PriceImportReviewSection.section_id.ilike(pattern),
            )
        )
    total_filters = [*filters]
    if status:
        filters.append(PriceImportReviewSection.status == status)
    total = int(
        await session.scalar(select(func.count()).select_from(joins).where(*filters)) or 0
    )
    totals_rows = (
        await session.execute(
            select(PriceImportReviewSection.status, func.count())
            .select_from(joins)
            .where(*total_filters)
            .group_by(PriceImportReviewSection.status)
        )
    ).all()
    rows = (
        await session.execute(
            select(
                PriceImportReviewSection,
                PriceImportReview,
                PriceImport,
                Supplier,
                PriceFile,
            )
            .select_from(joins)
            .where(*filters)
            .order_by(
                PriceImportReviewSection.status,
                PriceFile.original_filename,
                PriceImportReviewSection.sheet_name,
                PriceImportReviewSection.start_row_index,
                PriceImportReviewSection.section_id,
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    can_edit = isinstance(principal, AuthUser) and principal.role == "admin"
    items = [
        ReviewQueueSectionSummary(
            id=section.id,
            import_id=price_import.id,
            review_id=review.id,
            section_id=section.section_id,
            supplier_name=supplier.name,
            filename=price_file.original_filename,
            sheet_name=section.sheet_name,
            section_kind=section.section_kind,
            start_row_index=section.start_row_index,
            end_row_index=section.end_row_index,
            status=section.status,
            attempt_status=section.attempt_status,
            reason_code=section.reason_code,
            revision=review.revision,
            review_status=review.status,
            decision_count=int((section.decision_summary or {}).get("count", 0)),
            can_edit=can_edit
            and price_import.status == PriceImportStatus.review_required.value,
            updated_at=section.updated_at,
        )
        for section, review, price_import, supplier, price_file in rows
    ]
    return ReviewQueuePage(
        total=total,
        totals={str(name): int(count) for name, count in totals_rows},
        items=items,
    )

def _review_actor(principal: ServiceClient | AuthUser) -> tuple[str, bool]:
    if isinstance(principal, AuthUser):
        return principal.username, principal.role == "admin"
    return f"service:{principal.id}", False


@router.get("/{import_id}", response_model=PriceImportReviewOut)
async def get_price_import_review(
    import_id: uuid.UUID,
    principal: ServiceClient | AuthUser = Depends(get_import_reader),
    session: AsyncSession = Depends(get_session),
) -> PriceImportReviewOut:
    from app.review_service import build_review_snapshot

    price_import = await scoped_import(session, principal, import_id)
    if price_import.status not in {
        PriceImportStatus.review_required.value,
        PriceImportStatus.completed.value,
        PriceImportStatus.reparsing.value,
        PriceImportStatus.quality_check.value,
        PriceImportStatus.failed.value,
    }:
        raise HTTPException(status_code=409, detail="Review ещё недоступен для этого import")
    actor, can_edit = _review_actor(principal)
    return PriceImportReviewOut.model_validate(
        await build_review_snapshot(
            session,
            price_import=price_import,
            actor=actor,
            can_edit=can_edit,
        )
    )


@router.get(
    "/{import_id}/sections/{section_id}",
    response_model=ReviewSectionDetail,
)
async def get_price_import_review_section(
    import_id: uuid.UUID,
    section_id: str,
    row_offset: int = Query(default=0, ge=0),
    row_limit: int = Query(default=100, ge=1, le=200),
    principal: ServiceClient | AuthUser = Depends(get_import_reader),
    session: AsyncSession = Depends(get_session),
) -> ReviewSectionDetail:
    from app.review_service import build_review_snapshot
    from app.review_validation import load_source

    price_import = await scoped_import(session, principal, import_id)
    actor, can_edit = _review_actor(principal)
    snapshot = await build_review_snapshot(
        session, price_import=price_import, actor=actor, can_edit=can_edit
    )
    context = next(
        (row for row in snapshot["sections"] if str(row.get("section_id")) == section_id),
        None,
    )
    if context is None:
        raise HTTPException(status_code=404, detail="Секция review не найдена")

    price_file = await session.get(PriceFile, price_import.price_file_id)
    if price_file is not None:
        try:
            data = storage.get_price_file(price_file.storage_key)
            _loader, grids = load_source(data, price_file.original_filename)
            grid = next((row for row in grids if row.name == context.get("sheet_name")), None)
            if grid is not None:
                section_start = max(0, int(context.get("section_start_row_index") or 0))
                section_end = min(
                    len(grid.rows),
                    int(context.get("section_end_row_index") or len(grid.rows)),
                )
                start = min(section_end, section_start + row_offset)
                end = min(section_end, start + row_limit)

                def snapshot_row(row_index: int) -> dict:
                    cells = []
                    for cell in grid.rows[row_index]:
                        value = cell.display_value if cell.display_value is not None else cell.value
                        if value is not None and not isinstance(value, (str, int, float, bool)):
                            value = str(value)
                        cells.append(
                            {
                                "value": value,
                                "is_bold": bool(cell.is_bold),
                                "is_merged_fill": bool(cell.is_merged_fill),
                            }
                        )
                    return {"row_index": row_index, "cells": cells}

                context = dict(context)
                context["sample_rows"] = [snapshot_row(index) for index in range(start, end)]
        except HTTPException:
            pass

    attempt = next(
        (row for row in snapshot["attempts"] if str(row.get("section_id")) == section_id),
        None,
    )
    start_row = int(context.get("section_start_row_index") or 0) + 1
    end_row = int(context.get("section_end_row_index") or start_row)
    decisions = [
        row
        for row in snapshot["section_decisions"]
        if row.get("sheet") == context.get("sheet_name")
        and int(row.get("row_end") or 0) >= start_row
        and int(row.get("row_start") or 0) <= end_row
    ]
    return ReviewSectionDetail.from_context(
        import_id=import_id,
        review_id=snapshot["review_id"],
        revision=snapshot["revision"],
        context=context,
        mapping=snapshot["mapping"],
        decisions=decisions,
        attempt=attempt,
        validation=snapshot["validation"],
        actions=snapshot["actions"],
    )


@router.patch("/{import_id}", response_model=PriceImportReviewOut)
async def patch_review_draft(
    import_id: uuid.UUID,
    payload: ReviewDraftPatchIn,
    admin: AuthUser = Depends(get_import_admin),
    session: AsyncSession = Depends(get_session),
) -> PriceImportReviewOut:
    """Изменить черновик review: mapping, правило, решения по строкам, валюту.

    Правки применяются последовательно под одной ревизией: первая занимает
    `payload.revision`, каждая следующая — ту, что вернула предыдущая. Так
    патч из нескольких полей не конфликтует сам с собой, а конкурентная правка
    другого администратора по-прежнему ловится на первом же шаге.
    """

    from app.review_service import (
        build_review_snapshot,
        update_currency,
        update_decisions,
        update_mapping,
        update_rule,
    )

    price_import = await scoped_import(session, admin, import_id)
    changed = payload.model_fields_set
    revision = payload.revision

    if "mapping" in changed:
        if payload.mapping is None:
            raise HTTPException(status_code=422, detail="mapping нельзя очистить")
        review = await update_mapping(
            session,
            price_import=price_import,
            revision=revision,
            mapping=payload.mapping.model_dump(exclude_none=True),
            actor=admin,
        )
        revision = review.revision
    if "rule" in changed:
        if payload.rule is None:
            raise HTTPException(status_code=422, detail="rule нельзя очистить")
        review = await update_rule(
            session,
            price_import=price_import,
            revision=revision,
            name=payload.rule.name,
            recipe=payload.rule.recipe,
            actor=admin,
        )
        revision = review.revision
    if "decisions" in changed:
        review = await update_decisions(
            session,
            price_import=price_import,
            revision=revision,
            decisions=[decision.model_dump() for decision in (payload.decisions or [])],
            actor=admin,
        )
        revision = review.revision
    if "currency" in changed:
        await update_currency(
            session,
            price_import=price_import,
            revision=revision,
            currency=payload.currency,
            actor=admin,
        )

    return PriceImportReviewOut.model_validate(
        await build_review_snapshot(
            session, price_import=price_import, actor=admin.username, can_edit=True
        )
    )


@router.post("/{import_id}/validate", response_model=ReviewValidationOut)
async def validate_price_import_review(
    import_id: uuid.UUID,
    payload: ReviewValidateIn,
    admin: AuthUser = Depends(get_import_admin),
    session: AsyncSession = Depends(get_session),
) -> ReviewValidationOut:
    from app.review_service import validate_review

    price_import = await scoped_import(session, admin, import_id)
    review = await validate_review(
        session,
        price_import=price_import,
        revision=payload.revision,
        actor=admin,
    )
    return ReviewValidationOut.model_validate(review.validation)


@router.post("/{import_id}/apply", response_model=ReviewActionAccepted, status_code=202)
async def apply_price_import_review(
    import_id: uuid.UUID,
    payload: ReviewApplyIn,
    admin: AuthUser = Depends(get_import_admin),
    session: AsyncSession = Depends(get_session),
) -> ReviewActionAccepted:
    from app.review_service import request_apply, revert_apply_publication
    from app.tasks import apply_price_import_review as apply_task

    price_import = await scoped_import(session, admin, import_id)
    review, enqueue = await request_apply(
        session,
        price_import=price_import,
        revision=payload.revision,
        actor=admin,
    )
    job_id = None
    if enqueue:
        try:
            job_id = apply_task.delay(str(import_id)).id
        except Exception as exc:  # noqa: BLE001 - broker publication is part of acceptance
            await revert_apply_publication(
                session,
                price_import=price_import,
                review=review,
                actor=admin,
            )
            raise HTTPException(
                status_code=503,
                detail="Не удалось поставить повторный разбор в очередь",
            ) from exc
    return ReviewActionAccepted(
        import_id=import_id,
        review_id=review.id,
        revision=review.revision,
        status=review.status,
        job_id=job_id,
    )


@router.post("/{import_id}/activate-rule", response_model=ReviewActionAccepted)
async def activate_price_import_review_rule(
    import_id: uuid.UUID,
    payload: ReviewActivateIn,
    admin: AuthUser = Depends(get_import_admin),
    session: AsyncSession = Depends(get_session),
) -> ReviewActionAccepted:
    from app.review_service import activate_review_rule

    price_import = await scoped_import(session, admin, import_id)
    review = await activate_review_rule(
        session,
        price_import=price_import,
        revision=payload.revision,
        name=payload.name,
        actor=admin,
    )
    return ReviewActionAccepted(
        import_id=import_id,
        review_id=review.id,
        revision=review.revision,
        status=review.status,
    )
