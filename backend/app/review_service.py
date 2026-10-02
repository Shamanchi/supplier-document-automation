"""Transactional workflow for import-scoped human review."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.auth import AuthUser
from app.currencies import default_currency
from app.models import (
    ParseRule,
    ParseRuleProvenance,
    ParseRuleStatus,
    PriceFile,
    PriceImport,
    PriceImportEvent,
    PriceImportReview,
    PriceImportReviewEvent,
    PriceImportReviewSection,
    PriceImportReviewSectionStatus,
    PriceImportReviewStatus,
    PriceImportStatus,
    PriceVersion,
    RuleGenerationAttempt,
)
from app.parsing import loaders
from app.parsing.mapping import build_auto_mapping, stored_mapping_to_tables
from app.parsing.rule_proposal import build_tabular_section_contexts
from app.parsing.rules import (
    compute_fingerprint,
    compute_header_hash,
    recipe_from_decision,
    validation_metrics,
)
from app.review_validation import (
    dry_run_summary,
    load_source,
    parse_review_draft,
    review_state_hash,
    validate_mapping_payload,
    validate_non_price_decisions,
)


logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(review: PriceImportReview, event_type: str, actor: str, payload: dict) -> PriceImportReviewEvent:
    return PriceImportReviewEvent(
        price_import_review_id=review.id,
        price_import_id=review.price_import_id,
        event_type=event_type,
        revision=review.revision,
        actor=actor,
        payload=payload,
    )


def section_context_hash(context: dict) -> str:
    payload = json.dumps(
        context, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _attempt_value(attempt: object, name: str, default=None):
    return attempt.get(name, default) if isinstance(attempt, dict) else getattr(attempt, name, default)


async def sync_review_sections(
    session: AsyncSession,
    *,
    review: PriceImportReview,
    contexts: list[dict],
    attempts: list[object],
) -> list[PriceImportReviewSection]:
    existing = (
        await session.execute(
            select(PriceImportReviewSection).where(
                PriceImportReviewSection.price_import_review_id == review.id
            )
        )
    ).scalars().all()
    by_id = {row.section_id: row for row in existing}
    attempts_by_id = {str(_attempt_value(row, "section_id")): row for row in attempts}
    result: list[PriceImportReviewSection] = []
    for context in contexts:
        section_id = str(context.get("section_id") or "")
        if not section_id:
            continue
        attempt = attempts_by_id.get(section_id)
        validation = _attempt_value(attempt, "validation", {}) or {}
        reason_code = validation.get("reason_code") if isinstance(validation, dict) else None
        row = by_id.get(section_id)
        if row is None:
            row = PriceImportReviewSection(
                price_import_review_id=review.id,
                price_import_id=review.price_import_id,
                section_id=section_id,
            )
            session.add(row)
            by_id[section_id] = row
        row.source_context_hash = section_context_hash(context)
        row.sheet_name = str(context.get("sheet_name") or "")[:255]
        row.section_kind = str(context.get("section_kind") or "unknown")[:64]
        row.start_row_index = context.get("section_start_row_index")
        row.end_row_index = context.get("section_end_row_index")
        row.attempt_status = _attempt_value(attempt, "status")
        row.reason_code = str(reason_code)[:64] if reason_code else None
        result.append(row)
    await session.flush()
    return sorted(result, key=lambda row: (row.sheet_name, row.start_row_index or -1, row.section_id))


async def update_review_section_state(
    session: AsyncSession,
    *,
    review: PriceImportReview,
    status: PriceImportReviewSectionStatus,
    actor: str,
) -> None:
    """Keep the operator queue aligned with review mutations and outcomes."""

    sections = (
        await session.execute(
            select(PriceImportReviewSection).where(
                PriceImportReviewSection.price_import_review_id == review.id
            )
        )
    ).scalars().all()
    decisions = review.section_decisions or []
    for section in sections:
        scoped = []
        for decision in decisions:
            if decision.get("sheet") != section.sheet_name:
                continue
            excel_start = (section.start_row_index or 0) + 1
            excel_end = (section.end_row_index if section.end_row_index is not None else excel_start - 1) + 1
            if int(decision.get("row_end", -1)) < excel_start or int(
                decision.get("row_start", 0)
            ) > excel_end:
                continue
            scoped.append(decision)
        section.status = status.value
        section.decision_summary = {
            "count": len(scoped),
            "accepted": 0,
            "rejected": len(scoped),
        }
        section.updated_by = actor
        section.updated_at = _now()


def _recipe_review_mapping(recipe: dict, grids) -> dict | None:
    """Full stored mapping for a generic sheet-columns rule recipe.

    A fresh review used to open with an empty mapping (version.mapping is
    None for rule-parsed imports), so every column looked ``ignore`` and the
    operator had to re-assign roles by hand.  Workbook-layout/PDF recipes
    validate through their own compiler and are skipped by the caller.
    """
    from app.parsing.headers import normalize_header
    from app.parsing.mapping import ROLES

    tables: list[dict] = []
    for sheet in recipe.get("sheets", []):
        if sheet.get("skip"):
            continue
        match = sheet.get("match") or {}
        grid = None
        pattern = normalize_header(match.get("name_pattern") or "")
        if pattern:
            grid = next((g for g in grids if normalize_header(g.name) == pattern), None)
        if grid is None:
            index = match.get("or_index")
            if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(grids):
                grid = grids[index]
        if grid is None:
            continue
        columns_by_index: dict[int, dict] = {}
        for column in sheet.get("columns", []):
            try:
                index = int(column["index"])
            except (KeyError, TypeError, ValueError):
                continue
            if index >= 0:
                columns_by_index[index] = column
        header_row_index = sheet.get("header_row")
        if (
            isinstance(header_row_index, int)
            and not isinstance(header_row_index, bool)
            and 0 <= header_row_index < len(grid.rows)
        ):
            width = len(grid.rows[header_row_index])
        else:
            width = max(columns_by_index, default=-1) + 1
        columns: list[dict] = []
        for index in range(width):
            source = columns_by_index.get(index)
            if source is None:
                columns.append({"index": index, "role": "ignore"})
                continue
            role = source.get("role", "ignore")
            if role not in ROLES:
                role = "ignore"
            entry: dict = {"index": index, "role": role}
            if source.get("price_descriptor"):
                entry["price_descriptor"] = dict(source["price_descriptor"])
            if source.get("attribute_code"):
                entry["attribute_code"] = str(source["attribute_code"])
            attribute_value_kind = source.get("attribute_value_kind")
            if isinstance(attribute_value_kind, str):
                entry["attribute_value_kind"] = attribute_value_kind
            if source.get("canonical_unit"):
                entry["canonical_unit"] = str(source["canonical_unit"])
            columns.append(entry)
        roles = {column.get("role") for column in columns}
        if not {"name", "price"}.issubset(roles):
            continue
        table: dict = {"sheet_name": grid.name, "columns": columns}
        if (
            isinstance(header_row_index, int)
            and not isinstance(header_row_index, bool)
            and 0 <= header_row_index < len(grid.rows)
        ):
            table["header_row_index"] = header_row_index
        tables.append(table)
    if not tables:
        return None
    return {"tables": tables}


async def get_or_create_review(
    session: AsyncSession, *, price_import: PriceImport, actor: str
) -> PriceImportReview:
    review = (
        await session.execute(
            select(PriceImportReview).where(PriceImportReview.price_import_id == price_import.id)
        )
    ).scalar_one_or_none()
    if review is not None:
        return review
    version = await session.get(PriceVersion, price_import.price_version_id)
    rule_id = price_import.parse_rule_id or (version.parse_rule_id if version else None)
    rule = await session.get(ParseRule, rule_id) if rule_id else None
    review = PriceImportReview(
        price_import_id=price_import.id,
        mapping=version.mapping if version else None,
        rule_recipe=rule.recipe if rule else None,
        rule_name=rule.name if rule else None,
        parse_rule_id=rule.id if rule and rule.status == ParseRuleStatus.draft.value else None,
        created_by=actor,
        updated_by=actor,
    )
    session.add(review)
    try:
        await session.flush()
    except IntegrityError:
        # A concurrent reader created the unique review row first: reuse it
        # instead of failing the whole request.
        await session.rollback()
        review = (
            await session.execute(
                select(PriceImportReview).where(
                    PriceImportReview.price_import_id == price_import.id
                )
            )
        ).scalar_one()
        return review
    session.add(_event(review, "review_created", actor, {"source": "import_snapshot"}))
    await session.commit()
    return review


async def _collect_review_material(
    session: AsyncSession, *, price_import: PriceImport, review: PriceImportReview
) -> tuple[list[RuleGenerationAttempt], list[dict]]:
    """Model attempts plus deterministic tabular contexts for one import.

    Attempt contexts win on section-id collisions; the stored source file is
    a fallback, so a missing or OCR-only blob never hides recorded attempts.
    """
    attempts = (
        await session.execute(
            select(RuleGenerationAttempt)
            .where(RuleGenerationAttempt.price_import_id == price_import.id)
            .order_by(RuleGenerationAttempt.created_at, RuleGenerationAttempt.id)
        )
    ).scalars().all()
    contexts_by_id = {str(attempt.section_id): attempt.input_context for attempt in attempts}
    price_file = await session.get(PriceFile, price_import.price_file_id)
    if price_file is not None:
        try:
            data = storage.get_price_file(price_file.storage_key)
            _loader, grids = load_source(data, price_file.original_filename)
            for context in build_tabular_section_contexts(grids):
                contexts_by_id.setdefault(str(context["section_id"]), context)
            if review.mapping is None and review.rule_recipe is not None:
                from app.parsing.pdf_layout import is_pdf_layout_recipe
                from app.parsing.workbook_layout import is_workbook_layout_recipe

                recipe = review.rule_recipe
                if not is_pdf_layout_recipe(recipe) and not is_workbook_layout_recipe(recipe):
                    prefilled = _recipe_review_mapping(recipe, grids)
                    if prefilled is not None:
                        review.mapping = prefilled
        except HTTPException:
            pass
    sections = [contexts_by_id[key] for key in sorted(contexts_by_id)]
    return list(attempts), sections


async def ensure_review(
    session: AsyncSession, *, price_import: PriceImport, actor: str
) -> tuple[PriceImportReview, bool]:
    """Idempotent repair of the review_required invariant (Scenario E).

    A review_required import must already expose a review row and durable
    section rows before the engineering UI opens it, otherwise the
    section-joined operator queue stays empty and the import is a dead end.
    The second return value is True when this call materialised rows.
    """
    review = await get_or_create_review(session, price_import=price_import, actor=actor)
    section_count = int(
        await session.scalar(
            select(func.count())
            .select_from(PriceImportReviewSection)
            .where(PriceImportReviewSection.price_import_review_id == review.id)
        )
        or 0
    )
    if section_count:
        return review, False
    attempts, sections = await _collect_review_material(
        session, price_import=price_import, review=review
    )
    if not sections:
        return review, False
    await sync_review_sections(session, review=review, contexts=sections, attempts=attempts)
    await session.commit()
    return review, True


async def ensure_review_required_invariant(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    actor: str,
    service_client_id: uuid.UUID | None = None,
) -> int:
    """Repair-on-read for the operator queue (Scenario E invariant).

    Every import currently in review_required must own a review row with at
    least one section row, otherwise it is invisible to the section-joined
    /review/queue.  Imports with zero section rows are re-materialised here;
    steady-state queue reads do no source parsing.

    VAT_RATE_REQUIRED imports are excluded: their operator action is the
    VAT selector on the import page (PATCH resumes the pipeline), not a
    section review, and materialising section rows for them would leave
    stale pending queue entries after the PATCH-completed import.
    """
    query = (
        select(PriceImport.id)
        .join(PriceFile, PriceFile.id == PriceImport.price_file_id)
        .outerjoin(PriceImportReview, PriceImportReview.price_import_id == PriceImport.id)
        .outerjoin(
            PriceImportReviewSection,
            PriceImportReviewSection.price_import_review_id == PriceImportReview.id,
        )
        .where(
            PriceImport.status == PriceImportStatus.review_required.value,
            or_(
                PriceImport.error_code.is_(None),
                PriceImport.error_code != "VAT_RATE_REQUIRED",
            ),
            PriceFile.project_id == project_id,
        )
    )
    if service_client_id is not None:
        query = query.where(PriceImport.service_client_id == service_client_id)
    orphan_ids = (
        await session.execute(
            query.group_by(PriceImport.id).having(
                func.count(PriceImportReviewSection.id) == 0
            )
        )
    ).scalars().all()
    repaired = 0
    for import_id in orphan_ids:
        price_import = await session.get(PriceImport, import_id)
        if price_import is None:
            continue
        try:
            _review, changed = await ensure_review(
                session, price_import=price_import, actor=actor
            )
            if changed:
                repaired += 1
        except IntegrityError:
            # A concurrent reader repaired the same import: roll back the
            # failed flush; the next queue read sees the winner's rows.
            await session.rollback()
        except Exception as exc:  # noqa: BLE001 - one broken blob must not break the queue
            logger.warning("review repair skipped for import %s: %s", import_id, exc)
            await session.rollback()
    return repaired


async def build_review_snapshot(
    session: AsyncSession,
    *,
    price_import: PriceImport,
    actor: str,
    can_edit: bool,
) -> dict:
    review = await get_or_create_review(session, price_import=price_import, actor=actor)
    attempts, sections = await _collect_review_material(
        session, price_import=price_import, review=review
    )
    await sync_review_sections(session, review=review, contexts=sections, attempts=attempts)
    await session.commit()
    current_revision_valid = bool(
        review.validation
        and review.validation.get("revision") == review.revision
        and review.validation.get("state_hash")
        == review_state_hash(
            mapping=review.mapping,
            recipe=review.rule_recipe,
            decisions=review.section_decisions,
            currency=review.currency_override,
        )
    )
    actions: dict[str, bool | str] = {
        "can_edit": can_edit and price_import.status == PriceImportStatus.review_required.value,
        "can_validate": can_edit and price_import.status == PriceImportStatus.review_required.value,
        "can_apply": bool(
            can_edit
            and price_import.status == PriceImportStatus.review_required.value
            and current_revision_valid
            and review.validation.get("passed")
            and review.status == PriceImportReviewStatus.validated.value
        ),
        "can_activate": bool(
            can_edit
            and price_import.status == PriceImportStatus.completed.value
            and review.status == PriceImportReviewStatus.applied.value
            and review.last_applied_revision == review.revision
            and review.parse_rule_id
        ),
    }
    return {
        "import_id": price_import.id,
        "review_id": review.id,
        "revision": review.revision,
        "status": review.status,
        "mapping": review.mapping,
        "rule": (
            {"id": str(review.parse_rule_id) if review.parse_rule_id else None,
             "name": review.rule_name, "recipe": review.rule_recipe}
            if review.rule_recipe is not None
            else None
        ),
        "section_decisions": review.section_decisions,
        "currency_override": review.currency_override,
        "sections": sections,
        "attempts": [
            {
                "id": attempt.id,
                "section_id": attempt.section_id,
                "status": attempt.status,
                "provider": attempt.provider,
                "model": attempt.model,
                "prompt_version": attempt.prompt_version,
                "input_context": attempt.input_context,
                "proposal": attempt.proposal,
                "validation": attempt.validation,
                "error_message": attempt.error_message,
            }
            for attempt in attempts
        ],
        "validation": review.validation,
        "actions": actions,
        "updated_by": review.updated_by,
        "updated_at": review.updated_at,
    }


async def _locked_review(
    session: AsyncSession, *, price_import: PriceImport, revision: int
) -> PriceImportReview:
    review = (
        await session.execute(
            select(PriceImportReview)
            .where(PriceImportReview.price_import_id == price_import.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if review is None:
        raise HTTPException(status_code=409, detail="Сначала откройте review snapshot")
    if review.revision != revision:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REVIEW_REVISION_CONFLICT",
                "message": "Review уже изменён другим пользователем",
                "current_revision": review.revision,
            },
        )
    if price_import.status != PriceImportStatus.review_required.value:
        raise HTTPException(status_code=409, detail="Редактировать можно import в review_required")
    if review.status == PriceImportReviewStatus.applying.value:
        raise HTTPException(status_code=409, detail="Review уже применяется")
    return review


async def _finish_update(
    session: AsyncSession,
    *,
    review: PriceImportReview,
    actor: AuthUser,
    event_type: str,
    extra_events: list[tuple[str, dict]] | None = None,
) -> PriceImportReview:
    review.revision += 1
    review.status = PriceImportReviewStatus.draft.value
    review.validation = None
    review.validated_at = None
    review.updated_by = actor.username
    review.updated_at = _now()
    state_hash = review_state_hash(
        mapping=review.mapping,
        recipe=review.rule_recipe,
        decisions=review.section_decisions,
        currency=review.currency_override,
    )
    session.add(_event(review, event_type, actor.username, {"state_hash": state_hash}))
    for extra_type, extra_payload in extra_events or ():
        session.add(_event(review, extra_type, actor.username, extra_payload))
    await update_review_section_state(
        session,
        review=review,
        status=PriceImportReviewSectionStatus.in_progress,
        actor=actor.username,
    )
    await session.commit()
    return review


async def update_mapping(
    session: AsyncSession, *, price_import: PriceImport, revision: int, mapping: dict, actor: AuthUser
) -> PriceImportReview:
    validate_mapping_payload(mapping)
    review = await _locked_review(session, price_import=price_import, revision=revision)
    review.mapping = mapping
    review.rule_recipe = None
    review.rule_name = None
    review.parse_rule_id = None
    return await _finish_update(session, review=review, actor=actor, event_type="mapping_updated")


async def update_rule(
    session: AsyncSession,
    *,
    price_import: PriceImport,
    revision: int,
    name: str,
    recipe: dict,
    actor: AuthUser,
) -> PriceImportReview:
    from app.api.parse_rules import _validate_recipe_format

    _validate_recipe_format(recipe)
    review = await _locked_review(session, price_import=price_import, revision=revision)
    review.rule_recipe = recipe
    review.rule_name = name
    review.parse_rule_id = None
    return await _finish_update(session, review=review, actor=actor, event_type="rule_updated")


async def update_decisions(
    session: AsyncSession,
    *,
    price_import: PriceImport,
    revision: int,
    decisions: list[dict],
    actor: AuthUser,
) -> PriceImportReview:
    price_file = await session.get(PriceFile, price_import.price_file_id)
    data = storage.get_price_file(price_file.storage_key)
    _loader, grids = load_source(data, price_file.original_filename)
    overridden = validate_non_price_decisions(
        grids=grids, decisions=decisions, allow_override=True
    )
    review = await _locked_review(session, price_import=price_import, revision=revision)
    review.section_decisions = decisions
    extra_events: list[tuple[str, dict]] = []
    if overridden:
        # Обход гарда — не тихая правка: в журнал идут координаты скрытых
        # доказательств. Сами значения ячеек не пишем — аудит review
        # договорился быть content-free (`price_import_review_events`), а
        # значения оператор видит в ответе 422 и в исходном файле.
        extra_events.append(
            (
                "guard_overridden",
                {
                    "evidence": [
                        {key: item[key] for key in ("sheet", "row", "column")}
                        for item in overridden
                    ],
                    "count": len(overridden),
                    "reason_codes": sorted(
                        {
                            str(decision.get("reason_code"))
                            for decision in decisions
                            if decision.get("override_guard")
                        }
                    ),
                },
            )
        )
    return await _finish_update(
        session,
        review=review,
        actor=actor,
        event_type="decisions_updated",
        extra_events=extra_events,
    )


async def update_currency(
    session: AsyncSession,
    *,
    price_import: PriceImport,
    revision: int,
    currency: str | None,
    actor: AuthUser,
) -> PriceImportReview:
    from app.currencies import normalise_currency_hint

    try:
        normalized = normalise_currency_hint(currency)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    review = await _locked_review(session, price_import=price_import, revision=revision)
    review.currency_override = normalized
    return await _finish_update(session, review=review, actor=actor, event_type="currency_updated")


async def validate_review(
    session: AsyncSession,
    *,
    price_import: PriceImport,
    revision: int,
    actor: AuthUser,
) -> PriceImportReview:
    review = await _locked_review(session, price_import=price_import, revision=revision)
    price_file = await session.get(PriceFile, price_import.price_file_id)
    version = await session.get(PriceVersion, price_import.price_version_id)
    data = storage.get_price_file(price_file.storage_key)
    _loader, grids = load_source(data, price_file.original_filename)
    validate_non_price_decisions(
        grids=grids, decisions=review.section_decisions, allow_override=True
    )
    _loader, grids, result = parse_review_draft(
        data=data,
        filename=price_file.original_filename,
        mapping=review.mapping,
        recipe=review.rule_recipe,
    )
    summary = dry_run_summary(
        result=result,
        grids=grids,
        decisions=review.section_decisions,
        currency_override=review.currency_override,
        current_item_count=version.item_count,
        has_default_currency=await default_currency(session) is not None,
    )
    state_hash = review_state_hash(
        mapping=review.mapping,
        recipe=review.rule_recipe,
        decisions=review.section_decisions,
        currency=review.currency_override,
    )
    review.validation = {
        "revision": review.revision,
        "at": _now().isoformat(),
        "state_hash": state_hash,
        **summary,
    }
    review.validated_at = _now()
    review.status = (
        PriceImportReviewStatus.validated.value
        if summary["passed"]
        else PriceImportReviewStatus.draft.value
    )
    review.updated_by = actor.username
    review.updated_at = _now()
    session.add(
        _event(
            review,
            "validated",
            actor.username,
            {
                "passed": summary["passed"],
                "item_count": summary["metrics"]["item_count"],
                "confirmed_missed_positions": summary["metrics"]["confirmed_missed_positions"],
                "state_hash": state_hash,
            },
        )
    )
    await update_review_section_state(
        session,
        review=review,
        status=(
            PriceImportReviewSectionStatus.validated
            if summary["passed"]
            else PriceImportReviewSectionStatus.in_progress
        ),
        actor=actor.username,
    )
    await session.commit()
    return review


async def request_apply(
    session: AsyncSession,
    *,
    price_import: PriceImport,
    revision: int,
    actor: AuthUser,
) -> tuple[PriceImportReview, bool]:
    review = (
        await session.execute(
            select(PriceImportReview)
            .where(PriceImportReview.price_import_id == price_import.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if review is None:
        raise HTTPException(status_code=409, detail="Сначала откройте review snapshot")
    if review.revision != revision:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "REVIEW_REVISION_CONFLICT",
                "message": "Review уже изменён другим пользователем",
                "current_revision": review.revision,
            },
        )
    if review.last_applied_revision == revision and review.status in {
        PriceImportReviewStatus.applying.value,
        PriceImportReviewStatus.applied.value,
    }:
        return review, False
    if price_import.status != PriceImportStatus.review_required.value:
        raise HTTPException(status_code=409, detail="Применять можно import в review_required")
    expected_hash = review_state_hash(
        mapping=review.mapping,
        recipe=review.rule_recipe,
        decisions=review.section_decisions,
        currency=review.currency_override,
    )
    if not (
        review.status == PriceImportReviewStatus.validated.value
        and review.validation
        and review.validation.get("passed")
        and review.validation.get("revision") == revision
        and review.validation.get("state_hash") == expected_hash
    ):
        raise HTTPException(status_code=409, detail="Текущая revision не прошла dry-run")
    review.status = PriceImportReviewStatus.applying.value
    review.last_applied_revision = revision
    review.updated_by = actor.username
    review.updated_at = _now()
    previous = price_import.status
    price_import.status = PriceImportStatus.reparsing.value
    price_import.stage = PriceImportStatus.reparsing.value
    price_import.progress_percent = 75
    price_import.finished_at = None
    price_import.error_code = None
    price_import.error_message = None
    price_import.updated_at = _now()
    session.add(
        PriceImportEvent(
            price_import_id=price_import.id,
            event_type="review_apply_requested",
            from_status=previous,
            to_status=PriceImportStatus.reparsing.value,
            operation_id=f"review:{review.id}:{revision}",
            payload={"review_id": str(review.id), "revision": revision},
        )
    )
    session.add(_event(review, "apply_requested", actor.username, {"revision": revision}))
    await session.commit()
    return review, True


async def apply_review_revision(session: AsyncSession, import_id: uuid.UUID) -> str:
    """Apply once under the same process-independent lock used by imports."""

    bind = session.bind
    if bind is None or bind.dialect.name != "postgresql":
        return await _apply_review_revision_locked(session, import_id)
    lock_key = int.from_bytes(import_id.bytes[:8], byteorder="big", signed=True)
    async with bind.connect() as lock_connection:
        acquired = bool(
            await lock_connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_key)"), {"lock_key": lock_key}
            )
        )
        if not acquired:
            return "in_progress"
        try:
            return await _apply_review_revision_locked(session, import_id)
        finally:
            await lock_connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": lock_key}
            )
            await lock_connection.commit()


async def _apply_review_revision_locked(session: AsyncSession, import_id: uuid.UUID) -> str:
    price_import = await session.get(PriceImport, import_id)
    if price_import is None:
        return "not_found"
    review = (
        await session.execute(
            select(PriceImportReview).where(PriceImportReview.price_import_id == import_id)
        )
    ).scalar_one_or_none()
    if review is None or review.status != PriceImportReviewStatus.applying.value:
        return price_import.status
    version = await session.get(PriceVersion, price_import.price_version_id)
    price_file = await session.get(PriceFile, price_import.price_file_id)
    data = storage.get_price_file(price_file.storage_key)
    try:
        from app.parse_service import apply_parse_rule_to_version, parse_and_persist

        # A snapshot recipe without sheets (copied from a historical empty
        # rule) must not be applied verbatim: it would create a rule that the
        # editor shows as "В правиле нет листов". Fall back to the mapping
        # path, which rebuilds a real recipe from the confirmed parse.
        recipe_has_sheets = bool((review.rule_recipe or {}).get("sheets"))
        if review.rule_recipe is not None and recipe_has_sheets:
            rule = await session.get(ParseRule, review.parse_rule_id) if review.parse_rule_id else None
            if rule is not None and rule.project_id != price_file.project_id:
                raise ValueError("Правило review относится к другому проекту")
            loader, grids = load_source(data, price_file.original_filename)
            if rule is None:
                fingerprint = compute_fingerprint(loader, grids)
                header_hash = compute_header_hash(fingerprint)
                max_revision = (
                    await session.execute(
                        select(func.max(ParseRule.revision)).where(
                            ParseRule.project_id == price_file.project_id,
                            ParseRule.supplier_id == version.supplier_id,
                            ParseRule.loader == loader,
                            ParseRule.header_hash == header_hash,
                        )
                    )
                ).scalar_one()
                rule = ParseRule(
                    project_id=price_file.project_id,
                    supplier_id=version.supplier_id,
                    name=review.rule_name or f"Review: {price_file.original_filename}",
                    loader=loader,
                    header_hash=header_hash,
                    fingerprint=fingerprint,
                    recipe=review.rule_recipe,
                    revision=(max_revision or 0) + 1,
                    status=ParseRuleStatus.draft.value,
                    provenance=ParseRuleProvenance.manual.value,
                    created_from_version_id=version.id,
                    validation=review.validation,
                )
                session.add(rule)
                await session.flush()
                review.parse_rule_id = rule.id
            await apply_parse_rule_to_version(
                session, version=version, price_file=price_file, data=data, rule=rule
            )
        else:
            version.mapping = review.mapping
            await parse_and_persist(
                session, version, price_file, data, allow_rule_mutations=False
            )
            if version.status == "validated":
                loader, grids = load_source(data, price_file.original_filename)
                decision = stored_mapping_to_tables(review.mapping or {}, grids)
                if not decision.tables:
                    # A cleared/empty mapping must not produce an empty rule:
                    # fall back to the same deterministic auto-detection that
                    # parse_and_persist used to obtain the validated items.
                    decision = build_auto_mapping(grids)
                recipe = recipe_from_decision(decision, grids)
                if not recipe.get("sheets"):
                    raise ValueError(
                        "Не удалось построить правило: в файле не найдено товарных таблиц"
                    )
                fingerprint = compute_fingerprint(loader, grids)
                header_hash = compute_header_hash(fingerprint)
                max_revision = (
                    await session.execute(
                        select(func.max(ParseRule.revision)).where(
                            ParseRule.project_id == price_file.project_id,
                            ParseRule.supplier_id == version.supplier_id,
                            ParseRule.loader == loader,
                            ParseRule.header_hash == header_hash,
                        )
                    )
                ).scalar_one()
                rule = ParseRule(
                    project_id=price_file.project_id,
                    supplier_id=version.supplier_id,
                    name=review.rule_name or f"Review: {price_file.original_filename}",
                    loader=loader,
                    header_hash=header_hash,
                    fingerprint=fingerprint,
                    recipe=recipe,
                    revision=(max_revision or 0) + 1,
                    status=ParseRuleStatus.draft.value,
                    provenance=ParseRuleProvenance.manual.value,
                    created_from_version_id=version.id,
                    validation=review.validation,
                )
                session.add(rule)
                await session.flush()
                review.parse_rule_id = rule.id
                version.parse_rule_id = rule.id
        await session.commit()
        from app.import_service import run_quality_gate

        status = await run_quality_gate(
            session,
            price_import=price_import,
            version=version,
            price_file=price_file,
            data=data,
            currency_hint=review.currency_override,
            currency_source="manual",
            non_price_decisions=review.section_decisions,
        )
        await session.refresh(review)
        review.status = (
            PriceImportReviewStatus.applied.value
            if status == PriceImportStatus.completed.value
            else PriceImportReviewStatus.draft.value
        )
        review.applied_at = _now()
        review.updated_at = _now()
        session.add(
            _event(
                review,
                "apply_finished",
                review.updated_by,
                {"status": status, "item_count": version.item_count},
            )
        )
        await update_review_section_state(
            session,
            review=review,
            status=(
                PriceImportReviewSectionStatus.accepted
                if status == PriceImportStatus.completed.value
                else PriceImportReviewSectionStatus.in_progress
            ),
            actor=review.updated_by,
        )
        await session.commit()
        return status
    except Exception as exc:  # noqa: BLE001 - task must land in a documented state
        await session.rollback()
        price_import = await session.get(PriceImport, import_id)
        review = (
            await session.execute(
                select(PriceImportReview).where(PriceImportReview.price_import_id == import_id)
            )
        ).scalar_one()
        price_import.status = PriceImportStatus.failed.value
        price_import.stage = PriceImportStatus.failed.value
        price_import.progress_percent = 100
        price_import.finished_at = _now()
        price_import.error_code = "REVIEW_APPLY_FAILED"
        price_import.error_message = f"{type(exc).__name__}: {str(exc)[:700]}"
        review.status = PriceImportReviewStatus.draft.value
        session.add(
            PriceImportEvent(
                price_import_id=import_id,
                event_type="review_apply_failed",
                from_status=PriceImportStatus.reparsing.value,
                to_status=PriceImportStatus.failed.value,
                payload={"review_id": str(review.id), "revision": review.revision},
            )
        )
        await session.commit()
        return PriceImportStatus.failed.value


async def revert_apply_publication(
    session: AsyncSession,
    *,
    price_import: PriceImport,
    review: PriceImportReview,
    actor: AuthUser,
) -> None:
    """Restore the validated state when Celery publication itself fails."""

    current_review = await session.get(PriceImportReview, review.id, with_for_update=True)
    current_import = await session.get(PriceImport, price_import.id, with_for_update=True)
    if (
        current_review is None
        or current_import is None
        or current_review.status != PriceImportReviewStatus.applying.value
    ):
        return
    current_review.status = PriceImportReviewStatus.validated.value
    current_review.last_applied_revision = None
    current_review.updated_by = actor.username
    current_review.updated_at = _now()
    current_import.status = PriceImportStatus.review_required.value
    current_import.stage = PriceImportStatus.review_required.value
    current_import.progress_percent = 100
    current_import.finished_at = _now()
    current_import.error_code = "REVIEW_APPLY_QUEUE_FAILED"
    current_import.error_message = "Не удалось поставить повторный разбор в очередь"
    session.add(
        PriceImportEvent(
            price_import_id=current_import.id,
            event_type="review_apply_queue_failed",
            from_status=PriceImportStatus.reparsing.value,
            to_status=PriceImportStatus.review_required.value,
            payload={"review_id": str(current_review.id), "revision": current_review.revision},
        )
    )
    session.add(
        _event(
            current_review,
            "apply_queue_failed",
            actor.username,
            {"revision": current_review.revision},
        )
    )
    await session.commit()


async def activate_review_rule(
    session: AsyncSession,
    *,
    price_import: PriceImport,
    revision: int,
    name: str | None,
    actor: AuthUser,
) -> PriceImportReview:
    review = (
        await session.execute(
            select(PriceImportReview)
            .where(PriceImportReview.price_import_id == price_import.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if review is None or review.revision != revision:
        raise HTTPException(status_code=409, detail="Review revision изменилась")
    if not (
        price_import.status == PriceImportStatus.completed.value
        and review.status == PriceImportReviewStatus.applied.value
        and review.last_applied_revision == revision
        and review.parse_rule_id
    ):
        raise HTTPException(status_code=409, detail="Активировать можно только применённую revision")
    rule = await session.get(ParseRule, review.parse_rule_id)
    if rule is None or rule.status != ParseRuleStatus.draft.value:
        raise HTTPException(status_code=409, detail="Draft правила не найден")
    version = await session.get(PriceVersion, price_import.price_version_id)
    price_file = await session.get(PriceFile, version.price_file_id) if version else None
    if price_file is None or rule.project_id != price_file.project_id:
        raise HTTPException(status_code=409, detail="Draft правила относится к другому проекту")
    active_rules = (
        await session.execute(
            select(ParseRule).where(
                ParseRule.project_id == rule.project_id,
                ParseRule.supplier_id == rule.supplier_id,
                ParseRule.loader == rule.loader,
                ParseRule.header_hash == rule.header_hash,
                ParseRule.status == ParseRuleStatus.active.value,
                ParseRule.id != rule.id,
            )
        )
    ).scalars().all()
    for active in active_rules:
        active.status = ParseRuleStatus.deprecated.value
        active.updated_at = _now()
    if name:
        rule.name = name
    rule.status = ParseRuleStatus.active.value
    rule.provenance = ParseRuleProvenance.user_confirmed.value
    rule.updated_at = _now()
    session.add(
        _event(
            review,
            "rule_activated",
            actor.username,
            {"rule_id": str(rule.id), "rule_revision": rule.revision},
        )
    )
    await session.commit()
    return review
