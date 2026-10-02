"""Fail-closed deterministic preparation for one rule-generation section.

This is orchestration, not a parser and not a second evaluator.  The bounded
Stage 6A context is converted to the existing proposal contract, then accepted
only by the existing source compiler and dry-run validator.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Any

from app.parsing.mapping import MappingDecision
from app.parsing.matrix_unfold import MATRIX_PROPOSAL_CONTRACT
from app.parsing.rule_proposal import ProposalError, dry_run_decision
from app.parsing.rule_proposal_handlers import (
    TABULAR_PROPOSAL_CONTRACT,
    handler_for_contract,
)
from app.parsing.section_diagnostics import evaluate_section_llm_need


@dataclass(frozen=True)
class DeterministicFastPathResult:
    proposal: dict[str, Any]
    validation: dict[str, Any]
    evaluator: dict[str, Any]
    extraction_signature: str


def _extraction_signature(result) -> str:
    """Hash parsing semantics while leaving category to the P1 acceptance gate."""

    items: list[dict[str, Any]] = []
    for item in result.items:
        payload = asdict(item)
        payload.pop("category_path", None)
        # Raw evidence is retained for review, but the Stage 6B equivalence
        # contract compares the extraction fields and source coordinates.
        payload.pop("source_header_cells", None)
        payload.pop("source_data_cells", None)
        payload.pop("source_category_cells", None)
        items.append(payload)
    canonical = json.dumps(
        items,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _column_count(context: dict[str, Any]) -> int:
    shape = context.get("sheet_shape")
    if isinstance(shape, dict) and isinstance(shape.get("columns"), int):
        return max(0, int(shape["columns"]))
    headers = context.get("header_values")
    return len(headers) if isinstance(headers, list) else 0


def _deterministic_proposal(
    context: dict[str, Any], *, loader: str, evaluator: dict[str, Any]
) -> dict[str, Any]:
    if evaluator.get("decision") != "deterministic_candidate":
        raise ProposalError("Stage 6A evaluator requires LLM")
    contract = context.get("proposal_contract", TABULAR_PROPOSAL_CONTRACT)
    if contract == MATRIX_PROPOSAL_CONTRACT:
        raise ProposalError("Matrix sections are not eligible for deterministic fast-path")
    if contract != TABULAR_PROPOSAL_CONTRACT:
        raise ProposalError("Unknown deterministic proposal contract")

    column_count = _column_count(context)
    resolved = {
        int(row["index"]): row
        for row in evaluator.get("resolved_roles") or []
        if isinstance(row, dict) and isinstance(row.get("index"), int)
    }
    columns: list[dict[str, Any]] = []
    for index, evidence in sorted(resolved.items()):
        role = str(evidence.get("role"))
        descriptor = evidence.get("price_descriptor") if role == "price" else None
        columns.append(
            {
                "index": index,
                "role": role,
                "price_descriptor": descriptor,
                "attribute_code": evidence.get("attribute_code"),
                "attribute_value_kind": evidence.get("attribute_value_kind"),
                "canonical_unit": evidence.get("canonical_unit"),
                "quantity_threshold_column_index": evidence.get(
                    "quantity_threshold_column_index"
                ),
            }
        )

    header_row = context.get("header_row_index")
    if not isinstance(header_row, int):
        raise ProposalError("Deterministic section has no source header coordinate")
    sheet_name = context.get("sheet_name")
    section_id = context.get("section_id")
    if not isinstance(sheet_name, str) or not isinstance(section_id, str):
        raise ProposalError("Deterministic section identity is incomplete")
    return {
        "schema_version": "price-rule-proposal-v1",
        "decision": "propose",
        "rule_name": f"Deterministic: {sheet_name}"[:512],
        "loader": loader,
        "source_section_id": section_id,
        "mapping": {
            "sheet_name": sheet_name,
            "header_row_index": header_row,
            "name_fallback_column_indexes": [],
            "columns": columns,
        },
        "discovered_attributes": [],
        "evidence": [
            {
                "row_index": header_row,
                "column_index": index,
                "note": "deterministic inspected source column",
            }
            for index in range(column_count)
        ],
        "unresolved": [],
        "confidence": float(evaluator.get("confidence") or 0.0),
        "requires_manual_review": True,
    }


def prepare_deterministic_fast_path(
    context: dict[str, Any], *, loader: str, grids
) -> DeterministicFastPathResult | None:
    """Return a locally verified proposal, or None when Stage 6A requires LLM.

    Compiler and dry-run failures intentionally propagate to the caller, which
    records a fail-closed fallback and continues through the unchanged provider
    workflow.
    """

    evaluator = evaluate_section_llm_need(context, loader=loader)
    if evaluator["decision"] != "deterministic_candidate":
        return None
    raw = _deterministic_proposal(context, loader=loader, evaluator=evaluator)
    handler = handler_for_contract(
        context.get("proposal_contract", TABULAR_PROPOSAL_CONTRACT)
    )
    proposal = handler.parse(raw)
    compiled = handler.compile(
        proposal, source_context=context, loader=loader, grids=grids
    )
    if compiled.table is None:
        raise ProposalError("Deterministic compiler produced no executable table")
    result, validation = dry_run_decision(
        grids, MappingDecision(tables=[compiled.table], auto_ok=True)
    )
    if validation.get("passed") is not True or not result.items:
        raise ProposalError("Deterministic section failed local dry-run")
    if any(item.source_section_id != context.get("section_id") for item in result.items):
        raise ProposalError("Deterministic dry-run crossed the source section boundary")
    return DeterministicFastPathResult(
        proposal=proposal.model_dump(mode="json"),
        validation={**compiled.validation, **validation},
        evaluator=evaluator,
        extraction_signature=_extraction_signature(result),
    )


def verified_proposal_matches_fast_path(
    deterministic: DeterministicFastPathResult,
    verified_proposal: dict[str, Any],
    *,
    context: dict[str, Any],
    loader: str,
    grids,
) -> bool:
    """Keep a known exact work result stable when enabling the rollout flag."""

    handler = handler_for_contract(
        context.get("proposal_contract", TABULAR_PROPOSAL_CONTRACT)
    )
    proposal = handler.parse(verified_proposal)
    compiled = handler.compile(
        proposal, source_context=context, loader=loader, grids=grids
    )
    if compiled.table is None:
        return False
    result, validation = dry_run_decision(
        grids, MappingDecision(tables=[compiled.table], auto_ok=True)
    )
    return (
        validation.get("passed") is True
        and _extraction_signature(result) == deterministic.extraction_signature
    )
