"""Safe in-place reparse of existing price versions.

Unlike a corpus run, this module reads each original from MinIO and replaces
only the parsed rows of the existing version.  It never creates a new supplier
or version.  Active and superseded versions retain their lifecycle state when
the refreshed parse is validated successfully.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import storage
from app.db import make_task_engine
from app.models import (
    PriceFile,
    PriceParseIssue,
    PriceVersion,
    SupplierPriceItem,
    SupplierPriceItemAttributeValue,
    SupplierPriceValue,
    VersionStatus,
)
from app.parsing.rules import MIN_PRICE_RECOGNIZED_SHARE
from app.parse_service import parse_and_persist


REPARSEABLE_STATUSES = {
    VersionStatus.validated.value,
    VersionStatus.active.value,
    VersionStatus.superseded.value,
}

# ``--all-versions`` also retries versions that did not yield a usable
# snapshot in an earlier parser revision.  Uploads currently in progress and
# explicitly withdrawn history are deliberately not touched: an operator or a
# worker may still own the former, while the latter is not an active source to
# revive by a maintenance batch.
ALL_REPARSEABLE_STATUSES = REPARSEABLE_STATUSES | {
    VersionStatus.needs_mapping.value,
    VersionStatus.complex.value,
    VersionStatus.validation_failed.value,
    VersionStatus.failed.value,
    VersionStatus.needs_ocr.value,
    VersionStatus.unsupported_format.value,
}

# The source file is immutable for a price version. A much smaller item set
# after re-reading that same source is therefore more likely to be a parser
# regression than a legitimate catalogue change. Keep the last known-good
# snapshot for manual review instead of replacing it automatically.
MIN_RETAINED_ITEM_SHARE = 0.80


@dataclass(frozen=True)
class ReparseOutcome:
    version_id: uuid.UUID
    previous_status: str
    status: str
    item_count: int
    error_message: str | None = None
    dry_run: bool = False
    skipped: bool = False
    preflight: bool = False
    safe_to_apply: bool | None = None
    previous_metrics: "ReparseMetrics | None" = None
    candidate_metrics: "ReparseMetrics | None" = None


@dataclass(frozen=True)
class ReparseMetrics:
    """Comparable coverage of a stored snapshot or an uncommitted candidate."""

    item_count: int
    priced_item_count: int
    # Число строк цены. Слияние ступеней сокращает позиции, но не цены —
    # только это и отличает слияние от потери данных.
    value_count: int
    description_count: int
    dimensions_count: int
    attribute_count: int
    invalid_attribute_count: int
    rows_accepted: int
    errors_count: int
    warnings_count: int
    quality_score: float | None


async def _target_ids(
    session: AsyncSession,
    version_ids: list[uuid.UUID] | None,
    *,
    include_all_statuses: bool = False,
) -> list[uuid.UUID]:
    if version_ids is not None:
        return list(version_ids)
    target_statuses = (
        ALL_REPARSEABLE_STATUSES if include_all_statuses else REPARSEABLE_STATUSES
    )
    return list(
        (
            await session.execute(
                select(PriceVersion.id)
                .where(PriceVersion.status.in_(target_statuses))
                .order_by(PriceVersion.created_at, PriceVersion.id)
            )
        ).scalars()
    )


def _restore_lifecycle_status(version: PriceVersion, previous_status: str) -> None:
    if version.status != VersionStatus.validated.value:
        return
    if previous_status == VersionStatus.active.value:
        version.status = VersionStatus.active.value
    elif previous_status == VersionStatus.superseded.value:
        version.status = VersionStatus.superseded.value


def _exception_summary(exc: Exception) -> str:
    """Keep batch reports useful without copying raw SQL parameters into them."""
    detail = str(exc).split("[SQL:", maxsplit=1)[0].split("[parameters:", maxsplit=1)[0]
    detail = " ".join(detail.split())[:500]
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


async def _unsafe_refresh_reason(
    session: AsyncSession,
    version: PriceVersion,
    *,
    previous_item_count: int,
    previous_value_count: int | None = None,
) -> str | None:
    """Return a reason when a staged refresh is unsafe to commit.

    ``parse_and_persist`` deliberately accepts some partially usable automatic
    parses as ``validated`` so they can be inspected. That is appropriate for
    a new upload, but a backfill must not replace an existing good snapshot
    unless the same quality gate passes.
    """
    items_with_price = int(
        await session.scalar(
            select(func.count(func.distinct(SupplierPriceValue.item_id)))
            .join(SupplierPriceItem, SupplierPriceItem.id == SupplierPriceValue.item_id)
            .where(SupplierPriceItem.price_version_id == version.id)
        )
        or 0
    )
    blocking_errors = int(
        await session.scalar(
            select(func.count()).select_from(PriceParseIssue).where(
                PriceParseIssue.price_version_id == version.id,
                PriceParseIssue.severity == "error",
                PriceParseIssue.code == "PRICE_PARSE_ERROR",
            )
        )
        or 0
    )
    no_name_rows = int(
        await session.scalar(
            select(func.count()).select_from(PriceParseIssue).where(
                PriceParseIssue.price_version_id == version.id,
                PriceParseIssue.code == "NO_NAME",
            )
        )
        or 0
    )
    price_share = items_with_price / version.rows_accepted if version.rows_accepted else 0.0
    if (
        version.rows_accepted <= 0
        or price_share < MIN_PRICE_RECOGNIZED_SHARE
        or blocking_errors > 0
        or no_name_rows > 0
    ):
        return (
            "новый разбор не прошёл quality gate "
            f"(строк={version.rows_accepted}, с ценой={items_with_price}, "
            f"доля={price_share:.2%}, ошибок цены={blocking_errors}, "
            f"строк без названия={no_name_rows})"
        )
    if (
        previous_item_count > 0
        and version.item_count / previous_item_count < MIN_RETAINED_ITEM_SHARE
    ):
        # Позиций стало меньше — но это не всегда потеря. Слияние ценовых
        # ступеней по количеству складывает несколько строк прайса в одну
        # позицию с несколькими ценами: товаров меньше, цен столько же.
        # Отличает потерю от слияния число цен, а не число позиций.
        value_count = int(
            await session.scalar(
                select(func.count())
                .select_from(SupplierPriceValue)
                .join(SupplierPriceItem, SupplierPriceItem.id == SupplierPriceValue.item_id)
                .where(SupplierPriceItem.price_version_id == version.id)
            )
            or 0
        )
        merged = (
            previous_value_count is not None
            and previous_value_count > 0
            and value_count >= previous_value_count
        )
        if not merged:
            return (
                "новый разбор слишком сократил набор позиций "
                f"({previous_item_count} → {version.item_count}; минимум "
                f"{MIN_RETAINED_ITEM_SHARE:.0%}; цен {previous_value_count} → {value_count})"
            )
    return None


async def _reparse_metrics(session: AsyncSession, version: PriceVersion) -> ReparseMetrics:
    """Read coverage metrics without relying on denormalized version counters."""

    item_count = int(
        await session.scalar(
            select(func.count()).select_from(SupplierPriceItem).where(
                SupplierPriceItem.price_version_id == version.id
            )
        )
        or 0
    )
    priced_item_count = int(
        await session.scalar(
            select(func.count(func.distinct(SupplierPriceValue.item_id)))
            .join(SupplierPriceItem, SupplierPriceItem.id == SupplierPriceValue.item_id)
            .where(SupplierPriceItem.price_version_id == version.id)
        )
        or 0
    )
    value_count = int(
        await session.scalar(
            select(func.count())
            .select_from(SupplierPriceValue)
            .join(SupplierPriceItem, SupplierPriceItem.id == SupplierPriceValue.item_id)
            .where(SupplierPriceItem.price_version_id == version.id)
        )
        or 0
    )
    description_count = int(
        await session.scalar(
            select(func.count()).select_from(SupplierPriceItem).where(
                SupplierPriceItem.price_version_id == version.id,
                SupplierPriceItem.description.is_not(None),
                SupplierPriceItem.description != "",
            )
        )
        or 0
    )
    dimensions_count = int(
        await session.scalar(
            select(func.count()).select_from(SupplierPriceItem).where(
                SupplierPriceItem.price_version_id == version.id,
                SupplierPriceItem.dimensions.is_not(None),
                SupplierPriceItem.dimensions != "",
            )
        )
        or 0
    )
    attribute_count = int(
        await session.scalar(
            select(func.count())
            .select_from(SupplierPriceItemAttributeValue)
            .join(SupplierPriceItem, SupplierPriceItem.id == SupplierPriceItemAttributeValue.item_id)
            .where(SupplierPriceItem.price_version_id == version.id)
        )
        or 0
    )
    invalid_attribute_count = int(
        await session.scalar(
            select(func.count())
            .select_from(SupplierPriceItemAttributeValue)
            .join(SupplierPriceItem, SupplierPriceItem.id == SupplierPriceItemAttributeValue.item_id)
            .where(
                SupplierPriceItem.price_version_id == version.id,
                SupplierPriceItemAttributeValue.parse_status == "invalid",
            )
        )
        or 0
    )
    return ReparseMetrics(
        item_count=item_count,
        priced_item_count=priced_item_count,
        value_count=value_count,
        description_count=description_count,
        dimensions_count=dimensions_count,
        attribute_count=attribute_count,
        invalid_attribute_count=invalid_attribute_count,
        rows_accepted=version.rows_accepted,
        errors_count=version.errors_count,
        warnings_count=version.warnings_count,
        quality_score=version.quality_score,
    )


async def _preflight_one_version(
    session: AsyncSession,
    version: PriceVersion,
    *,
    previous_status: str,
    previous_metrics: ReparseMetrics,
) -> ReparseOutcome:
    """Evaluate a real parser run and roll every database change back.

    ``--dry-run`` is intentionally cheap and only lists IDs.  This path reads
    MinIO and executes the ordinary parser so a batch can be reviewed before
    any snapshot is replaced.
    """
    # Keep identity outside the transaction: a failed autoflush expires ORM
    # attributes until rollback, but a preflight must still report this one
    # version and continue with every other source.
    target_version_id = version.id

    try:
        price_file = await session.get(PriceFile, version.price_file_id)
        if price_file is None:
            return ReparseOutcome(
                version_id=target_version_id,
                previous_status=previous_status,
                status=previous_status,
                item_count=previous_metrics.item_count,
                error_message="Пропущено: оригинал прайса не найден",
                skipped=True,
                preflight=True,
                safe_to_apply=False,
                previous_metrics=previous_metrics,
            )

        version.status = VersionStatus.parsing.value
        version.error_message = None
        data = storage.get_price_file(price_file.storage_key)
        parsed_status = await parse_and_persist(
            session,
            version,
            price_file,
            data,
            allow_rule_mutations=False,
        )
        candidate_metrics = await _reparse_metrics(session, version)
        if parsed_status != VersionStatus.validated.value:
            reason = f"Пропущено: новый разбор завершился со статусом {parsed_status}"
        else:
            reason = await _unsafe_refresh_reason(
                session,
                version,
                previous_item_count=previous_metrics.item_count,
                previous_value_count=previous_metrics.value_count,
            )
            if reason is not None:
                reason = f"Пропущено: {reason}"
        return ReparseOutcome(
            version_id=target_version_id,
            previous_status=previous_status,
            status=previous_status,
            item_count=previous_metrics.item_count,
            error_message=reason,
            skipped=reason is not None,
            preflight=True,
            safe_to_apply=reason is None,
            previous_metrics=previous_metrics,
            candidate_metrics=candidate_metrics,
        )
    except Exception as exc:  # noqa: BLE001 - every version must be reported
        return ReparseOutcome(
            version_id=target_version_id,
            previous_status=previous_status,
            status=previous_status,
            item_count=previous_metrics.item_count,
            error_message=f"Пропущено: {_exception_summary(exc)}",
            skipped=True,
            preflight=True,
            safe_to_apply=False,
            previous_metrics=previous_metrics,
        )
    finally:
        # Candidate items, issues, attribute dictionary rows and version
        # fields are all staged in this transaction.  Never leave a trace of
        # a preflight run in production.
        await session.rollback()


async def reparse_versions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    version_ids: list[uuid.UUID] | None = None,
    dry_run: bool = False,
    preflight: bool = False,
    include_all_statuses: bool = False,
) -> list[ReparseOutcome]:
    """Reparse selected versions in place.

    Each version is handled in its own transaction. A refresh replaces stored
    rows only when it validates successfully; an unreadable or unmappable
    source rolls back and leaves the last known-good snapshot untouched.
    ``dry_run`` only lists targets; ``preflight`` reads and parses originals
    but rolls the candidate back after collecting comparable coverage metrics.
    With ``include_all_statuses`` the batch additionally retries terminal
    failed/unmapped/OCR/format states; only a validated candidate can change
    one of those statuses.
    """
    async with session_factory() as session:
        target_ids = await _target_ids(
            session,
            version_ids,
            include_all_statuses=include_all_statuses,
        )

    outcomes: list[ReparseOutcome] = []
    for version_id in target_ids:
        async with session_factory() as session:
            version = await session.scalar(
                select(PriceVersion).where(PriceVersion.id == version_id).with_for_update()
            )
            if version is None:
                continue
            previous_status = version.status
            previous_item_count = version.item_count
            previous_value_count = (await _reparse_metrics(session, version)).value_count
            permitted_statuses = (
                ALL_REPARSEABLE_STATUSES
                if include_all_statuses
                else REPARSEABLE_STATUSES
            )
            if version_ids is None and previous_status not in permitted_statuses:
                continue

            if dry_run:
                outcomes.append(
                    ReparseOutcome(
                        version_id=version.id,
                        previous_status=previous_status,
                        status=previous_status,
                        item_count=version.item_count,
                        dry_run=True,
                    )
                )
                continue

            if preflight:
                previous_metrics = await _reparse_metrics(session, version)
                outcomes.append(
                    await _preflight_one_version(
                        session,
                        version,
                        previous_status=previous_status,
                        previous_metrics=previous_metrics,
                    )
                )
                continue

            price_file = await session.get(PriceFile, version.price_file_id)
            if price_file is None:
                outcomes.append(
                    ReparseOutcome(
                        version_id=version.id,
                        previous_status=previous_status,
                        status=previous_status,
                        item_count=version.item_count,
                        error_message="Пропущено: оригинал прайса не найден",
                        skipped=True,
                    )
                )
                continue

            version.status = VersionStatus.parsing.value
            version.error_message = None
            try:
                data = storage.get_price_file(price_file.storage_key)
                parsed_status = await parse_and_persist(
                    session,
                    version,
                    price_file,
                    data,
                    allow_rule_mutations=False,
                )
                if parsed_status != VersionStatus.validated.value:
                    await session.rollback()
                    restored = await session.get(PriceVersion, version_id)
                    if restored is None:
                        continue
                    outcomes.append(
                        ReparseOutcome(
                            version_id=restored.id,
                            previous_status=previous_status,
                            status=restored.status,
                            item_count=restored.item_count,
                            error_message=(
                                "Пропущено: новый разбор завершился со статусом "
                                f"{parsed_status}"
                            ),
                            skipped=True,
                        )
                    )
                    continue
                unsafe_reason = await _unsafe_refresh_reason(
                    session,
                    version,
                    previous_item_count=previous_item_count,
                    previous_value_count=previous_value_count,
                )
                if unsafe_reason is not None:
                    await session.rollback()
                    restored = await session.get(PriceVersion, version_id)
                    if restored is None:
                        continue
                    outcomes.append(
                        ReparseOutcome(
                            version_id=restored.id,
                            previous_status=previous_status,
                            status=restored.status,
                            item_count=restored.item_count,
                            error_message=f"Пропущено: {unsafe_reason}",
                            skipped=True,
                        )
                    )
                    continue
                _restore_lifecycle_status(version, previous_status)
                await session.commit()
            except Exception as exc:  # noqa: BLE001 - a batch must finish other versions
                await session.rollback()
                restored = await session.get(PriceVersion, version_id)
                if restored is None:
                    continue
                outcomes.append(
                    ReparseOutcome(
                        version_id=restored.id,
                        previous_status=previous_status,
                        status=restored.status,
                        item_count=restored.item_count,
                        error_message=f"Пропущено: {_exception_summary(exc)}",
                        skipped=True,
                    )
                )
                continue

            outcomes.append(
                ReparseOutcome(
                    version_id=version.id,
                    previous_status=previous_status,
                    status=version.status,
                    item_count=version.item_count,
                    error_message=version.error_message,
                )
            )
    return outcomes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Безопасно повторно разобрать существующие версии из MinIO"
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--all-successful",
        action="store_true",
        help="обработать validated, active и superseded версии",
    )
    scope.add_argument(
        "--all-versions",
        action="store_true",
        help=(
            "повторить все завершённые версии, включая needs_mapping, complex, "
            "failed, needs_ocr и unsupported_format; uploading/parsing/withdrawn не трогать"
        ),
    )
    scope.add_argument(
        "--version",
        action="append",
        type=uuid.UUID,
        help="UUID одной версии; можно указать несколько раз",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="только вывести план, не читать MinIO и не менять БД",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="реально разобрать originals, собрать candidate-метрики и откатить все изменения",
    )
    parser.add_argument(
        "--fail-on-skipped",
        action="store_true",
        help="вернуть ненулевой код, если хотя бы одна версия не готова к применению",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="куда записать JSON-результат помимо stdout",
    )
    return parser


def _outcome_payload(outcome: ReparseOutcome) -> dict:
    return {
        "version_id": str(outcome.version_id),
        "previous_status": outcome.previous_status,
        "status": outcome.status,
        "item_count": outcome.item_count,
        "error_message": outcome.error_message,
        "dry_run": outcome.dry_run,
        "skipped": outcome.skipped,
        "preflight": outcome.preflight,
        "safe_to_apply": outcome.safe_to_apply,
        "previous_metrics": asdict(outcome.previous_metrics)
        if outcome.previous_metrics is not None
        else None,
        "candidate_metrics": asdict(outcome.candidate_metrics)
        if outcome.candidate_metrics is not None
        else None,
    }


async def _run_cli(args: argparse.Namespace) -> int:
    engine, session_factory = make_task_engine()
    try:
        if args.dry_run and args.preflight:
            raise ValueError("--dry-run и --preflight нельзя использовать одновременно")
        outcomes = await reparse_versions(
            session_factory,
            version_ids=args.version if not (args.all_successful or args.all_versions) else None,
            dry_run=args.dry_run,
            preflight=args.preflight,
            include_all_statuses=args.all_versions,
        )
    finally:
        await engine.dispose()

    payload = {
        "dry_run": args.dry_run,
        "preflight": args.preflight,
        "total": len(outcomes),
        "failed": sum(1 for outcome in outcomes if outcome.status == VersionStatus.failed.value),
        "skipped": sum(1 for outcome in outcomes if outcome.skipped),
        "safe_to_apply": sum(1 for outcome in outcomes if outcome.safe_to_apply is True),
        "outcomes": [_outcome_payload(outcome) for outcome in outcomes],
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(body + "\n", encoding="utf-8")
    print(body)
    return 1 if payload["failed"] or (args.fail_on_skipped and payload["skipped"]) else 0


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
