"""DB-side parse-rule operations: detection lookups, auto-draft creation and
user-confirmed upsert. Pure fingerprint/recipe math lives in app.parsing.rules.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ParseRule, ParseRuleProvenance, ParseRuleStatus
from app.parsing.pdf_layout import is_pdf_layout_recipe, recipe_matches_pdf_layout
from app.parsing.rules import PARTIAL_JACCARD_THRESHOLD, partial_similarity
from app.parsing.workbook_layout import (
    is_workbook_layout_recipe,
    recipe_matches_workbook_layout,
)

_STATUS_RANK = {ParseRuleStatus.active.value: 0, ParseRuleStatus.draft.value: 1}


def _eligible_for_automatic_match(rule: ParseRule) -> bool:
    """A model draft is review material, not an input to auto-parsing.

    Existing ``auto`` drafts predate this safety boundary and are internally
    generated from a validated deterministic parse. A model-proposed rule is
    different: it becomes matchable only after the explicit activate action.
    """

    return (
        rule.provenance != ParseRuleProvenance.model_proposed.value
        or rule.status == ParseRuleStatus.active.value
    )


async def find_exact_rule(
    session: AsyncSession,
    header_hash: str,
    loader: str,
    supplier_id: uuid.UUID,
    *,
    project_id: uuid.UUID,
) -> ParseRule | None:
    """Exact detection: same header_hash + loader. A supplier-scoped rule wins
    over a global one; active wins over draft; newest revision wins."""
    rules = (
        (
            await session.execute(
                select(ParseRule).where(
                    ParseRule.project_id == project_id,
                    ParseRule.header_hash == header_hash,
                    ParseRule.loader == loader,
                    ParseRule.status != ParseRuleStatus.deprecated.value,
                    (ParseRule.supplier_id == supplier_id) | (ParseRule.supplier_id.is_(None)),
                )
            )
        )
        .scalars()
        .all()
    )
    rules = [rule for rule in rules if _eligible_for_automatic_match(rule)]
    if not rules:
        return None
    rules.sort(
        key=lambda r: (
            0 if r.supplier_id == supplier_id else 1,
            _STATUS_RANK.get(r.status, 2),
            -r.revision,
        )
    )
    return rules[0]


async def find_partial_rule(
    session: AsyncSession,
    fingerprint: dict,
    loader: str,
    supplier_id: uuid.UUID,
    *,
    project_id: uuid.UUID,
    excluded_rule_ids: set[uuid.UUID] | None = None,
) -> tuple[ParseRule, float] | None:
    """Partial detection among this supplier's (and global) non-deprecated
    rules of the same loader: best header-set Jaccard >= threshold."""
    rules = (
        (
            await session.execute(
                select(ParseRule).where(
                    ParseRule.project_id == project_id,
                    ParseRule.loader == loader,
                    ParseRule.status != ParseRuleStatus.deprecated.value,
                    (ParseRule.supplier_id == supplier_id) | (ParseRule.supplier_id.is_(None)),
                )
            )
        )
        .scalars()
        .all()
    )
    rules = [rule for rule in rules if _eligible_for_automatic_match(rule)]
    excluded_rule_ids = excluded_rule_ids or set()
    best: tuple[ParseRule, float] | None = None
    for rule in rules:
        if rule.id in excluded_rule_ids:
            continue
        score = partial_similarity(fingerprint, rule.fingerprint or {})
        if score >= PARTIAL_JACCARD_THRESHOLD and (best is None or score > best[1]):
            best = (rule, score)
        elif best is not None and score == best[1] and rule.supplier_id == supplier_id:
            best = (rule, score)
    return best


async def find_pdf_layout_rule(
    session: AsyncSession,
    data: bytes,
    loader: str,
    supplier_id: uuid.UUID,
    *,
    project_id: uuid.UUID,
) -> ParseRule | None:
    """Find a saved layout rule by its explicit source evidence.

    Layout PDFs have no reliable table header hash before their recipe runs.
    Rules therefore carry a small ``recipe.match.required_text`` signature;
    we only evaluate recipes that prove they match the bounded text evidence.
    """
    if loader != "pdf_text":
        return None
    rules = (
        (
            await session.execute(
                select(ParseRule).where(
                    ParseRule.project_id == project_id,
                    ParseRule.loader == loader,
                    ParseRule.status != ParseRuleStatus.deprecated.value,
                    (ParseRule.supplier_id == supplier_id) | (ParseRule.supplier_id.is_(None)),
                )
            )
        )
        .scalars()
        .all()
    )
    candidates = [
        rule
        for rule in rules
        if _eligible_for_automatic_match(rule)
        and is_pdf_layout_recipe(rule.recipe or {})
        and recipe_matches_pdf_layout(data, rule.recipe or {})
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda rule: (
            0 if rule.supplier_id == supplier_id else 1,
            _STATUS_RANK.get(rule.status, 2),
            -rule.revision,
        )
    )
    return candidates[0]


async def find_workbook_layout_rule(
    session: AsyncSession,
    grids: list,
    loader: str,
    supplier_id: uuid.UUID,
    *,
    project_id: uuid.UUID,
) -> ParseRule | None:
    """Find a saved XLS/XLSX layout rule by structural workbook evidence."""
    if loader not in {"xls", "xlsx"}:
        return None
    rules = (
        (
            await session.execute(
                select(ParseRule).where(
                    ParseRule.project_id == project_id,
                    ParseRule.loader == loader,
                    ParseRule.status != ParseRuleStatus.deprecated.value,
                    (ParseRule.supplier_id == supplier_id)
                    | (ParseRule.supplier_id.is_(None)),
                )
            )
        )
        .scalars()
        .all()
    )
    candidates = [
        rule
        for rule in rules
        if _eligible_for_automatic_match(rule)
        and is_workbook_layout_recipe(rule.recipe or {})
        and recipe_matches_workbook_layout(grids, rule.recipe or {})
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda rule: (
            0 if rule.supplier_id == supplier_id else 1,
            _STATUS_RANK.get(rule.status, 2),
            -rule.revision,
        )
    )
    return candidates[0]


async def create_draft_rule(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    supplier_id: uuid.UUID,
    name: str,
    loader: str,
    header_hash: str,
    fingerprint: dict,
    recipe: dict,
    created_from_version_id: uuid.UUID,
    validation: dict | None,
) -> ParseRule | None:
    """Auto-draft after a confident, validated auto-parse. Idempotent: a rule
    with the same header_hash for this supplier (or global) already covering
    the structure means no new draft."""
    existing = (
        await session.execute(
            select(func.count(ParseRule.id)).where(
                ParseRule.project_id == project_id,
                ParseRule.header_hash == header_hash,
                ParseRule.loader == loader,
                ParseRule.status != ParseRuleStatus.deprecated.value,
                (ParseRule.supplier_id == supplier_id) | (ParseRule.supplier_id.is_(None)),
            )
        )
    ).scalar_one()
    if existing:
        return None
    max_revision = (
        await session.execute(
            select(func.max(ParseRule.revision)).where(
                ParseRule.project_id == project_id,
                ParseRule.header_hash == header_hash,
                ParseRule.loader == loader,
                ParseRule.supplier_id == supplier_id,
            )
        )
    ).scalar_one()
    rule = ParseRule(
        project_id=project_id,
        supplier_id=supplier_id,
        name=name,
        loader=loader,
        header_hash=header_hash,
        fingerprint=fingerprint,
        recipe=recipe,
        revision=(max_revision or 0) + 1,
        status=ParseRuleStatus.draft.value,
        provenance=ParseRuleProvenance.auto.value,
        created_from_version_id=created_from_version_id,
        validation=validation,
    )
    session.add(rule)
    await session.flush()
    return rule


async def create_model_draft_rule(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    supplier_id: uuid.UUID,
    name: str,
    loader: str,
    header_hash: str,
    fingerprint: dict,
    recipe: dict,
    created_from_version_id: uuid.UUID,
    validation: dict,
) -> ParseRule:
    """Create one non-matchable draft from reviewed model proposals.

    A repeated click for the same model batch returns the same draft. A model
    draft from another batch is a signal to inspect it first. An active/manual
    rule may coexist with this non-matchable review draft so operators can
    compare a newly generated recipe without disrupting production parsing.
    """

    same_structure = (
        (
            await session.execute(
                select(ParseRule).where(
                    ParseRule.project_id == project_id,
                    ParseRule.header_hash == header_hash,
                    ParseRule.loader == loader,
                    ParseRule.supplier_id == supplier_id,
                    ParseRule.status != ParseRuleStatus.deprecated.value,
                )
            )
        )
        .scalars()
        .all()
    )
    batch_id = validation.get("model_batch_id")
    for existing in same_structure:
        if existing.provenance != ParseRuleProvenance.model_proposed.value:
            continue
        existing_batch_id = (existing.validation or {}).get("model_batch_id")
        if batch_id and existing_batch_id == batch_id:
            return existing
        raise ValueError(
            "Для этой структуры уже есть черновик из другого model batch; "
            "сначала проверьте или отклоните его"
        )
    max_revision = (
        await session.execute(
            select(func.max(ParseRule.revision)).where(
                ParseRule.project_id == project_id,
                ParseRule.header_hash == header_hash,
                ParseRule.loader == loader,
                ParseRule.supplier_id == supplier_id,
            )
        )
    ).scalar_one()
    rule = ParseRule(
        project_id=project_id,
        supplier_id=supplier_id,
        name=name,
        loader=loader,
        header_hash=header_hash,
        fingerprint=fingerprint,
        recipe=recipe,
        revision=(max_revision or 0) + 1,
        status=ParseRuleStatus.draft.value,
        provenance=ParseRuleProvenance.model_proposed.value,
        created_from_version_id=created_from_version_id,
        validation=validation,
    )
    session.add(rule)
    await session.flush()
    return rule


async def upsert_user_rule(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    supplier_id: uuid.UUID,
    name: str,
    loader: str,
    header_hash: str,
    fingerprint: dict,
    recipe: dict,
    created_from_version_id: uuid.UUID,
    validation: dict | None,
) -> ParseRule:
    """A user-confirmed mapping becomes (or refreshes) the supplier's active
    rule for this structure: new revision on top of whatever existed, previous
    revisions of the same structure are deprecated."""
    same_structure = (
        (
            await session.execute(
                select(ParseRule).where(
                    ParseRule.project_id == project_id,
                    ParseRule.header_hash == header_hash,
                    ParseRule.loader == loader,
                    ParseRule.supplier_id == supplier_id,
                )
            )
        )
        .scalars()
        .all()
    )
    max_revision = max((r.revision for r in same_structure), default=0)
    for old in same_structure:
        if old.status != ParseRuleStatus.deprecated.value:
            old.status = ParseRuleStatus.deprecated.value
            old.updated_at = datetime.now(timezone.utc)
    rule = ParseRule(
        project_id=project_id,
        supplier_id=supplier_id,
        name=name,
        loader=loader,
        header_hash=header_hash,
        fingerprint=fingerprint,
        recipe=recipe,
        revision=max_revision + 1,
        status=ParseRuleStatus.active.value,
        provenance=ParseRuleProvenance.user_confirmed.value,
        created_from_version_id=created_from_version_id,
        validation=validation,
    )
    session.add(rule)
    await session.flush()
    return rule
