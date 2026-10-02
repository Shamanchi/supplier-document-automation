"""Refresh only category associations from the current parser.

The maintenance path deliberately leaves item/value/attribute snapshots
untouched. A candidate parse runs in a rolled-back transaction; category
context is copied only to a uniquely matching stored item identified by its
sheet, physical row and normalized source name.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import uuid
from collections import Counter
from dataclasses import asdict, dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import storage
from app.category_service import (
    bind_supplier_categories,
    normalize_category_path,
    split_category_path,
)
from app.db import make_task_engine
from app.models import PriceFile, PriceVersion, SupplierPriceItem, VersionStatus
from app.parse_service import parse_and_persist
from app.parsing.mapping import default_category_from_sheet_name
from app.parsing.pipeline import MAX_CATEGORY_DEPTH, ParsedItem, parse_workbook
from app.reparse import REPARSEABLE_STATUSES


_PRICE_CATEGORY_PREFIX_RE = re.compile(
    r"^(?:цена|цены|стоимость)\b", re.IGNORECASE
)
_OUTLINE_MARKER_ONLY_RE = re.compile(r"^(?:\d+(?:\.\d+)+\.?|\d+\.)$")


@dataclass(frozen=True)
class CategoryRecord:
    item_id: uuid.UUID | None
    source_sheet: str
    source_row: int
    normalized_name: str
    category_path: str | None


@dataclass(frozen=True)
class CategoryRefreshOutcome:
    version_id: uuid.UUID
    stored_items: int
    candidate_items: int
    matched_items: int
    changed_items: int
    skipped: bool = False
    error_message: str | None = None


def _identity(record: CategoryRecord) -> tuple[str, int, str]:
    return (
        record.source_sheet.casefold().strip(),
        record.source_row,
        record.normalized_name,
    )


def exact_category_matches(
    stored: list[CategoryRecord], candidates: list[CategoryRecord]
) -> dict[uuid.UUID, str | None]:
    """Return category updates only for identities unique on both sides."""

    stored_counts = Counter(_identity(record) for record in stored)
    candidate_counts = Counter(_identity(record) for record in candidates)
    candidate_by_identity = {
        _identity(record): record.category_path
        for record in candidates
        if candidate_counts[_identity(record)] == 1
    }
    return {
        record.item_id: candidate_by_identity[_identity(record)]
        for record in stored
        if record.item_id is not None
        and stored_counts[_identity(record)] == 1
        and _identity(record) in candidate_by_identity
    }


def cap_category_path(category_path: str | None) -> str | None:
    """Remove non-semantic captions and keep the local end of a deep path."""

    if not category_path:
        return category_path
    segments = split_category_path(category_path)
    while segments and _PRICE_CATEGORY_PREFIX_RE.search(segments[0]):
        segments.pop(0)
    segments = [
        segment for segment in segments if not _OUTLINE_MARKER_ONLY_RE.fullmatch(segment)
    ]
    if not segments:
        return None
    if len(segments) <= MAX_CATEGORY_DEPTH:
        cleaned = " / ".join(segments)
        return cleaned if cleaned != category_path else category_path
    return " / ".join(segments[-MAX_CATEGORY_DEPTH:])


def safe_stored_category_path(
    category_path: str | None, source_sheet: str
) -> str | None:
    """Repair stored context without inventing semantics or source provenance."""

    cleaned = cap_category_path(category_path)
    return cleaned or default_category_from_sheet_name(source_sheet)


def _record(item: SupplierPriceItem) -> CategoryRecord:
    return CategoryRecord(
        item_id=item.id,
        source_sheet=item.source_sheet,
        source_row=item.source_row,
        normalized_name=item.normalized_name,
        category_path=item.category_path,
    )


def _parsed_record(item: ParsedItem) -> CategoryRecord:
    return CategoryRecord(
        item_id=None,
        source_sheet=item.source_sheet,
        source_row=item.source_row,
        normalized_name=item.normalized_name,
        category_path=item.category_path,
    )


async def _candidate_records(
    session: AsyncSession, version: PriceVersion
) -> tuple[list[CategoryRecord], str | None]:
    price_file = await session.get(PriceFile, version.price_file_id)
    if price_file is None:
        return [], "Оригинал прайса не найден"
    try:
        data = storage.get_price_file(price_file.storage_key)
        stored_mapping = version.mapping
        version.status = VersionStatus.parsing.value
        parsed_status = await parse_and_persist(
            session,
            version,
            price_file,
            data,
            allow_rule_mutations=False,
        )
        if parsed_status != VersionStatus.validated.value:
            # A workbook can contain many safely parsed tables and one
            # ambiguous sheet. Persistence correctly rejects that partial
            # result, but category-only maintenance can still use the safe
            # rows: it copies context only when sheet + physical row + name
            # identify one stored item uniquely on both sides.
            partial = parse_workbook(
                data,
                price_file.original_filename,
                stored_mapping=stored_mapping,
            )
            if partial.items:
                return [_parsed_record(item) for item in partial.items], None
            return [], f"Кандидат завершился со статусом {parsed_status}"
        items = (
            await session.execute(
                select(SupplierPriceItem).where(
                    SupplierPriceItem.price_version_id == version.id
                )
            )
        ).scalars().all()
        return [_record(item) for item in items], None
    except Exception as exc:  # noqa: BLE001 - report and continue the batch
        detail = " ".join(str(exc).split())[:500]
        return [], f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


async def refresh_version_categories(
    session_factory: async_sessionmaker[AsyncSession],
    version_id: uuid.UUID,
    *,
    cap_only: bool = False,
) -> CategoryRefreshOutcome:
    candidate_records: list[CategoryRecord] = []
    candidate_error: str | None = None
    if not cap_only:
        async with session_factory() as candidate_session:
            version = await candidate_session.get(PriceVersion, version_id)
            if version is None:
                return CategoryRefreshOutcome(version_id, 0, 0, 0, 0, True, "Версия не найдена")
            try:
                candidate_records, candidate_error = await _candidate_records(
                    candidate_session, version
                )
            finally:
                await candidate_session.rollback()

    async with session_factory() as session:
        version = await session.scalar(
            select(PriceVersion).where(PriceVersion.id == version_id).with_for_update()
        )
        if version is None:
            return CategoryRefreshOutcome(version_id, 0, len(candidate_records), 0, 0, True, "Версия не найдена")
        stored_items = (
            await session.execute(
                select(SupplierPriceItem).where(
                    SupplierPriceItem.price_version_id == version_id
                )
            )
        ).scalars().all()
        matches = (
            exact_category_matches(
                [_record(item) for item in stored_items], candidate_records
            )
            if candidate_error is None
            else {}
        )
        # Old or manually mapped files are not always reparseable. They still
        # need the same bounded hierarchy invariant as newly parsed data. This
        # fallback changes only category associations and preserves every item,
        # value and attribute snapshot.
        planned_paths = {}
        for item in stored_items:
            category_path = matches.get(
                item.id,
                safe_stored_category_path(item.category_path, item.source_sheet),
            )
            if item.id in matches or category_path != item.category_path:
                planned_paths[item.id] = category_path
        bindings = await bind_supplier_categories(
            session,
            supplier_id=version.supplier_id,
            version_id=version.id,
            project_id=await session.scalar(
                select(PriceFile.project_id).where(PriceFile.id == version.price_file_id)
            ),
            source_paths=planned_paths.values(),
        )
        changed = 0
        for item in stored_items:
            if item.id not in planned_paths:
                continue
            category_path = planned_paths[item.id]
            supplier_category = (
                bindings.get(normalize_category_path(category_path))
                if category_path
                else None
            )
            supplier_category_id = supplier_category.id if supplier_category else None
            if (
                item.category_path != category_path
                or item.supplier_category_id != supplier_category_id
            ):
                item.category_path = category_path
                item.supplier_category_id = supplier_category_id
                changed += 1
        await session.commit()
        return CategoryRefreshOutcome(
            version_id,
            len(stored_items),
            len(candidate_records),
            len(matches),
            changed,
            candidate_error is not None,
            candidate_error,
        )


async def refresh_categories(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    version_ids: list[uuid.UUID] | None = None,
    deep_only: bool = False,
    cap_only: bool = False,
) -> list[CategoryRefreshOutcome]:
    if version_ids is None:
        async with session_factory() as session:
            query = select(PriceVersion.id)
            if not cap_only:
                query = query.where(PriceVersion.status.in_(REPARSEABLE_STATUSES))
            if deep_only:
                category_depth = 1 + (
                    func.length(SupplierPriceItem.category_path)
                    - func.length(
                        func.replace(SupplierPriceItem.category_path, " / ", "")
                    )
                ) / 3
                requires_cleanup = (
                    (category_depth > MAX_CATEGORY_DEPTH)
                    | SupplierPriceItem.category_path.ilike("цена%")
                    | SupplierPriceItem.category_path.ilike("цены%")
                    | SupplierPriceItem.category_path.ilike("стоимость%")
                )
                query = query.where(
                    PriceVersion.id.in_(
                        select(SupplierPriceItem.price_version_id)
                        .where(
                            SupplierPriceItem.category_path.is_not(None),
                            requires_cleanup,
                        )
                        .distinct()
                    )
                )
            version_ids = list(
                (
                    await session.execute(
                        query.order_by(PriceVersion.created_at, PriceVersion.id)
                    )
                ).scalars()
            )
    outcomes = []
    for version_id in version_ids:
        outcomes.append(
            await refresh_version_categories(
                session_factory, version_id, cap_only=cap_only
            )
        )
    return outcomes


async def _run_cli(args: argparse.Namespace) -> int:
    engine, session_factory = make_task_engine()
    try:
        outcomes = await refresh_categories(
            session_factory,
            version_ids=args.version,
            deep_only=args.deep_only,
            cap_only=args.cap_only,
        )
    finally:
        await engine.dispose()
    payload = {
        "total": len(outcomes),
        "skipped": sum(outcome.skipped for outcome in outcomes),
        "stored_items": sum(outcome.stored_items for outcome in outcomes),
        "candidate_items": sum(outcome.candidate_items for outcome in outcomes),
        "matched_items": sum(outcome.matched_items for outcome in outcomes),
        "changed_items": sum(outcome.changed_items for outcome in outcomes),
        "outcomes": [
            {**asdict(outcome), "version_id": str(outcome.version_id)}
            for outcome in outcomes
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Обновить только категории точно совпавших сохранённых позиций"
    )
    parser.add_argument("--version", action="append", type=uuid.UUID)
    parser.add_argument(
        "--deep-only",
        action="store_true",
        help="обработать только версии с глубокой или служебной категорией",
    )
    parser.add_argument(
        "--cap-only",
        action="store_true",
        help="не перечитывать оригиналы, только ограничить существующие пути",
    )
    return asyncio.run(_run_cli(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
