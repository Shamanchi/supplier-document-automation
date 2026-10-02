"""Celery tasks: parse a price version; run a corpus directory."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app import storage
from app.config import settings
from app.corpus_service import run_corpus
from app.db import make_task_engine
from app.models import (
    PriceDocumentType,
    PriceFile,
    PriceImport,
    PriceImportStatus,
    PriceImportWebhookDelivery,
    PriceVersion,
    RuleGenerationAttempt,
    RuleGenerationOperation,
    ServiceClient,
    VersionStatus,
)
from app.matching.canonical import CanonicalService
from app.matching.embeddings import EmbeddingIndexService
from app.matching.variants import SEARCHABLE_STATUSES
from app.parse_service import apply_parse_rule_to_version, parse_and_persist
from app.rule_proposal_service import run_rule_generation_attempt
from app.rule_generation_lifecycle import (
    ATTEMPT_SUCCEEDED,
    claim_attempt,
    heartbeat_attempt,
    inspect_batch,
    mark_section_succeeded,
    record_section_failure,
    recover_stale_attempts,
    reserve_dispatchable_attempts,
)
from app.rule_proposal_batching import continuation_page, page_summary
from app.worker import celery_app


@celery_app.task(
    name="app.tasks.run_price_import",
    acks_late=True,
    reject_on_worker_lost=True,
)
def run_price_import(import_id: str) -> str:
    result = asyncio.run(_run_price_import(import_id))
    delivery_id = result.get("delivery_id")
    if delivery_id:
        deliver_import_webhook.delay(delivery_id)
    return str(result["status"])


@celery_app.task(
    name="app.tasks.advance_price_import_task",
    acks_late=True,
    reject_on_worker_lost=True,
)
def advance_price_import_task(import_id: str) -> str:
    """Resume import after VAT rate has been set via PATCH."""
    return asyncio.run(_advance_price_import(import_id))


async def _advance_price_import(import_id: str) -> str:
    from app.import_service import advance_price_import

    engine, session_factory = make_task_engine()
    try:
        async with session_factory() as session:
            return await advance_price_import(session, uuid.UUID(import_id))
    finally:
        await engine.dispose()


@celery_app.task(
    name="app.tasks.apply_price_import_review",
    acks_late=True,
    reject_on_worker_lost=True,
)
def apply_price_import_review(import_id: str) -> str:
    return asyncio.run(_apply_price_import_review(import_id))


async def _apply_price_import_review(import_id: str) -> str:
    from app.review_service import apply_review_revision

    engine, session_factory = make_task_engine()
    try:
        async with session_factory() as session:
            return await apply_review_revision(session, uuid.UUID(import_id))
    finally:
        await engine.dispose()


async def _run_price_import(import_id: str) -> dict[str, str | None]:
    from app.import_service import advance_price_import

    engine, session_factory = make_task_engine()
    try:
        async with session_factory() as session:
            parsed_id = uuid.UUID(import_id)
            status = await advance_price_import(session, parsed_id)
            delivery_id = (
                await session.execute(
                    select(PriceImportWebhookDelivery.id).where(
                        PriceImportWebhookDelivery.price_import_id == parsed_id,
                        PriceImportWebhookDelivery.status == "queued",
                    )
                )
            ).scalar_one_or_none()
            return {
                "status": status,
                "delivery_id": str(delivery_id) if delivery_id else None,
            }
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.deliver_import_webhook", bind=True, max_retries=4)
def deliver_import_webhook(self, delivery_id: str) -> str:
    delivered, retryable = asyncio.run(_deliver_import_webhook(delivery_id))
    if retryable:
        raise self.retry(countdown=2 ** (self.request.retries + 1))
    return "delivered" if delivered else "failed"


async def _deliver_import_webhook(delivery_id: str) -> tuple[bool, bool]:
    from app.webhook_service import deliver_webhook_once

    engine, session_factory = make_task_engine()
    try:
        async with session_factory() as session:
            return await deliver_webhook_once(session, uuid.UUID(delivery_id))
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.parse_price_version")
def parse_price_version(version_id: str) -> str:
    return asyncio.run(_parse_price_version(version_id))


async def _parse_price_version(version_id: str) -> str:
    engine, session_factory = make_task_engine()
    try:
        async with session_factory() as session:
            version = await session.get(PriceVersion, uuid.UUID(version_id))
            if version is None:
                return "not_found"
            price_file = await session.get(PriceFile, version.price_file_id)

            version.status = VersionStatus.parsing.value
            version.error_message = None
            await session.commit()

            try:
                data = storage.get_price_file(price_file.storage_key)
                await parse_and_persist(session, version, price_file, data)
            except Exception as exc:  # noqa: BLE001 — any parse crash must land in the version status
                await session.rollback()
                version = await session.get(PriceVersion, uuid.UUID(version_id))
                version.status = VersionStatus.failed.value
                version.error_message = f"{type(exc).__name__}: {exc}"
                await session.commit()
                return VersionStatus.failed.value
            await session.commit()
            # Прайс перечитан: если версия уже была в индексе подбора, её часть
            # индекса пересобирается — позиции могли измениться или исчезнуть.
            refresh_version_embeddings.delay(version_id)
            return version.status
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.generate_rule_proposal")
def generate_rule_proposal(attempt_id: str) -> str:
    """Run exactly one bounded GPT proposal for one price-table section."""

    return asyncio.run(_generate_rule_proposal(attempt_id))


async def _generate_rule_proposal(attempt_id: str) -> str:
    engine, session_factory = make_task_engine()
    try:
        async with session_factory() as session:
            return await run_rule_generation_attempt(session, uuid.UUID(attempt_id))
    finally:
        await engine.dispose()


async def _section_heartbeat(
    attempt_id: uuid.UUID, lease_owner: str, stop: asyncio.Event
) -> None:
    interval = min(60, max(5, settings.rule_generation_lease_seconds // 3))
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        engine, session_factory = make_task_engine()
        try:
            async with session_factory() as session:
                owned = await heartbeat_attempt(
                    session,
                    attempt_id=attempt_id,
                    lease_owner=lease_owner,
                    lease_seconds=settings.rule_generation_lease_seconds,
                )
                if not owned:
                    return
        finally:
            await engine.dispose()


def _transport_failure(operation: RuleGenerationOperation | None) -> tuple[bool, str]:
    if operation is None:
        return False, "section_execution_failed"
    status = operation.http_status
    retryable_status = status in {408, 409, 425, 429} or (
        isinstance(status, int) and 500 <= status <= 599
    )
    retryable = operation.error_type == "transport_error" or (
        operation.error_type == "provider_http_error" and retryable_status
    )
    reason = operation.error_type or (
        f"HTTP_{status}" if status is not None else "section_execution_failed"
    )
    return retryable, reason


@celery_app.task(
    name="app.tasks.generate_rule_proposal_section",
    acks_late=True,
    reject_on_worker_lost=True,
)
def generate_rule_proposal_section(attempt_id: str) -> str:
    """Process one section; PostgreSQL owns eligibility and idempotency."""

    if not settings.durable_rule_generation_enabled:
        return "disabled"
    result = asyncio.run(_generate_rule_proposal_section(attempt_id))
    batch_id = result.get("batch_id")
    if batch_id and result.get("claimed"):
        finalize_rule_batch.delay(batch_id)
    return str(result["status"])


async def _generate_rule_proposal_section(attempt_id: str) -> dict[str, object]:
    parsed_id = uuid.UUID(attempt_id)
    lease_owner = uuid.uuid4().hex
    engine, session_factory = make_task_engine()
    stop = asyncio.Event()
    heartbeat_task: asyncio.Task | None = None
    try:
        async with session_factory() as session:
            claim = await claim_attempt(
                session,
                attempt_id=parsed_id,
                lease_owner=lease_owner,
                lease_seconds=settings.rule_generation_lease_seconds,
            )
            attempt = await session.get(RuleGenerationAttempt, parsed_id)
            batch_id = str(attempt.batch_id) if attempt is not None else None
            if not claim.claimed:
                return {"status": claim.status, "claimed": False, "batch_id": batch_id}
            heartbeat_task = asyncio.create_task(
                _section_heartbeat(parsed_id, lease_owner, stop)
            )
            status = await run_rule_generation_attempt(
                session,
                parsed_id,
                lease_claimed=True,
                recover_started_operation=True,
            )
            if status == "proposed":
                await mark_section_succeeded(
                    session, attempt_id=parsed_id, lease_owner=lease_owner
                )
                status = ATTEMPT_SUCCEEDED
            elif status in {"catalog_without_price", "skipped_non_price"}:
                await mark_section_succeeded(
                    session,
                    attempt_id=parsed_id,
                    lease_owner=lease_owner,
                    status=status,
                )
            elif status in {"rejected", "insufficient_evidence"}:
                status = await record_section_failure(
                    session,
                    attempt_id=parsed_id,
                    lease_owner=lease_owner,
                    failure_class="semantic",
                    terminal_reason=status,
                    retry_delays=settings.rule_generation_retry_delays_seconds,
                )
            elif status == "failed":
                operation = await session.scalar(
                    select(RuleGenerationOperation)
                    .where(RuleGenerationOperation.attempt_id == parsed_id)
                    .order_by(RuleGenerationOperation.operation_no.desc())
                    .limit(1)
                )
                retryable, reason = _transport_failure(operation)
                status = await record_section_failure(
                    session,
                    attempt_id=parsed_id,
                    lease_owner=lease_owner,
                    failure_class="transport" if retryable else "semantic",
                    terminal_reason=reason,
                    retry_delays=settings.rule_generation_retry_delays_seconds,
                )
            return {"status": status, "claimed": True, "batch_id": batch_id}
    finally:
        stop.set()
        if heartbeat_task is not None:
            await heartbeat_task
        await engine.dispose()


@celery_app.task(name="app.tasks.dispatch_queued_rule_attempts")
def dispatch_queued_rule_attempts() -> dict[str, int]:
    if not settings.durable_rule_generation_enabled:
        return {"published": 0, "finalizers": 0}
    result = asyncio.run(_dispatch_queued_rule_attempts())
    for attempt_id in result["attempt_ids"]:
        generate_rule_proposal_section.delay(attempt_id)
    for batch_id in result["finalizable_batch_ids"]:
        finalize_rule_batch.delay(batch_id)
    return {
        "published": len(result["attempt_ids"]),
        "finalizers": len(result["finalizable_batch_ids"]),
    }


async def _dispatch_queued_rule_attempts() -> dict[str, list[str]]:
    engine, session_factory = make_task_engine()
    try:
        async with session_factory() as session:
            ids = await reserve_dispatchable_attempts(
                session,
                limit=settings.rule_generation_dispatch_limit,
                publication_lease_seconds=max(
                    10, settings.rule_generation_dispatch_interval_seconds * 2
                ),
            )
            non_terminal = PriceImport.status.not_in(PriceImportStatus.terminal_values())
            batch_ids = list(
                (
                    await session.execute(
                        select(PriceImport.rule_batch_id).where(
                            non_terminal,
                            PriceImport.rule_batch_id.is_not(None),
                        )
                    )
                ).scalars()
            )
            finalizable: list[str] = []
            for batch_id in batch_ids:
                state = await inspect_batch(session, batch_id=batch_id)
                if state.pending_count == 0:
                    finalizable.append(str(batch_id))
            return {
                "attempt_ids": [str(value) for value in ids],
                "finalizable_batch_ids": finalizable,
            }
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.recover_stale_rule_attempts")
def recover_stale_rule_attempts() -> dict[str, int]:
    if not settings.durable_rule_generation_enabled:
        return {"recovered": 0}
    recovered = asyncio.run(_recover_stale_rule_attempts())
    if recovered:
        dispatch_queued_rule_attempts.delay()
    return {"recovered": len(recovered)}


async def _recover_stale_rule_attempts() -> list[str]:
    engine, session_factory = make_task_engine()
    try:
        async with session_factory() as session:
            ids = await recover_stale_attempts(
                session, limit=settings.rule_generation_dispatch_limit
            )
            return [str(value) for value in ids]
    finally:
        await engine.dispose()


@celery_app.task(
    name="app.tasks.finalize_rule_batch",
    acks_late=True,
    reject_on_worker_lost=True,
)
def finalize_rule_batch(batch_id: str) -> str:
    """Lightweight coordinator; it never calls a model provider."""

    if not settings.durable_rule_generation_enabled:
        return "disabled"
    return asyncio.run(_finalize_rule_batch(batch_id))


async def _finalize_rule_batch(batch_id: str) -> str:
    from app.import_service import advance_price_import

    parsed_batch_id = uuid.UUID(batch_id)
    engine, session_factory = make_task_engine()
    try:
        async with session_factory() as session:
            state = await inspect_batch(session, batch_id=parsed_batch_id)
            price_import = await session.scalar(
                select(PriceImport).where(PriceImport.rule_batch_id == parsed_batch_id)
            )
            if price_import is None:
                return "no_import"
            if (
                price_import.status in PriceImportStatus.terminal_values()
                and price_import.status != PriceImportStatus.review_required.value
            ):
                return price_import.status
            if state.total_count == 0:
                return "waiting"
            # advance_price_import owns the existing advisory lock and the
            # idempotent partial materialization and final quality pipeline.
            # It persists newly successful sections even while neighbours are
            # pending, and only reaches final compilation when all are ready.
            return await advance_price_import(session, price_import.id)
    finally:
        await engine.dispose()


@celery_app.task(
    name="app.tasks.generate_rule_proposal_batch",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def generate_rule_proposal_batch(
    self, batch_id: str, page_size: int = 24, page_offset: int = 0
) -> dict:
    """Process one bounded page and enqueue the next continuation page."""

    delivery_info = getattr(self.request, "delivery_info", {}) or {}
    result = asyncio.run(
        _generate_rule_proposal_batch(
            batch_id,
            page_size,
            page_offset,
            reset_generating=bool(delivery_info.get("redelivered")),
        )
    )
    next_offset = result.get("next_page_offset")
    if isinstance(next_offset, int):
        # Broker publication is part of this task's success condition. If it
        # fails, Celery redelivers this page; completed attempts are skipped
        # and the missing continuation is published again.
        self.apply_async(args=[batch_id, page_size, next_offset])
    elif result.get("price_import_id"):
        # The last bounded page hands control back to the lightweight import
        # state machine. If publication fails, late ack causes this page to be
        # redelivered; completed attempts are skipped and continuation is sent.
        run_price_import.delay(str(result["price_import_id"]))
    return result


async def _generate_rule_proposal_batch(
    batch_id: str,
    page_size: int,
    page_offset: int = 0,
    *,
    reset_generating: bool = False,
) -> dict:
    parsed_batch_id = uuid.UUID(batch_id)
    engine, session_factory = make_task_engine()
    statuses: dict[str, int] = {}
    processed = 0
    budget_exhausted = False
    price_import_id: uuid.UUID | None = None
    try:
        async with session_factory() as session:
            attempt_ids = list(
                (
                    await session.execute(
                        select(RuleGenerationAttempt.id)
                        .where(RuleGenerationAttempt.batch_id == parsed_batch_id)
                        .order_by(RuleGenerationAttempt.created_at, RuleGenerationAttempt.id)
                    )
                ).scalars()
            )
            price_import = (
                await session.execute(
                    select(PriceImport).where(PriceImport.rule_batch_id == parsed_batch_id)
                )
            ).scalar_one_or_none()
            service_client = (
                await session.get(ServiceClient, price_import.service_client_id)
                if price_import is not None
                else None
            )
            price_import_id = price_import.id if price_import is not None else None
            page, next_offset = continuation_page(
                attempt_ids, page_size=page_size, page_offset=page_offset
            )
            if reset_generating and page:
                await session.execute(
                    update(RuleGenerationAttempt)
                    .where(
                        RuleGenerationAttempt.id.in_(page),
                        RuleGenerationAttempt.status == "generating",
                    )
                    .values(status="queued")
                )
                await session.commit()
            for attempt_id in page:
                if price_import is not None and service_client is not None:
                    from app.import_telemetry import aggregate_import_telemetry

                    usage = await aggregate_import_telemetry(session, price_import.id)
                    if (
                        int(usage["input_tokens"])
                        >= service_client.max_input_tokens_per_import
                        or int(usage["output_tokens"])
                        >= service_client.max_output_tokens_per_import
                    ):
                        price_import.telemetry_summary = usage
                        budget_exhausted = True
                        next_offset = None
                        await session.commit()
                        break
                status = await run_rule_generation_attempt(session, attempt_id)
                statuses[status] = statuses.get(status, 0) + 1
                processed += 1
        return {
            "batch_id": batch_id,
            "page_offset": page_offset,
            "next_page_offset": next_offset,
            "processed": processed,
            "statuses": statuses,
            "price_import_id": str(price_import_id) if price_import_id else None,
            "budget_exhausted": budget_exhausted,
            **page_summary(total_sections=len(attempt_ids), page_size=page_size),
        }
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.recover_stale_price_imports")
def recover_stale_price_imports() -> dict[str, int]:
    import_ids = asyncio.run(_recover_stale_price_imports())
    for import_id in import_ids:
        run_price_import.delay(import_id)
    return {"requeued": len(import_ids)}


async def _recover_stale_price_imports() -> list[str]:
    """Requeue non-terminal imports whose owning worker disappeared."""

    from app.config import settings

    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.price_import_stale_after_seconds
    )
    engine, session_factory = make_task_engine()
    try:
        async with session_factory() as session:
            imports = list(
                (
                    await session.execute(
                        select(PriceImport).where(
                            PriceImport.status.not_in(PriceImportStatus.terminal_values()),
                            PriceImport.updated_at < cutoff,
                        )
                    )
                ).scalars()
            )
            if not imports:
                return []
            ids = [price_import.id for price_import in imports]
            await session.execute(
                update(RuleGenerationAttempt)
                .where(
                    RuleGenerationAttempt.price_import_id.in_(ids),
                    RuleGenerationAttempt.status == "generating",
                    RuleGenerationAttempt.updated_at < cutoff,
                )
                .values(status="queued", error_message=None)
            )
            now = datetime.now(timezone.utc)
            for price_import in imports:
                if price_import.status == PriceImportStatus.generating_rule.value:
                    price_import.attempt_no = 0
                price_import.updated_at = now
            await session.commit()
            return [str(import_id) for import_id in ids]
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.apply_model_rule_to_version")
def apply_model_rule_to_version(version_id: str, rule_id: str) -> str:
    return asyncio.run(_apply_model_rule_to_version(version_id, rule_id))


async def _apply_model_rule_to_version(version_id: str, rule_id: str) -> str:
    from app.models import ParseRule

    engine, session_factory = make_task_engine()
    try:
        async with session_factory() as session:
            version = await session.get(PriceVersion, uuid.UUID(version_id))
            rule = await session.get(ParseRule, uuid.UUID(rule_id))
            if version is None or rule is None:
                return "not_found"
            price_file = await session.get(PriceFile, version.price_file_id)
            if price_file is None:
                return "not_found"
            try:
                data = storage.get_price_file(price_file.storage_key)
                await apply_parse_rule_to_version(
                    session,
                    version=version,
                    price_file=price_file,
                    data=data,
                    rule=rule,
                )
                await session.commit()
                return version.status
            except Exception as exc:  # noqa: BLE001 - persist task failure
                await session.rollback()
                version = await session.get(PriceVersion, uuid.UUID(version_id))
                if version is not None:
                    version.status = VersionStatus.validation_failed.value
                    version.error_message = f"{type(exc).__name__}: {exc}"
                    await session.commit()
                return VersionStatus.validation_failed.value
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.run_corpus_task")
def run_corpus_task(corpus_run_id: str, path: str, force: bool = False) -> str:
    return asyncio.run(_run_corpus(corpus_run_id, path, force))


async def _run_corpus(corpus_run_id: str, path: str, force: bool) -> str:
    engine, session_factory = make_task_engine()
    try:
        return await run_corpus(session_factory, uuid.UUID(corpus_run_id), path, force)
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.build_item_embeddings")
def build_item_embeddings(version_id: str, rebuild: bool = False) -> str:
    """Собрать индекс эмбеддингов версии прайса.

    Новый прайс расширяет индекс (`rebuild=False`), перезалитый или
    переразобранный — пересобирает свою часть (`rebuild=True`). Задача фоновая:
    считать векторы в HTTP-запросе активации значило бы держать пользователя
    на прогрессе индексации.
    """
    return asyncio.run(_build_item_embeddings(version_id, rebuild))


async def _build_item_embeddings(version_id: str, rebuild: bool) -> str:
    engine, session_factory = make_task_engine()
    try:
        async with session_factory() as session:
            service = EmbeddingIndexService(session)
            result = await service.index_version(uuid.UUID(version_id), rebuild=rebuild)
            await session.commit()
            return (
                f"{result.mode}: items={result.items} inserted={result.inserted} "
                f"reused={result.reused} removed={result.removed}"
            )
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.drop_version_embeddings")
def drop_version_embeddings(version_id: str) -> str:
    """Убрать версию из индекса подбора: её позиции больше не отвечают."""
    return asyncio.run(_drop_version_embeddings(version_id))


async def _drop_version_embeddings(version_id: str) -> str:
    engine, session_factory = make_task_engine()
    try:
        async with session_factory() as session:
            removed = await EmbeddingIndexService(session).drop_version(uuid.UUID(version_id))
            await session.commit()
            return f"removed={removed}"
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.refresh_version_embeddings")
def refresh_version_embeddings(version_id: str) -> str:
    """Пересобрать индекс версии после повторного разбора прайса."""
    return asyncio.run(_refresh_version_embeddings(version_id))


async def _refresh_version_embeddings(version_id: str) -> str:
    engine, session_factory = make_task_engine()
    try:
        async with session_factory() as session:
            result = await EmbeddingIndexService(session).refresh_version(uuid.UUID(version_id))
            await session.commit()
            if result is None:
                return "not_indexed"
            return f"rebuild: items={result.items} inserted={result.inserted} reused={result.reused}"
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.rebuild_canonical_layer")
def rebuild_canonical_layer(limit: int = 0) -> str:
    """Пересобрать канонический слой целиком.

    Операция идёт минуты: на боевом каталоге это 227 тыс. позиций. Синхронным
    HTTP-запросом она не выживает — обратный прокси рвёт соединение по
    таймауту, запрос отменяется вместе с транзакцией, и в базе не остаётся
    ничего. Поэтому её место здесь, рядом с разбором прайса.
    """
    return asyncio.run(_rebuild_canonical_layer(limit))


async def _rebuild_canonical_layer(limit: int) -> str:
    engine, session_factory = make_task_engine()
    try:
        async with session_factory() as session:
            result = await CanonicalService(session).rebuild(limit=limit)
            await session.commit()
            return (
                f"items={result.items} canonicals={result.canonicals} "
                f"disputed={result.disputed}"
            )
    finally:
        await engine.dispose()


@celery_app.task(name="app.tasks.rebuild_search_index")
def rebuild_search_index(rebuild: bool = False) -> str:
    """Собрать индекс подбора по всем версиям, участвующим в поиске.

    Без этого индекса векторная ступень каскада не находит ничего и подбор
    молча деградирует до сравнения строк: на боевом каталоге запрос
    «огнетушитель ОП-2(з)-АВСЕ-01» приводил к кронштейну для ОУ-2, потому что
    в его наименовании нашлось похожее обозначение.
    """
    return asyncio.run(_rebuild_search_index(rebuild))


async def _rebuild_search_index(rebuild: bool) -> str:
    engine, session_factory = make_task_engine()
    try:
        async with session_factory() as session:
            versions = list(
                await session.scalars(
                    select(PriceVersion.id).where(
                        PriceVersion.status.in_(SEARCHABLE_STATUSES),
                        # Глобальный индекс собирается по общим прайсам: разовый
                        # счёт/КП заявки в общий каталог не входит.
                        PriceVersion.document_type
                        == PriceDocumentType.price_list.value,
                    )
                )
            )
            indexed = 0
            inserted = 0
            for version_id in versions:
                result = await EmbeddingIndexService(session).index_version(
                    version_id, rebuild=rebuild
                )
                # Коммит на версию, а не на весь прогон: 227 тыс. позиций в
                # одной транзакции держат снимок базы минутами, а падение на
                # последней версии обнуляет всю работу.
                await session.commit()
                indexed += 1
                inserted += result.inserted
            return f"versions={indexed} inserted={inserted}"
    finally:
        await engine.dispose()
