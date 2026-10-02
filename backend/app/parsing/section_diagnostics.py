"""Deterministic evidence for the cost and necessity of section LLM calls.

Stage 6A exposes this evidence as diagnostics.  Stage 6B consumes the same
evaluator behind a default-off rollout flag; there is deliberately no second
runtime evaluator with subtly different safety rules.
"""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
import math
from typing import Any, Iterable

from app.parsing.headers import AUTO_CONFIDENCE_THRESHOLD, normalize_header


TOKEN_ESTIMATION_METHOD = "utf8_bytes_div_4_ceiling"


def _as_ints(value: object) -> set[int]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, int) and item >= 0}


def _descriptor_is_usable(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("kind"), str)
        and bool(value["kind"].strip())
        and isinstance(value.get("vat_mode"), str)
        and bool(value["vat_mode"].strip())
    )


def _resolved_roles(context: dict[str, Any]) -> list[dict[str, Any]]:
    roles: list[dict[str, Any]] = []
    for observation in context.get("deterministic_observations") or []:
        if not isinstance(observation, dict):
            continue
        index = observation.get("index")
        role = observation.get("suggested_role")
        confidence = observation.get("confidence")
        if (
            not isinstance(index, int)
            or index < 0
            or not isinstance(role, str)
            or not isinstance(confidence, (int, float))
            or float(confidence) < AUTO_CONFIDENCE_THRESHOLD
            or role == "ignore"
        ):
            continue
        roles.append(
            {
                "index": index,
                "role": role,
                "confidence": round(float(confidence), 4),
                "price_descriptor": (
                    observation.get("price_descriptor") if role == "price" else None
                ),
                "attribute_code": observation.get("attribute_code"),
                "attribute_value_kind": observation.get("attribute_value_kind"),
                "canonical_unit": observation.get("canonical_unit"),
                "quantity_threshold_column_index": observation.get(
                    "quantity_threshold_column_index"
                ),
            }
        )

    # The existing headerless detector is itself deterministic and its mapping
    # uses the compiler-compatible generic descriptor below.  Reuse that
    # already-proven evidence without modifying the source context or mapping.
    hint = context.get("headerless_mapping_hint")
    if isinstance(hint, dict):
        hinted: list[tuple[int, str, dict[str, str] | None]] = []
        name_index = hint.get("name_column_index")
        if isinstance(name_index, int):
            hinted.append((name_index, "name", None))
        unit_index = hint.get("unit_column_index")
        if isinstance(unit_index, int):
            hinted.append((unit_index, "unit", None))
        for price_index in sorted(_as_ints(hint.get("price_column_indexes"))):
            hinted.append(
                (
                    price_index,
                    "price",
                    {"kind": "other", "vat_mode": "unknown"},
                )
            )
        existing = {(item["index"], item["role"]) for item in roles}
        for index, role, descriptor in hinted:
            if (index, role) not in existing:
                roles.append(
                    {
                        "index": index,
                        "role": role,
                        "confidence": AUTO_CONFIDENCE_THRESHOLD,
                        "price_descriptor": descriptor,
                    }
                )
    return sorted(roles, key=lambda item: (item["index"], item["role"]))


def evaluate_section_llm_need(
    context: dict[str, Any], *, loader: str
) -> dict[str, Any]:
    """Describe whether existing deterministic evidence is already complete.

    This is intentionally an evaluator, not a fast path.  Its return value is
    never used to skip an attempt or to alter parser/compiler decisions.
    """

    del loader  # reserved for diagnostics compatibility across loader families
    resolved_roles = _resolved_roles(context)
    name_columns = {
        item["index"] for item in resolved_roles if item["role"] == "name"
    }
    price_roles = [item for item in resolved_roles if item["role"] == "price"]
    price_columns = {item["index"] for item in price_roles}
    reasons: list[str] = []
    ambiguities: list[str] = []

    is_matrix = (
        context.get("loader_scope") == "matrix"
        or str(context.get("proposal_contract", "")).startswith("matrix-")
    )
    if is_matrix:
        reasons.append("ambiguous_matrix_axis")
        ambiguities.append("matrix_axis_requires_semantic_resolution")

    section_kind = str(context.get("section_kind") or "")
    if section_kind == "ambiguous_tabular_rows":
        reasons.append("parser_critical_ambiguity")
        ambiguities.append("semantic_roles_not_confirmed")
    elif section_kind == "name_rows_without_price":
        # A recognised price header does not prove that this bounded fragment
        # contains prices.  Replaying it as a priced table can cross the chunk
        # boundary and duplicate priced siblings; catalog-vs-sparse-price is a
        # semantic decision and must remain in the established LLM workflow.
        reasons.append("catalog_semantic_decision_required")
        ambiguities.append("catalog_vs_sparse_price")

    if not name_columns:
        reasons.append("missing_name_role")
    if not price_columns:
        reasons.append("missing_price_role")

    unresolved_descriptors = sorted(
        item["index"]
        for item in price_roles
        if not _descriptor_is_usable(item.get("price_descriptor"))
    )
    if unresolved_descriptors:
        reasons.append("unresolved_price_descriptor")

    presence = context.get("price_presence")
    presence = presence if isinstance(presence, dict) else {}
    structural = context.get("structural_candidates")
    structural = structural if isinstance(structural, dict) else {}
    material_columns = (
        _as_ints(presence.get("price_column_indexes"))
        | _as_ints(structural.get("candidate_numeric_column_indexes"))
    )
    unresolved_material_columns = sorted(material_columns - price_columns)
    if unresolved_material_columns:
        reasons.append("unresolved_material_column")

    expected_columns = set(range(_column_count(context)))
    inspected_columns = _as_ints(presence.get("inspected_column_indexes"))
    if (
        not presence
        or presence.get("all_source_columns_inspected") is not True
        or inspected_columns != expected_columns
    ):
        reasons.append("source_columns_not_fully_inspected")

    for panel in context.get("horizontal_panels") or []:
        if not isinstance(panel, dict):
            continue
        panel_names = _as_ints(panel.get("name_columns"))
        panel_prices = _as_ints(panel.get("price_columns"))
        if not (panel_names & name_columns) or not panel_prices or not panel_prices.issubset(
            price_columns
        ):
            reasons.append("unexplained_horizontal_panel")
            ambiguities.append("horizontal_panel_roles_incomplete")
            break

    reasons = list(dict.fromkeys(reasons))
    ambiguities = list(dict.fromkeys(ambiguities))
    resolved_confidences = [float(item["confidence"]) for item in resolved_roles]
    confidence = min(resolved_confidences) if resolved_confidences else 0.0
    if reasons:
        confidence = min(confidence, 0.69)
    return {
        "decision": "llm_required" if reasons else "deterministic_candidate",
        "confidence": round(confidence, 4),
        "has_name": bool(name_columns),
        "has_price": bool(price_columns),
        "resolved_roles": resolved_roles,
        "unresolved_price_descriptors": unresolved_descriptors,
        "unresolved_material_columns": unresolved_material_columns,
        "structural_ambiguities": ambiguities,
        "reasons": reasons,
    }


def _column_count(context: dict[str, Any]) -> int:
    shape = context.get("sheet_shape")
    if isinstance(shape, dict) and isinstance(shape.get("columns"), int):
        return max(0, int(shape["columns"]))
    headers = context.get("header_values")
    if isinstance(headers, list):
        return len(headers)
    indexes = [
        item.get("index")
        for item in context.get("deterministic_observations") or []
        if isinstance(item, dict) and isinstance(item.get("index"), int)
    ]
    return max(indexes, default=-1) + 1


def _normalised_headers(context: dict[str, Any], column_count: int) -> list[str]:
    values = context.get("header_values")
    if isinstance(values, list):
        return [normalize_header(value) for value in values[:column_count]] + [
            ""
        ] * max(0, column_count - len(values))
    by_index = {
        item["index"]: item.get("header")
        for item in context.get("deterministic_observations") or []
        if isinstance(item, dict) and isinstance(item.get("index"), int)
    }
    return [normalize_header(by_index.get(index)) for index in range(column_count)]


def structural_signature(context: dict[str, Any], *, loader: str) -> str:
    """Hash supplier-independent table topology, never sample values/identity."""

    column_count = _column_count(context)
    roles = []
    for observation in context.get("deterministic_observations") or []:
        if not isinstance(observation, dict) or not isinstance(
            observation.get("index"), int
        ):
            continue
        descriptor = observation.get("price_descriptor")
        roles.append(
            {
                "index": observation["index"],
                "role": str(observation.get("suggested_role") or "unknown"),
                "price_kind": (
                    str(descriptor.get("kind")) if isinstance(descriptor, dict) else None
                ),
                "vat_mode": (
                    str(descriptor.get("vat_mode"))
                    if isinstance(descriptor, dict)
                    else None
                ),
            }
        )
    hint = context.get("headerless_mapping_hint")
    hint_shape = None
    if isinstance(hint, dict):
        hint_shape = {
            "name": hint.get("name_column_index"),
            "unit": hint.get("unit_column_index"),
            "prices": sorted(_as_ints(hint.get("price_column_indexes"))),
        }
    panels = [
        {
            "start": panel.get("start_column_index"),
            "end": panel.get("end_column_index"),
            "names": sorted(_as_ints(panel.get("name_columns"))),
            "prices": sorted(_as_ints(panel.get("price_columns"))),
        }
        for panel in context.get("horizontal_panels") or []
        if isinstance(panel, dict)
    ]
    blocks = [
        {"kind": str(block.get("kind") or "unknown")}
        for block in context.get("blocks") or []
        if isinstance(block, dict)
    ]
    payload = {
        "loader": loader.casefold().strip(),
        "loader_scope": str(context.get("loader_scope") or "tabular"),
        "section_kind": str(context.get("section_kind") or "matrix"),
        "column_count": column_count,
        "headers": _normalised_headers(context, column_count),
        "roles": sorted(roles, key=lambda item: (item["index"], item["role"])),
        "headerless_hint": hint_shape,
        "panels": panels,
        "matrix": {
            "proposal_contract": str(context.get("proposal_contract") or ""),
            "block_count": len(blocks),
            "blocks": blocks,
        },
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _context_size(value: object) -> tuple[int, int]:
    rows = 0
    cells = 0
    if isinstance(value, dict):
        maybe_cells = value.get("cells")
        if isinstance(maybe_cells, list):
            rows += 1
            cells += len(maybe_cells)
        for nested in value.values():
            nested_rows, nested_cells = _context_size(nested)
            rows += nested_rows
            cells += nested_cells
    elif isinstance(value, list):
        for nested in value:
            nested_rows, nested_cells = _context_size(nested)
            rows += nested_rows
            cells += nested_cells
    return rows, cells


def measure_prompt(prompt: str, *, context: dict[str, Any]) -> dict[str, Any]:
    """Measure the stored user prompt; token count is a named approximation."""

    prompt_bytes = len(prompt.encode("utf-8"))
    context_rows, context_cells = _context_size(context)
    return {
        "prompt_chars": len(prompt),
        "prompt_bytes": prompt_bytes,
        "estimated_input_tokens": math.ceil(prompt_bytes / 4),
        "token_estimation_method": TOKEN_ESTIMATION_METHOD,
        "context_rows": context_rows,
        "context_cells": context_cells,
    }


def build_section_diagnostics(
    context: dict[str, Any], *, loader: str, prompt: str
) -> dict[str, Any]:
    return {
        "llm_need": evaluate_section_llm_need(context, loader=loader),
        "structural_signature": structural_signature(context, loader=loader),
        "prompt_metrics": measure_prompt(prompt, context=context),
    }


def summarize_batch_diagnostics(
    diagnostics: Iterable[dict[str, Any]],
) -> dict[str, int]:
    rows = list(diagnostics)
    signatures = Counter(
        str(row.get("structural_signature") or "") for row in rows
    )
    decisions = [
        (row.get("llm_need") or {}).get("decision")
        for row in rows
        if isinstance(row.get("llm_need"), dict)
    ]
    return {
        "sections_total": len(rows),
        "sections_deterministic_candidates": decisions.count(
            "deterministic_candidate"
        ),
        "sections_llm_required": decisions.count("llm_required"),
        "unique_structural_signatures": len(signatures),
        "repeated_signature_groups": sum(
            1 for count in signatures.values() if count > 1
        ),
        "max_signature_group_size": max(signatures.values(), default=0),
        "prompt_chars_total": sum(
            int((row.get("prompt_metrics") or {}).get("prompt_chars", 0))
            for row in rows
        ),
        "estimated_input_tokens_total": sum(
            int(
                (row.get("prompt_metrics") or {}).get(
                    "estimated_input_tokens", 0
                )
            )
            for row in rows
        ),
    }
