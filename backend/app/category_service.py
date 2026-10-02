"""Supplier and global category directory helpers.

Source category text is provenance and is never rewritten.  Normalized values
only provide stable technical duplicate keys; semantic equivalence is always
an explicit SupplierCategoryMapping selected by an administrator.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import delete, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    GlobalCategory,
    ORIGINAL_PROJECT_ID,
    PriceParseIssue,
    PriceFile,
    PriceVersion,
    SupplierCategory,
    SupplierCategoryAlias,
    SupplierCategoryMapping,
    SupplierPriceItem,
)


_WS_RE = re.compile(r"\s+")
_PATH_SEPARATOR_RE = re.compile(r"\s+/\s+")
_ARTICLE_ONLY_RE = re.compile(r"^[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё._/()\-]{1,63}$")
_OUTLINE_CATEGORY_PREFIX_RE = re.compile(
    r"^(?:\d+(?:\.\d+)+\.?|\d+\.)\s+"
)
_TRAILING_CATEGORY_PUNCTUATION_RE = re.compile(r"\s*[.;:,]+\s*$")
_GLOBAL_CATEGORY_FALLBACK_NAME = "Требует разбора"


def normalize_category_name(value: str) -> str:
    """Return a conservative duplicate key without inventing semantics."""

    normalized = unicodedata.normalize("NFKC", str(value)).casefold().replace("ё", "е")
    return _WS_RE.sub(" ", normalized).strip()


def canonical_global_category_name(value: str) -> str:
    """Remove supplier outline syntax while retaining the exact source elsewhere.

    Only unambiguous dotted outline prefixes are removed. Plain numeric names
    such as ``12 В`` and ``16 портовые`` remain meaningful category labels.
    """

    source = _WS_RE.sub(" ", unicodedata.normalize("NFKC", str(value))).strip()
    source = _OUTLINE_CATEGORY_PREFIX_RE.sub("", source)
    source = _TRAILING_CATEGORY_PUNCTUATION_RE.sub("", source)
    return source.strip()


def canonical_global_category_path(value: str) -> list[tuple[str, str]]:
    """Return safe display/key pairs for every explicit source-path segment.

    A source path is the identity of an automatic mapping.  Falling back the
    whole path (rather than silently dropping one bad ancestor) keeps malformed
    supplier headings visible for operator review and prevents accidental
    cross-branch merges.
    """

    result: list[tuple[str, str]] = []
    for segment in split_category_path(value):
        name = canonical_global_category_name(segment)
        key = normalize_category_name(name)
        if not key or len(key) > 512 or len(name) > 512:
            fallback_key = normalize_category_name(_GLOBAL_CATEGORY_FALLBACK_NAME)
            return [(_GLOBAL_CATEGORY_FALLBACK_NAME, fallback_key)]
        result.append((name, key))
    if result:
        return result
    fallback_key = normalize_category_name(_GLOBAL_CATEGORY_FALLBACK_NAME)
    return [(_GLOBAL_CATEGORY_FALLBACK_NAME, fallback_key)]


def split_category_path(value: str) -> list[str]:
    """Split only the explicit ``space/slash/space`` hierarchy emitted by the parser.

    An unspaced slash may be part of a supplier's category name and therefore
    must not silently create another hierarchy level.
    """

    source = unicodedata.normalize("NFKC", str(value)).strip()
    return [segment.strip() for segment in _PATH_SEPARATOR_RE.split(source) if segment.strip()]


def normalize_category_path(value: str) -> str:
    return " / ".join(normalize_category_name(segment) for segment in split_category_path(value))


def looks_like_article_only(value: str) -> bool:
    candidate = _WS_RE.sub(" ", str(value)).strip()
    return bool(
        _ARTICLE_ONLY_RE.fullmatch(candidate)
        and any(char.isalpha() for char in candidate)
        and any(char.isdigit() for char in candidate)
    )


async def bootstrap_global_category_directory(
    session: AsyncSession,
    *,
    created_by: str,
    project_id: uuid.UUID = ORIGINAL_PROJECT_ID,
    commit: bool = True,
) -> dict[str, int]:
    """Build path-aware global nodes and map every new source category.

    Exact normalized *paths* are reused across suppliers.  Equal leaf labels
    below different parents intentionally remain different global categories.
    No synonym or semantic inference is attempted.
    """

    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"global-category-bootstrap:{project_id}"},
        )

    unmapped = (
        await session.execute(
            select(SupplierCategory)
            .outerjoin(SupplierCategoryMapping)
            .where(
                SupplierCategory.project_id == project_id,
                SupplierCategoryMapping.id.is_(None),
            )
            .order_by(SupplierCategory.normalized_path, SupplierCategory.id)
        )
    ).scalars().all()
    targets, created_count, reused_count, blocked_count = (
        await _ensure_global_category_paths(session, unmapped, project_id=project_id)
    )

    mapping_records: list[dict] = []
    for row in unmapped:
        target = targets.get(row.id)
        if target is None:
            continue
        mapping_records.append(
            {
                "id": uuid.uuid4(),
                "supplier_category_id": row.id,
                "global_category_id": target.id,
                "mapping_source": "import",
                "created_by": created_by[:255],
            }
        )
    if mapping_records:
        await session.execute(insert(SupplierCategoryMapping), mapping_records)
    if commit:
        await session.commit()
    else:
        await session.flush()

    remaining = (
        await session.execute(
            select(SupplierCategory.id)
            .outerjoin(SupplierCategoryMapping)
            .where(
                SupplierCategory.project_id == project_id,
                SupplierCategoryMapping.id.is_(None),
            )
        )
    ).scalars().all()
    return {
        "created_global_categories": created_count,
        "created_mappings": len(mapping_records),
        "reused_global_categories": reused_count,
        "blocked_deprecated_paths": blocked_count,
        "remaining_unmapped": len(remaining),
    }


def _prefer_category_label(current: str | None, candidate: str) -> str:
    if current is None or (current.isupper() and not candidate.isupper()):
        return candidate
    return current


async def _ensure_global_category_paths(
    session: AsyncSession,
    sources: Iterable[SupplierCategory],
    *,
    project_id: uuid.UUID = ORIGINAL_PROJECT_ID,
) -> tuple[dict[uuid.UUID, GlobalCategory | None], int, int, int]:
    """Resolve source paths to active global leaves, creating missing nodes.

    Deprecated nodes are never reactivated implicitly: their source paths stay
    unmapped (or keep the current mapping during canonicalization) until an
    operator chooses an active target.
    """

    source_rows = list(sources)
    source_keys: dict[uuid.UUID, tuple[str, ...]] = {}
    labels: dict[tuple[str, ...], str] = {}
    for source in source_rows:
        canonical = canonical_global_category_path(source.source_path)
        keys: list[str] = []
        for name, key in canonical:
            keys.append(key)
            prefix = tuple(keys)
            labels[prefix] = _prefer_category_label(labels.get(prefix), name)
        source_keys[source.id] = tuple(keys)

    existing = (
        await session.execute(
            select(GlobalCategory).where(GlobalCategory.project_id == project_id)
        )
    ).scalars().all()
    by_identity = {(row.parent_id, row.normalized_name): row for row in existing}
    by_path: dict[tuple[str, ...], GlobalCategory | None] = {}
    created = 0
    reused_ids: set[uuid.UUID] = set()
    for path_key in sorted(labels, key=lambda value: (len(value), value)):
        parent_path = path_key[:-1]
        parent = by_path.get(parent_path) if parent_path else None
        if parent_path and parent is None:
            by_path[path_key] = None
            continue
        identity = (parent.id if parent is not None else None, path_key[-1])
        category = by_identity.get(identity)
        if category is not None and category.status != "active":
            by_path[path_key] = None
            continue
        if category is None:
            name = labels[path_key]
            category = GlobalCategory(
                id=uuid.uuid4(),
                project_id=project_id,
                parent_id=parent.id if parent is not None else None,
                name=name,
                normalized_name=path_key[-1],
                path=f"{parent.path} / {name}" if parent is not None else name,
                normalized_path=(
                    f"{parent.normalized_path} / {path_key[-1]}"
                    if parent is not None
                    else path_key[-1]
                ),
                status="active",
            )
            session.add(category)
            by_identity[identity] = category
            created += 1
        else:
            reused_ids.add(category.id)
        by_path[path_key] = category

    await session.flush()
    targets = {source.id: by_path.get(source_keys[source.id]) for source in source_rows}
    blocked_sources = sum(1 for target in targets.values() if target is None)
    return targets, created, len(reused_ids), blocked_sources


async def canonicalize_import_category_mappings(
    session: AsyncSession,
    *,
    project_id: uuid.UUID = ORIGINAL_PROJECT_ID,
) -> tuple[dict[str, int], set[uuid.UUID]]:
    """Move automatic mappings onto canonical full paths.

    Manual semantic choices remain untouched.  The one structural exception is
    a legacy manual mapping from a nested source path to a same-named global
    root: before hierarchy existed that root was the only possible target, so
    it is upgraded once to the new path-aware node while retaining ``manual``
    provenance.
    """

    loaded_mappings = (
        await session.execute(
            select(SupplierCategoryMapping, SupplierCategory, GlobalCategory)
            .join(
                SupplierCategory,
                SupplierCategory.id == SupplierCategoryMapping.supplier_category_id,
            )
            .join(
                GlobalCategory,
                GlobalCategory.id == SupplierCategoryMapping.global_category_id,
            )
            .where(SupplierCategory.project_id == project_id)
        )
    ).all()
    mappings: list[tuple[SupplierCategoryMapping, SupplierCategory]] = []
    legacy_manual_ids: set[uuid.UUID] = set()
    for mapping, source, target in loaded_mappings:
        if mapping.mapping_source == "import":
            mappings.append((mapping, source))
            continue
        canonical_path = canonical_global_category_path(source.source_path)
        is_legacy_flat_manual = (
            mapping.mapping_source == "manual"
            and len(canonical_path) > 1
            and target.parent_id is None
            and target.normalized_name == canonical_path[-1][1]
        )
        if is_legacy_flat_manual:
            mappings.append((mapping, source))
            legacy_manual_ids.add(mapping.id)

    sources = [source for _mapping, source in mappings]
    targets, created, _reused, blocked = await _ensure_global_category_paths(
        session, sources, project_id=project_id
    )

    remapped_import = 0
    remapped_legacy_manual = 0
    old_global_ids: set[uuid.UUID] = set()
    for mapping, source in mappings:
        target = targets.get(source.id)
        if target is None:
            continue
        if mapping.global_category_id == target.id:
            continue
        old_global_ids.add(mapping.global_category_id)
        mapping.global_category_id = target.id
        if mapping.id in legacy_manual_ids:
            remapped_legacy_manual += 1
        else:
            remapped_import += 1
    await session.flush()
    return (
        {
            "created_canonical_categories": created,
            "remapped_import_mappings": remapped_import,
            "remapped_legacy_flat_manual_mappings": remapped_legacy_manual,
            "blocked_deprecated_paths": blocked,
        },
        old_global_ids,
    )


async def refresh_category_directory(
    session: AsyncSession,
    *,
    created_by: str,
    project_id: uuid.UUID = ORIGINAL_PROJECT_ID,
    commit: bool = True,
) -> dict[str, int]:
    """Prune category paths unused after a reparse and rebuild mappings.

    Item-linked supplier categories and all of their ancestors are retained.
    Stale aliases/mappings are removed with their supplier paths. Only global
    categories that were targets of those removed mappings may be pruned, and
    only if no rebuilt mapping or child category still references them. This
    preserves unrelated operator-created empty global categories.
    """

    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"category-directory-refresh:{project_id}"},
        )

    categories = (
        await session.execute(
            select(SupplierCategory).where(SupplierCategory.project_id == project_id)
        )
    ).scalars().all()
    categories_by_id = {row.id: row for row in categories}
    live_ids = set(
        (
            await session.execute(
                select(SupplierPriceItem.supplier_category_id)
                .join(PriceVersion, PriceVersion.id == SupplierPriceItem.price_version_id)
                .join(PriceFile, PriceFile.id == PriceVersion.price_file_id)
                .where(SupplierPriceItem.supplier_category_id.is_not(None))
                .where(PriceFile.project_id == project_id)
                .distinct()
            )
        ).scalars()
    )
    pending = list(live_ids)
    while pending:
        category = categories_by_id.get(pending.pop())
        if category is None or category.parent_id is None or category.parent_id in live_ids:
            continue
        live_ids.add(category.parent_id)
        pending.append(category.parent_id)

    stale = [row for row in categories if row.id not in live_ids]
    stale_ids = {row.id for row in stale}
    candidate_global_ids: set[uuid.UUID] = set()
    if stale_ids:
        candidate_global_ids = set(
            (
                await session.execute(
                    select(SupplierCategoryMapping.global_category_id).where(
                        SupplierCategoryMapping.supplier_category_id.in_(stale_ids)
                    )
                )
            ).scalars()
        )
        await session.execute(
            delete(SupplierCategoryAlias).where(
                SupplierCategoryAlias.supplier_category_id.in_(stale_ids)
            )
        )
        await session.execute(
            delete(SupplierCategoryMapping).where(
                SupplierCategoryMapping.supplier_category_id.in_(stale_ids)
            )
        )
        depths = sorted(
            {row.normalized_path.count(" / ") for row in stale}, reverse=True
        )
        for depth in depths:
            ids_at_depth = {
                row.id for row in stale if row.normalized_path.count(" / ") == depth
            }
            await session.execute(
                delete(SupplierCategory).where(SupplierCategory.id.in_(ids_at_depth))
            )
            await session.flush()

    bootstrap = await bootstrap_global_category_directory(
        session, created_by=created_by, project_id=project_id, commit=False
    )
    canonicalized, old_global_ids = await canonicalize_import_category_mappings(
        session, project_id=project_id
    )
    candidate_global_ids.update(old_global_ids)

    removed_global_categories = 0
    if candidate_global_ids:
        remaining_candidates = set(candidate_global_ids)
        while remaining_candidates:
            used_global_ids = set(
                (
                    await session.execute(
                        select(SupplierCategoryMapping.global_category_id).where(
                            SupplierCategoryMapping.global_category_id.in_(
                                remaining_candidates
                            )
                        )
                    )
                ).scalars()
            )
            parent_global_ids = set(
                (
                    await session.execute(
                        select(GlobalCategory.parent_id).where(
                            GlobalCategory.parent_id.in_(remaining_candidates)
                        )
                    )
                ).scalars()
            )
            orphan_ids = remaining_candidates - used_global_ids - parent_global_ids
            if not orphan_ids:
                break
            result = await session.execute(
                delete(GlobalCategory).where(GlobalCategory.id.in_(orphan_ids))
            )
            removed_global_categories += result.rowcount or 0
            remaining_candidates -= orphan_ids
            await session.flush()

    if commit:
        await session.commit()
    else:
        await session.flush()
    return {
        "removed_supplier_categories": len(stale_ids),
        "removed_global_categories": removed_global_categories,
        **bootstrap,
        **canonicalized,
    }


async def bind_supplier_categories(
    session: AsyncSession,
    *,
    supplier_id: uuid.UUID,
    version_id: uuid.UUID,
    project_id: uuid.UUID = ORIGINAL_PROJECT_ID,
    source_paths: Iterable[str | None],
) -> dict[str, SupplierCategory]:
    """Upsert observed category hierarchies and exact aliases for one parse."""

    raw_paths = {
        unicodedata.normalize("NFKC", str(path)).strip()
        for path in source_paths
        if path is not None and str(path).strip()
    }
    if not raw_paths:
        return {}

    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        # Two imports for the same supplier may finish parsing concurrently.
        # Serialize only this supplier's directory upsert so the database UK
        # remains the invariant without turning a harmless race into a failed
        # import.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"supplier-categories:{project_id}:{supplier_id}"},
        )

    existing = (
        await session.execute(
            select(SupplierCategory).where(
                SupplierCategory.project_id == project_id,
                SupplierCategory.supplier_id == supplier_id,
            )
        )
    ).scalars().all()
    by_normalized_path = {row.normalized_path: row for row in existing}
    existing_aliases = (
        await session.execute(
            select(SupplierCategoryAlias)
            .join(SupplierCategory)
            .where(
                SupplierCategory.project_id == project_id,
                SupplierCategory.supplier_id == supplier_id,
            )
        )
    ).scalars().all()
    aliases_by_key = {
        (row.supplier_category_id, row.source_path): row for row in existing_aliases
    }
    result: dict[str, SupplierCategory] = {}

    for raw_path in sorted(raw_paths):
        segments = split_category_path(raw_path)
        if not segments:
            continue
        parent: SupplierCategory | None = None
        source_prefix: list[str] = []
        normalized_prefix: list[str] = []
        for segment_index, segment in enumerate(segments):
            source_prefix.append(segment)
            normalized_prefix.append(normalize_category_name(segment))
            source_key = (
                raw_path
                if segment_index == len(segments) - 1
                else " / ".join(source_prefix)
            )
            normalized_key = " / ".join(normalized_prefix)
            category = by_normalized_path.get(normalized_key)
            if category is None:
                category = SupplierCategory(
                    project_id=project_id,
                    supplier_id=supplier_id,
                    parent_id=parent.id if parent is not None else None,
                    source_name=segment,
                    normalized_name=normalized_prefix[-1],
                    source_path=source_key,
                    normalized_path=normalized_key,
                    first_seen_version_id=version_id,
                    last_seen_version_id=version_id,
                )
                session.add(category)
                await session.flush()
                by_normalized_path[normalized_key] = category
            else:
                category.last_seen_version_id = version_id
            alias_key = (category.id, source_key)
            alias = aliases_by_key.get(alias_key)
            if alias is None:
                alias = SupplierCategoryAlias(
                    supplier_category_id=category.id,
                    source_path=source_key,
                    first_seen_version_id=version_id,
                    last_seen_version_id=version_id,
                )
                session.add(alias)
                aliases_by_key[alias_key] = alias
            else:
                alias.last_seen_version_id = version_id
                alias.last_seen_at = datetime.now(timezone.utc)
            parent = category
        result[normalize_category_path(raw_path)] = parent
    return result


def global_category_paths(
    *, parent: GlobalCategory | None, name: str
) -> tuple[str, str, str]:
    source_name = _WS_RE.sub(" ", unicodedata.normalize("NFKC", name)).strip()
    normalized_name = normalize_category_name(source_name)
    if not source_name or not normalized_name:
        raise ValueError("Название категории не может быть пустым")
    path = f"{parent.path} / {source_name}" if parent is not None else source_name
    normalized_path = (
        f"{parent.normalized_path} / {normalized_name}"
        if parent is not None
        else normalized_name
    )
    return source_name, path, normalized_path


def item_category_context(item: SupplierPriceItem) -> dict:
    supplier_category = item.supplier_category
    mapping = supplier_category.mapping if supplier_category is not None else None
    derived_category = mapping.global_category if mapping is not None else None
    manual_category = item.manual_category
    # An operator-assigned category wins over the supplier-derived one.
    global_category = manual_category or derived_category
    if manual_category is not None:
        mapping_status = "manual"
    elif mapping is not None:
        mapping_status = "mapped"
    else:
        mapping_status = "unmapped"
    identity = item.normalized_article or item.normalized_name
    return {
        "source_path": item.category_path,
        "supplier_category_id": supplier_category.id if supplier_category else None,
        "supplier_normalized_path": (
            supplier_category.normalized_path if supplier_category else None
        ),
        "global_category_id": global_category.id if global_category else None,
        "global_path": global_category.path if global_category else None,
        "mapping_status": mapping_status,
        "matching_identity": identity,
        "matching_scope": (
            f"global:{global_category.id}"
            if global_category is not None
            else f"supplier:{supplier_category.id}"
            if supplier_category is not None
            else None
        ),
    }


_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+")
_CATEGORY_STOPWORDS = frozenset({
    "и", "в", "на", "для", "по", "из", "с", "к", "от", "до", "шт", "уп", "м",
    "категория", "раздел", "группа", "товар", "продукция", "прочее", "другое",
    # Generic units / measurements: matching on these alone is not a signal.
    "литр", "литра", "литров", "литре", "метр", "метра", "метров", "кг", "гр",
    "грамм", "грамма", "граммов", "мм", "см", "вольт", "ампер", "ватт", "вт",
    "упаковка", "упаковки", "упаковок", "компл", "комплект",
})


def _name_tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value).lower().replace("ё", "е")
    return {
        token
        for token in _TOKEN_RE.findall(normalized)
        if len(token) >= 3 and token not in _CATEGORY_STOPWORDS
    }


def _tokens_match(a: str, b: str) -> bool:
    """Fuzzy equality for Russian inflection: shared long-enough prefix."""

    if a == b:
        return True
    prefix = 0
    for x, y in zip(a, b):
        if x != y:
            break
        prefix += 1
    shorter = min(len(a), len(b))
    return prefix >= 4 and prefix >= 0.7 * shorter


def suggest_category_for_name(
    name: str, categories: list[GlobalCategory]
) -> tuple[GlobalCategory, float] | None:
    """Best global-category guess for a product name by token overlap.

    Deterministic prototype scorer: fuzzy (prefix-based, to tolerate Russian
    inflection) overlap between the product-name tokens and each category's
    leaf-name tokens, with a bonus when a whole category name appears inside
    the product name.  Returns the category and a 0..1 score, or None when
    nothing overlaps.
    """
    name_tokens = _name_tokens(name)
    if not name_tokens:
        return None
    lowered = unicodedata.normalize("NFKC", name).lower().replace("ё", "е")
    best: tuple[GlobalCategory, float] | None = None
    for category in categories:
        if category.status != "active":
            continue
        cat_tokens = _name_tokens(category.name)
        if not cat_tokens:
            continue
        overlap = sum(
            1
            for cat_token in cat_tokens
            if any(_tokens_match(cat_token, name_token) for name_token in name_tokens)
        )
        if overlap == 0:
            continue
        score = overlap / len(cat_tokens | name_tokens)
        if category.name.lower().replace("ё", "е") in lowered:
            score = max(score, 0.9)
        if best is None or score > best[1]:
            best = (category, round(min(score, 1.0), 3))
    return best


async def apply_category_context_gate(
    session: AsyncSession, *, version: PriceVersion
) -> dict:
    """Block article-only items whose category context is absent."""

    items = (
        await session.execute(
            select(SupplierPriceItem).where(SupplierPriceItem.price_version_id == version.id)
        )
    ).scalars().all()
    failures = [
        item
        for item in items
        if looks_like_article_only(item.source_name) and item.supplier_category_id is None
    ]
    existing_coordinates = set(
        (
            await session.execute(
                select(PriceParseIssue.sheet, PriceParseIssue.row_no).where(
                    PriceParseIssue.price_version_id == version.id,
                    PriceParseIssue.code == "ARTICLE_ONLY_CATEGORY_MISSING",
                )
            )
        ).all()
    )
    for item in failures:
        coordinate = (item.source_sheet, item.source_row)
        if coordinate in existing_coordinates:
            continue
        session.add(
            PriceParseIssue(
                price_version_id=version.id,
                severity="error",
                code="ARTICLE_ONLY_CATEGORY_MISSING",
                sheet=item.source_sheet,
                row_no=item.source_row,
                source_value=item.source_name,
                message=(
                    "Позиция похожа только на артикул/модель, но категория не определена; "
                    "без контекста безопасное сопоставление запрещено"
                ),
            )
        )
    if failures:
        version.errors_count += sum(
            1
            for item in failures
            if (item.source_sheet, item.source_row) not in existing_coordinates
        )
    await session.flush()
    return {
        "passed": not failures,
        "failures": len(failures),
        "coordinates": [
            {
                "sheet": item.source_sheet,
                "row": item.source_row,
                "panel_index": item.source_panel_index,
                "source_name": item.source_name,
            }
            for item in failures
        ],
    }
