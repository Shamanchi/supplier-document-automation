"""Resumable orchestration for the public price-import state machine."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.config import settings
from app.category_service import apply_category_context_gate
from app.import_telemetry import aggregate_import_telemetry
from app.llm_providers import resolve_rule_model_config
from app.models import (
    AttributeDefinition,
    ParseRule,
    PriceFile,
    PriceImport,
    PriceImportEvent,
    PriceImportStatus,
    PriceImportWebhookDelivery,
    PriceVersion,
    RuleGenerationAttempt,
    ServiceClient,
    SupplierPriceItem,
    SupplierPriceValue,
    VersionStatus,
)
from app.parse_service import finalize_materialized_version_with_rule, parse_and_persist
from app.parsing import loaders
from app.reconciliation import apply_money_gate, reconcile_version
from app.rule_proposal_service import (
    ATTEMPT_CATALOG_WITHOUT_PRICE,
    ATTEMPT_DRAFT_CREATED,
    ATTEMPT_GENERATING,
    ATTEMPT_PROPOSED,
    ATTEMPT_QUEUED,
    ATTEMPT_SKIPPED_NON_PRICE,
    TABULAR_PROPOSAL_LOADERS,
    ProposalError,
    batch_prompt_version,
    build_attempt_specs,
    create_model_draft_from_batch,
    materialize_succeeded_sections,
    serialise_attribute_catalog,
)


PROGRESS = {
    "queued": 0,
    "parsing": 10,
    "generating_rule": 35,
    "compiling_rule": 65,
    "reparsing": 75,
    "quality_check": 90,
    "completed": 100,
    "review_required": 100,
    "needs_ocr": 100,
    "failed": 100,
}


def rule_generation_task_name() -> str:
    """Return the rollout-selected delivery path without publishing work."""

    return (
        "app.tasks.generate_rule_proposal_section"
        if settings.durable_rule_generation_enabled
        else "app.tasks.generate_rule_proposal_batch"
    )


class LlmProviderNotConfigured(ProposalError):
    """A required model mapping cannot start without a server-side credential."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def transition(
    price_import: PriceImport,
    status: str,
    *,
    event_type: str = "status_changed",
    payload: dict | None = None,
) -> PriceImportEvent:
    previous = price_import.status
    price_import.status = status
    price_import.stage = status
    price_import.progress_percent = PROGRESS[status]
    price_import.updated_at = _now()
    if price_import.started_at is None and status != PriceImportStatus.queued.value:
        price_import.started_at = _now()
    if status in PriceImportStatus.terminal_values():
        price_import.finished_at = _now()
    return PriceImportEvent(
        price_import_id=price_import.id,
        event_type=event_type,
        from_status=previous,
        to_status=status,
        payload=payload or {},
    )


async def _terminal(
    session: AsyncSession,
    price_import: PriceImport,
    status: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
) -> str:
    price_import.error_code = error_code
    price_import.error_message = error_message
    session.add(transition(price_import, status))
    if price_import.callback_url:
        existing = (
            await session.execute(
                select(PriceImportWebhookDelivery).where(
                    PriceImportWebhookDelivery.price_import_id == price_import.id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                PriceImportWebhookDelivery(
                    price_import_id=price_import.id,
                    callback_url=price_import.callback_url,
                )
            )
    await session.commit()
    return status


async def _create_attempts(
    session: AsyncSession,
    *,
    price_import: PriceImport,
    version: PriceVersion,
    price_file: PriceFile,
    data: bytes,
) -> list[RuleGenerationAttempt]:
    request_config = resolve_rule_model_config(
        provider=price_import.provider,
        model=price_import.model,
    )
    if not request_config.api_key and not settings.rule_generation_stub_mode:
        raise LlmProviderNotConfigured(
            f"API-ключ провайдера {request_config.provider} не задан; "
            "настройте LLM или выполните разметку вручную"
        )
    loader = loaders.detect_loader(data, price_file.original_filename)
    if loader not in TABULAR_PROPOSAL_LOADERS:
        raise ProposalError(f"Loader {loader} не поддерживает безопасную генерацию правил")
    grids = loaders.load_grids_for(loader, data, price_file.original_filename)
    definitions = (
        await session.execute(
            select(AttributeDefinition)
            .where(AttributeDefinition.status == "active")
            .order_by(AttributeDefinition.code)
            .limit(256)
        )
    ).scalars().all()
    specs = build_attempt_specs(
        grids=grids,
        loader=loader,
        model=request_config.model,
        attribute_catalog=serialise_attribute_catalog(list(definitions)),
    )
    if not specs:
        raise ProposalError("Extractor не нашёл секции для анализа")
    if len(specs) > settings.openai_rule_max_sections_per_operation:
        raise ProposalError(
            "Число секций превышает серверный лимит одной операции; "
            "импорт передан на проверку без частичного результата"
        )
    batch_id = uuid.uuid4()
    attempts = [
        RuleGenerationAttempt(
            price_version_id=version.id,
            price_import_id=price_import.id,
            batch_id=batch_id,
            section_id=spec["section_id"],
            status=ATTEMPT_QUEUED,
            provider=request_config.provider,
            model=request_config.model,
            request_settings={
                **request_config.persisted_settings(),
                "section_diagnostics": spec["diagnostics"],
            },
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
    price_import.rule_batch_id = batch_id
    price_import.provider = request_config.provider
    price_import.model = request_config.model
    price_import.prompt_version = batch_prompt_version(specs)
    await session.commit()
    return attempts


async def _usage_within_budget(
    session: AsyncSession,
    *,
    price_import: PriceImport,
    client: ServiceClient,
) -> bool:
    price_import.telemetry_summary = await aggregate_import_telemetry(
        session, price_import.id
    )
    await session.commit()
    return (
        int(price_import.telemetry_summary["input_tokens"])
        < client.max_input_tokens_per_import
        and int(price_import.telemetry_summary["output_tokens"])
        < client.max_output_tokens_per_import
    )


async def _quality_gate(
    session: AsyncSession,
    *,
    price_import: PriceImport,
    version: PriceVersion,
    price_file: PriceFile,
    data: bytes,
    currency_hint: str | None = None,
    currency_source: str = "request_hint",
    non_price_decisions: list[dict] | None = None,
) -> str:
    # Проверка: НДС обязателен для перехода в validated
    if price_import.vat_rate_percent is None:
        return await _terminal(
            session,
            price_import,
            PriceImportStatus.review_required.value,
            error_code="VAT_RATE_REQUIRED",
            error_message="Невозможно завершить импорт: не указана ставка НДС",
        )

    session.add(transition(price_import, PriceImportStatus.quality_check.value))
    await session.commit()
    money = await apply_money_gate(
        session,
        version=version,
        currency_hint=currency_hint if currency_hint is not None else price_import.currency_hint,
        currency_source=currency_source,
    )
    completeness = await reconcile_version(
        session,
        version=version,
        data=data,
        filename=price_file.original_filename,
        non_price_decisions=non_price_decisions,
    )
    category_context = await apply_category_context_gate(session, version=version)
    price_import.completeness_summary = {
        **completeness,
        "money_gate": money,
        "category_context_gate": category_context,
        "final_validation_complete": True,
    }
    price_import.telemetry_summary = await aggregate_import_telemetry(session, price_import.id)
    price_import.loader = version.loader
    price_import.parse_rule_id = version.parse_rule_id
    if version.parse_rule_id:
        rule = await session.get(ParseRule, version.parse_rule_id)
        if rule is not None:
            price_import.parse_rule_revision = rule.revision
    quality_failed = (
        bool(money["failures"])
        or not completeness["passed"]
        or not category_context["passed"]
        or version.errors_count > 0
    )
    if quality_failed and version.status == VersionStatus.validated.value:
        version.status = VersionStatus.validation_failed.value
    await session.commit()
    if quality_failed:
        return await _terminal(
            session,
            price_import,
            PriceImportStatus.review_required.value,
            error_code="QUALITY_REVIEW_REQUIRED",
            error_message="Денежный или completeness gate требует проверки",
        )
    return await _terminal(session, price_import, PriceImportStatus.completed.value)


async def run_quality_gate(
    session: AsyncSession,
    *,
    price_import: PriceImport,
    version: PriceVersion,
    price_file: PriceFile,
    data: bytes,
    currency_hint: str | None = None,
    currency_source: str = "request_hint",
    non_price_decisions: list[dict] | None = None,
) -> str:
    """Public wrapper used by the initial pipeline and a reviewed reparse."""

    return await _quality_gate(
        session,
        price_import=price_import,
        version=version,
        price_file=price_file,
        data=data,
        currency_hint=currency_hint,
        currency_source=currency_source,
        non_price_decisions=non_price_decisions,
    )


async def backfill_vat_rate_percent(
    session: AsyncSession,
    *,
    price_version_id: uuid.UUID,
    vat_rate_percent: Decimal | None,
) -> int:
    """Carry the operator-chosen VAT rate into already-persisted prices.

    SupplierPriceValue rows are materialised before the operator picks the
    import rate (the VAT_RATE_REQUIRED gate), so values created in that
    window keep ``vat_rate_percent`` NULL.  The PATCH handler runs this
    backfill in the same transaction that records the operator's choice,
    so existing prices start carrying the rate before the workflow
    resumes.  Only missing rates are filled; explicit parser-level rates
    are preserved, which also makes repeated PATCH calls idempotent.
    Values materialised later already take the current import rate at
    creation time.
    """
    if vat_rate_percent is None:
        return 0
    result = await session.execute(
        update(SupplierPriceValue)
        .where(
            SupplierPriceValue.vat_rate_percent.is_(None),
            SupplierPriceValue.item_id.in_(
                select(SupplierPriceItem.id).where(
                    SupplierPriceItem.price_version_id == price_version_id
                )
            ),
        )
        .values(vat_rate_percent=vat_rate_percent)
    )
    return result.rowcount or 0

async def advance_price_import(session: AsyncSession, import_id: uuid.UUID) -> str:
    """Advance one import under a process-independent PostgreSQL lock.

    The lock lives on a dedicated connection, so intermediate commits made by
    parsing and model-attempt services cannot release it.  A duplicate Celery
    delivery returns immediately and the active delivery remains authoritative.
    """

    bind = session.bind
    if bind is None or bind.dialect.name != "postgresql":
        return await _advance_price_import_locked(session, import_id)
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
            return await _advance_price_import_locked(session, import_id)
        finally:
            await lock_connection.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": lock_key}
            )
            await lock_connection.commit()


async def _advance_price_import_locked(session: AsyncSession, import_id: uuid.UUID) -> str:
    price_import = (
        await session.execute(
            select(PriceImport).where(PriceImport.id == import_id).with_for_update()
        )
    ).scalar_one_or_none()
    if price_import is None:
        return "not_found"
    terminal_review_recovery = (
        price_import.status == PriceImportStatus.review_required.value
    )
    if (
        price_import.status in PriceImportStatus.terminal_values()
        and not terminal_review_recovery
    ):
        return price_import.status
    version = await session.get(PriceVersion, price_import.price_version_id)
    price_file = await session.get(PriceFile, price_import.price_file_id)
    client = await session.get(ServiceClient, price_import.service_client_id)
    if version is None or price_file is None or client is None:
        return await _terminal(
            session,
            price_import,
            PriceImportStatus.failed.value,
            error_code="IMPORT_DEPENDENCY_MISSING",
            error_message="Связанные данные импорта не найдены",
        )
    data = storage.get_price_file(price_file.storage_key)
    try:
        if (
            price_import.status == PriceImportStatus.quality_check.value
            and version.status == VersionStatus.validated.value
        ):
            # VAT-resume (P0-3): the PATCH handler records the operator rate,
            # backfills SupplierPriceValue rows and moves a review_required
            # VAT_RATE_REQUIRED import to quality_check.  The version was
            # already parsed and validated, so only the remaining quality
            # gates need to run; starting rule-generation attempts here would
            # pay for LLM calls even when a deterministic saved rule matched.
            return await _quality_gate(
                session,
                price_import=price_import,
                version=version,
                price_file=price_file,
                data=data,
            )
        if price_import.status in {PriceImportStatus.queued.value, PriceImportStatus.parsing.value}:
            if price_import.status == PriceImportStatus.queued.value:
                session.add(transition(price_import, PriceImportStatus.parsing.value))
                await session.commit()
            if version.status in {VersionStatus.uploaded.value, VersionStatus.parsing.value}:
                version.status = VersionStatus.parsing.value
                version.error_message = None
                await session.commit()
                await parse_and_persist(
                    session,
                    version,
                    price_file,
                    data,
                    vat_rate_percent=price_import.vat_rate_percent,
                )
                await session.commit()
            if version.status == VersionStatus.needs_ocr.value:
                return await _terminal(session, price_import, PriceImportStatus.needs_ocr.value)
            if version.status == VersionStatus.validated.value:
                return await _quality_gate(
                    session,
                    price_import=price_import,
                    version=version,
                    price_file=price_file,
                    data=data,
                )
            if version.status not in {VersionStatus.needs_mapping.value}:
                terminal = (
                    PriceImportStatus.review_required.value
                    if version.status in {
                        VersionStatus.complex.value,
                        VersionStatus.validation_failed.value,
                    }
                    else PriceImportStatus.failed.value
                )
                return await _terminal(
                    session,
                    price_import,
                    terminal,
                    error_code=f"VERSION_{version.status.upper()}",
                    error_message=version.error_message,
                )
            if price_import.status != PriceImportStatus.generating_rule.value:
                session.add(transition(price_import, PriceImportStatus.generating_rule.value))
                await session.commit()

        attempts = (
            await session.execute(
                select(RuleGenerationAttempt)
                .where(RuleGenerationAttempt.price_import_id == price_import.id)
                .order_by(RuleGenerationAttempt.created_at, RuleGenerationAttempt.id)
            )
        ).scalars().all()
        if not attempts:
            if terminal_review_recovery:
                return price_import.status
            attempts = await _create_attempts(
                session,
                price_import=price_import,
                version=version,
                price_file=price_file,
                data=data,
            )
        partial = await materialize_succeeded_sections(
            session,
            version=version,
            price_file=price_file,
            data=data,
            attempts=attempts,
        )
        price_import.completeness_summary = {
            **(price_import.completeness_summary or {}),
            "partial_materialization": partial,
            "final_validation_complete": False,
        }
        await session.commit()
        if terminal_review_recovery:
            return price_import.status
        if not await _usage_within_budget(
            session, price_import=price_import, client=client
        ):
            return await _terminal(
                session,
                price_import,
                PriceImportStatus.review_required.value,
                error_code="LLM_BUDGET_EXCEEDED",
                error_message="Исчерпан лимит LLM-токенов импорта",
            )
        pending = [
            attempt
            for attempt in attempts
            if attempt.status
            in {
                ATTEMPT_QUEUED,
                ATTEMPT_GENERATING,
                "leased",
                "retryable_failed",
            }
        ]
        if pending:
            if price_import.attempt_no == 0:
                price_import.attempt_no = 1
                price_import.updated_at = _now()
                await session.commit()
                if settings.durable_rule_generation_enabled:
                    from app.tasks import generate_rule_proposal_section

                    # Immediate publication is a fast path. PostgreSQL remains
                    # authoritative if broker publication is lost.
                    for attempt in pending:
                        if attempt.status == ATTEMPT_QUEUED:
                            try:
                                generate_rule_proposal_section.delay(str(attempt.id))
                            except Exception:  # noqa: BLE001
                                pass
                else:
                    from app.tasks import generate_rule_proposal_batch

                    generate_rule_proposal_batch.delay(
                        str(price_import.rule_batch_id),
                        settings.openai_rule_batch_page_size,
                        0,
                    )
            return PriceImportStatus.generating_rule.value
        allowed = {
            ATTEMPT_PROPOSED,
            ATTEMPT_CATALOG_WITHOUT_PRICE,
            ATTEMPT_SKIPPED_NON_PRICE,
            ATTEMPT_DRAFT_CREATED,
            "succeeded",
        }
        if any(attempt.status not in allowed for attempt in attempts):
            return await _terminal(
                session,
                price_import,
                PriceImportStatus.review_required.value,
                error_code="MODEL_PROPOSAL_REJECTED",
                error_message="Не все секции прошли source compiler и dry-run",
            )
        if price_import.status != PriceImportStatus.compiling_rule.value:
            session.add(transition(price_import, PriceImportStatus.compiling_rule.value))
            await session.commit()
        rule = await create_model_draft_from_batch(
            session,
            version_id=version.id,
            batch_id=price_import.rule_batch_id,
        )
        await session.commit()
        if price_import.status != PriceImportStatus.reparsing.value:
            session.add(transition(price_import, PriceImportStatus.reparsing.value))
            await session.commit()
        await finalize_materialized_version_with_rule(
            session,
            version=version,
            price_file=price_file,
            rule=rule,
        )
        await session.commit()
        return await _quality_gate(
            session,
            price_import=price_import,
            version=version,
            price_file=price_file,
            data=data,
        )
    except LlmProviderNotConfigured as exc:
        await session.rollback()
        price_import = await session.get(PriceImport, import_id)
        return await _terminal(
            session,
            price_import,
            PriceImportStatus.review_required.value,
            error_code="LLM_PROVIDER_NOT_CONFIGURED",
            error_message=str(exc)[:800],
        )
    except ProposalError as exc:
        await session.rollback()
        price_import = await session.get(PriceImport, import_id)
        return await _terminal(
            session,
            price_import,
            PriceImportStatus.review_required.value,
            error_code="RULE_COMPILER_REJECTED",
            error_message=str(exc)[:800],
        )
    except Exception as exc:  # noqa: BLE001 - public jobs must become terminal
        await session.rollback()
        price_import = await session.get(PriceImport, import_id)
        return await _terminal(
            session,
            price_import,
            PriceImportStatus.failed.value,
            error_code="IMPORT_EXECUTION_FAILED",
            error_message=f"{type(exc).__name__}: {str(exc)[:700]}",
        )
