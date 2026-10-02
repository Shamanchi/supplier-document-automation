"""Review API for server-side LLM section mapping proposals."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.auth import AuthUser, require_admin
from app.config import settings
from app.db import get_session
from app.llm_providers import provider_options, resolve_rule_model_config
from app.models import (
    AttributeDefinition,
    PriceFile,
    PriceVersion,
    RuleGenerationAttempt,
    VersionStatus,
)
from app.parsing import loaders
from app.parsing.rule_proposal import ProposalError
from app.rule_proposal_service import (
    ATTEMPT_QUEUED,
    ATTEMPT_FAILED,
    ATTEMPT_GENERATING,
    TABULAR_PROPOSAL_LOADERS,
    batch_prompt_version,
    build_attempt_specs,
    create_model_draft_from_batch,
    serialise_attribute_catalog,
)
from app.rule_proposal_batching import page_summary
from app.schemas import (
    ParseRuleOut,
    RuleProposalApplyOut,
    RuleProposalAttemptOut,
    RuleProposalBatchOut,
    RuleDraftProgressOut,
    RuleModelOptionsOut,
    RuleProposalStartIn,
    RuleSectionRetryIn,
    RuleSectionRetryOut,
)
from app.rule_generation_status_service import (
    RetryImportStateError,
    RetryScopeError,
    RetryUnavailableError,
    retry_rule_sections,
)


router = APIRouter(tags=["rule-proposals"])

RULE_PROPOSAL_REVIEWABLE_STATUSES = {
    VersionStatus.needs_mapping.value,
    VersionStatus.validated.value,
    VersionStatus.active.value,
    VersionStatus.superseded.value,
}


async def _store_draft_progress(
    session: AsyncSession,
    *,
    version_id: uuid.UUID,
    batch_id: uuid.UUID,
    status: str,
    phase: str,
    percent: int,
    completed_sections: int,
    total_sections: int,
    message: str | None = None,
    parse_rule_id: uuid.UUID | None = None,
    error_message: str | None = None,
) -> dict:
    version = await session.get(PriceVersion, version_id)
    if version is None:
        raise ProposalError("Версия прайса не найдена")
    now = datetime.now(timezone.utc)
    current = version.rule_generation if isinstance(version.rule_generation, dict) else {}
    previous = current.get("draft_creation") if isinstance(current.get("draft_creation"), dict) else {}
    progress = {
        "batch_id": str(batch_id),
        "status": status,
        "phase": phase,
        "percent": max(0, min(100, percent)),
        "completed_sections": completed_sections,
        "total_sections": total_sections,
        "message": message,
        "parse_rule_id": str(parse_rule_id) if parse_rule_id else None,
        "error_message": error_message,
        "started_at": previous.get("started_at") or now.isoformat(),
        "updated_at": now.isoformat(),
    }
    version.rule_generation = {**current, "draft_creation": progress}
    await session.commit()
    return progress


def _draft_progress_out(raw: dict | None) -> RuleDraftProgressOut:
    if not isinstance(raw, dict):
        return RuleDraftProgressOut(
            status="idle", phase="idle", percent=0,
            completed_sections=0, total_sections=0,
        )
    return RuleDraftProgressOut.model_validate(raw)


async def _version_and_file(
    session: AsyncSession, version_id: uuid.UUID, *, lock: bool = False
) -> tuple[PriceVersion, PriceFile]:
    stmt = select(PriceVersion).where(PriceVersion.id == version_id)
    if lock:
        # Serialises paid batch starts and draft creation against the same
        # version on PostgreSQL. SQLite test runs safely ignore FOR UPDATE.
        stmt = stmt.with_for_update()
    version = (await session.execute(stmt)).scalar_one_or_none()
    if version is None:
        raise HTTPException(status_code=404, detail="Версия прайса не найдена")
    price_file = await session.get(PriceFile, version.price_file_id)
    if price_file is None:
        raise HTTPException(status_code=404, detail="Оригинал прайса не найден")
    return version, price_file


@router.get(
    "/price-versions/{version_id}/rule-proposals",
    response_model=list[RuleProposalAttemptOut],
)
async def list_rule_proposals(
    version_id: uuid.UUID,
    batch_id: uuid.UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> list[RuleProposalAttemptOut]:
    await _version_and_file(session, version_id)
    stmt = (
        select(RuleGenerationAttempt)
        .where(RuleGenerationAttempt.price_version_id == version_id)
        .order_by(RuleGenerationAttempt.created_at.desc(), RuleGenerationAttempt.section_id)
    )
    if batch_id is not None:
        stmt = stmt.where(RuleGenerationAttempt.batch_id == batch_id)
    attempts = (await session.execute(stmt)).scalars().all()
    return [RuleProposalAttemptOut.model_validate(attempt) for attempt in attempts]


@router.get(
    "/price-versions/{version_id}/rule-proposals/{batch_id}/draft-progress",
    response_model=RuleDraftProgressOut,
)
async def get_rule_draft_progress(
    version_id: uuid.UUID,
    batch_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> RuleDraftProgressOut:
    version, _ = await _version_and_file(session, version_id)
    generation = version.rule_generation if isinstance(version.rule_generation, dict) else {}
    raw = generation.get("draft_creation")
    if not isinstance(raw, dict) or raw.get("batch_id") != str(batch_id):
        return _draft_progress_out(None)
    return _draft_progress_out(raw)


@router.post(
    "/price-versions/{version_id}/rule-proposals/{batch_id}/retry",
    response_model=RuleSectionRetryOut,
)
async def retry_rule_proposal_sections(
    version_id: uuid.UUID,
    batch_id: uuid.UUID,
    payload: RuleSectionRetryIn,
    admin: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> RuleSectionRetryOut:
    await _version_and_file(session, version_id)
    try:
        return await retry_rule_sections(
            session,
            price_version_id=version_id,
            batch_id=batch_id,
            section_ids=payload.section_ids,
            retryable_only=payload.retryable_only,
            actor=admin.username,
        )
    except RetryUnavailableError as exc:
        raise HTTPException(status_code=404, detail="DURABLE_RULE_GENERATION_DISABLED") from exc
    except RetryScopeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RetryImportStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/price-versions/{version_id}/rule-proposals",
    response_model=RuleProposalBatchOut,
    status_code=202,
)
async def start_rule_proposals(
    version_id: uuid.UUID,
    payload: RuleProposalStartIn | None = None,
    session: AsyncSession = Depends(get_session),
) -> RuleProposalBatchOut:
    """Queue one independent model call for every header/table section.

    This endpoint intentionally creates artifacts only. It does not overwrite
    a version's mapping, parsed items, or active rule, even if a model answer
    looks confident.
    """

    # The version row is the per-file lock: two simultaneous POSTs cannot both
    # see "no active batch" and spend the model budget.
    version, price_file = await _version_and_file(session, version_id, lock=True)
    if version.status not in RULE_PROPOSAL_REVIEWABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                "Для этой версии сейчас нельзя генерировать правило: дождитесь окончания "
                "загрузки/разбора или выберите сохранённую версию прайса"
            ),
        )
    choice = payload or RuleProposalStartIn()
    try:
        request_config = resolve_rule_model_config(
            provider=choice.provider,
            model=choice.model,
            temperature=choice.temperature,
            top_p=choice.top_p,
            max_output_tokens=choice.max_output_tokens,
            timeout_seconds=choice.timeout_seconds,
            thinking=choice.thinking,
            reasoning_effort=choice.reasoning_effort,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not request_config.api_key:
        raise HTTPException(
            status_code=503,
            detail=(
                f"API-ключ провайдера {request_config.provider} не задан: "
                "генерация правил через него отключена"
            ),
        )
    existing_batch = (
        await session.execute(
            select(RuleGenerationAttempt.batch_id)
            .where(
                RuleGenerationAttempt.price_version_id == version.id,
                RuleGenerationAttempt.status.in_(
                    [
                        ATTEMPT_QUEUED,
                        ATTEMPT_GENERATING,
                        "leased",
                        "retryable_failed",
                    ]
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing_batch is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Для этой версии уже выполняется batch {existing_batch}",
        )
    last_started_at = (
        await session.execute(
            select(RuleGenerationAttempt.created_at)
            .where(RuleGenerationAttempt.price_version_id == version.id)
            .order_by(RuleGenerationAttempt.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last_started_at is not None:
        if last_started_at.tzinfo is None:
            last_started_at = last_started_at.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - last_started_at).total_seconds()
        if age_seconds < settings.openai_rule_start_cooldown_seconds:
            wait_seconds = max(1, int(settings.openai_rule_start_cooldown_seconds - age_seconds))
            raise HTTPException(
                status_code=429,
                detail=(
                    "Повторный LLM-batch для этой версии временно ограничен; "
                    f"повторите через {wait_seconds} с."
                ),
            )
    data = storage.get_price_file(price_file.storage_key)
    try:
        loader = loaders.detect_loader(data, price_file.original_filename)
    except loaders.UnknownFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if loader not in TABULAR_PROPOSAL_LOADERS:
        raise HTTPException(
            status_code=409,
            detail=(
                "Для этого формата действует отдельный layout/OCR-контур; "
                "LLM section mapping безопасно поддержан только для табличных файлов."
            ),
        )
    try:
        grids = loaders.load_grids_for(loader, data, price_file.original_filename)
    except (loaders.DocumentConversionError, loaders.PdfNoTablesError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    canonical_definitions = (
        (
            await session.execute(
                select(AttributeDefinition)
                .where(AttributeDefinition.status == "active")
                .order_by(AttributeDefinition.code)
                .limit(256)
            )
        )
        .scalars()
        .all()
    )
    attribute_catalog = serialise_attribute_catalog(list(canonical_definitions))
    specs = build_attempt_specs(
        grids=grids,
        loader=loader,
        model=request_config.model,
        attribute_catalog=attribute_catalog,
    )
    if not specs:
        raise HTTPException(status_code=409, detail="Не найдено ни одной секции для анализа")
    if len(specs) > settings.openai_rule_max_sections_per_operation:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Найдено секций: {len(specs)}; защитный предел операции: "
                f"{settings.openai_rule_max_sections_per_operation}."
            ),
        )

    batch_id = uuid.uuid4()
    attempts = [
        RuleGenerationAttempt(
            price_version_id=version.id,
            batch_id=batch_id,
            section_id=spec["section_id"],
            status=ATTEMPT_QUEUED,
            provider=request_config.provider,
            model=request_config.model,
            request_settings=request_config.persisted_settings(),
            prompt_version=spec["prompt_version"],
            prompt_id=spec["prompt_id"],
            source_file_sha256=price_file.sha256,
            source_context_hash=spec["source_context_hash"],
            input_context=spec["input_context"],
            prompt=spec["prompt"],
        )
        for spec in specs
    ]
    session.add_all(attempts)
    # Retain a compact compatibility card on the existing version DTO. The
    # detailed prompt/proposal/audit trail lives in the new attempt rows.
    version.rule_generation = {
        "provider": request_config.provider,
        "model": request_config.model,
        "request_settings": request_config.persisted_settings(),
        "prompt_version": batch_prompt_version(specs),
        "prompt_id": attempts[0].prompt_id,
        "prompt": attempts[0].prompt,
        "status": "queued",
        "batch_id": str(batch_id),
        "sections_count": len(attempts),
        "requires_manual_review": True,
    }
    await session.commit()

    job_ids: list[str] = []
    if settings.durable_rule_generation_enabled:
        from app.tasks import generate_rule_proposal_section

        for attempt in attempts:
            try:
                job_ids.append(generate_rule_proposal_section.delay(str(attempt.id)).id)
            except Exception:  # noqa: BLE001 - dispatcher recovers DB work
                continue
        return RuleProposalBatchOut(
            batch_id=batch_id,
            attempts=[RuleProposalAttemptOut.model_validate(attempt) for attempt in attempts],
            job_ids=job_ids,
            **page_summary(
                total_sections=len(attempts),
                page_size=settings.openai_rule_batch_page_size,
            ),
        )

    from app.tasks import generate_rule_proposal_batch

    try:
        job_ids.append(
            generate_rule_proposal_batch.delay(
                str(batch_id), settings.openai_rule_batch_page_size
            ).id
        )
    except Exception as exc:  # noqa: BLE001 - do not leave invisible queued work
        message = f"Не удалось поставить LLM-задачи в очередь: {type(exc).__name__}"
        await session.execute(
            update(RuleGenerationAttempt)
            .where(
                RuleGenerationAttempt.id.in_([attempt.id for attempt in attempts]),
                RuleGenerationAttempt.status == ATTEMPT_QUEUED,
            )
            .values(status=ATTEMPT_FAILED, error_message=message)
        )
        version.rule_generation = {**(version.rule_generation or {}), "status": "queue_failed"}
        await session.commit()
        raise HTTPException(status_code=503, detail=message) from exc
    pagination = page_summary(
        total_sections=len(attempts), page_size=settings.openai_rule_batch_page_size
    )
    return RuleProposalBatchOut(
        batch_id=batch_id,
        attempts=[RuleProposalAttemptOut.model_validate(attempt) for attempt in attempts],
        job_ids=job_ids,
        **pagination,
    )


@router.post(
    "/price-versions/{version_id}/rule-proposals/{batch_id}/create-draft",
    response_model=ParseRuleOut,
)
async def create_rule_draft_from_proposals(
    version_id: uuid.UUID,
    batch_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ParseRuleOut:
    """Explicit human action: combine verified section proposals into a draft.

    The draft remains excluded from automatic matching and is never activated
    here. Existing normal rule validation/activation remains the second human
    checkpoint.
    """

    attempt_ids = list((await session.execute(
        select(RuleGenerationAttempt.id).where(
            RuleGenerationAttempt.price_version_id == version_id,
            RuleGenerationAttempt.batch_id == batch_id,
        )
    )).scalars())
    total_sections = len(attempt_ids)
    await _store_draft_progress(
        session, version_id=version_id, batch_id=batch_id,
        status="running", phase="queued", percent=1,
        completed_sections=0, total_sections=total_sections,
        message="Начинаем сборку проверенного черновика",
    )

    async def report(phase: str, completed: int, total: int, percent: int, message: str) -> None:
        await _store_draft_progress(
            session, version_id=version_id, batch_id=batch_id,
            status="running", phase=phase, percent=percent,
            completed_sections=completed, total_sections=total, message=message,
        )

    try:
        rule = await create_model_draft_from_batch(
            session, version_id=version_id, batch_id=batch_id, progress=report
        )
        await session.commit()
    except (ProposalError, ValueError) as exc:
        await session.rollback()
        await _store_draft_progress(
            session, version_id=version_id, batch_id=batch_id,
            status="failed", phase="failed", percent=100,
            completed_sections=0, total_sections=total_sections,
            message="Черновик не создан", error_message=str(exc),
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        await session.rollback()
        await _store_draft_progress(
            session, version_id=version_id, batch_id=batch_id,
            status="failed", phase="failed", percent=100,
            completed_sections=0, total_sections=total_sections,
            message="Черновик не создан", error_message=type(exc).__name__,
        )
        raise
    await _store_draft_progress(
        session, version_id=version_id, batch_id=batch_id,
        status="completed", phase="completed", percent=100,
        completed_sections=total_sections, total_sections=total_sections,
        message="Черновик создан и проверен", parse_rule_id=rule.id,
    )
    return ParseRuleOut.model_validate(rule)


@router.post(
    "/price-versions/{version_id}/rule-proposals/{batch_id}/create-draft-and-apply",
    response_model=RuleProposalApplyOut,
    status_code=202,
)
async def create_and_apply_rule_draft_from_proposals(
    version_id: uuid.UUID,
    batch_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> RuleProposalApplyOut:
    """Human-approved action: create the draft and reparse its source version."""

    version, _ = await _version_and_file(session, version_id, lock=True)
    try:
        rule = await create_model_draft_from_batch(
            session, version_id=version_id, batch_id=batch_id
        )
        version.status = VersionStatus.parsing.value
        await session.commit()
    except (ProposalError, ValueError) as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    from app.tasks import apply_model_rule_to_version

    try:
        job = apply_model_rule_to_version.delay(str(version_id), str(rule.id))
    except Exception as exc:  # noqa: BLE001 - leave a visible recoverable state
        version = await session.get(PriceVersion, version_id)
        if version is not None:
            version.status = VersionStatus.needs_mapping.value
            await session.commit()
        raise HTTPException(status_code=503, detail="Не удалось поставить применение в очередь") from exc
    return RuleProposalApplyOut(rule=ParseRuleOut.model_validate(rule), job_id=job.id)
