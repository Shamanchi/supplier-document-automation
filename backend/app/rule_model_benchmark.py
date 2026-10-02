"""Pure scoring helpers for the reviewed-recipe LLM benchmark."""
from __future__ import annotations

import statistics
from typing import Any


def score_mapping(gold_sheet: dict[str, Any], proposal_mapping: dict[str, Any]) -> dict[str, Any]:
    """Compare a proposal with semantic columns from a reviewed recipe.

    Legacy recipes may mark named source columns as ``ignore`` while the new
    safety contract retains them as attributes. Those columns are deliberately
    excluded; the score measures the roles the reviewed parser actually uses.
    """

    gold_roles = {
        int(column["index"]): str(column["role"])
        for column in gold_sheet.get("columns", [])
        if column.get("role") != "ignore" and isinstance(column.get("index"), int)
    }
    proposed_roles = {
        int(column["index"]): str(column["role"])
        for column in proposal_mapping.get("columns", [])
        if isinstance(column.get("index"), int)
    }
    matched = sum(proposed_roles.get(index) == role for index, role in gold_roles.items())
    total = len(gold_roles)
    f1 = matched / total if total else 0.0
    gold_mandatory = sorted(
        (index, role) for index, role in gold_roles.items() if role in {"name", "price"}
    )
    proposed_mandatory = sorted(
        (index, role)
        for index, role in proposed_roles.items()
        if role in {"name", "price"}
    )
    return {
        "header_row_exact": proposal_mapping.get("header_row_index")
        == gold_sheet.get("header_row"),
        "semantic_columns_total": total,
        "semantic_columns_matched": matched,
        "semantic_role_f1": round(f1, 4),
        "name_price_columns_exact": proposed_mandatory == gold_mandatory,
    }


def choose_winner(summary: dict[str, dict[str, Any]]) -> str | None:
    """Select by safety/quality first and latency only as a tie-breaker."""

    if not summary:
        return None
    return max(
        summary,
        key=lambda model: (
            int(summary[model].get("valid_cases", 0)),
            float(summary[model].get("mean_semantic_role_f1", 0.0)),
            -float(summary[model].get("mean_latency_seconds", float("inf"))),
        ),
    )


def summarize_by_sample_rows(
    results: list[dict[str, Any]], *, row_counts: tuple[int, ...]
) -> dict[int, dict[str, Any]]:
    """Aggregate identical-model benchmark arms and compare them with one row."""

    summary: dict[int, dict[str, Any]] = {}
    for row_count in row_counts:
        rows = [row for row in results if row.get("sample_rows_requested") == row_count]
        timed = [row for row in rows if "latency_seconds" in row]
        summary[row_count] = {
            "cases": len(rows),
            "extractor_valid_cases": sum(bool(row.get("extractor_valid")) for row in rows),
            "valid_cases": sum(bool(row.get("valid")) for row in rows),
            "schema_valid_cases": sum(bool(row.get("schema_valid")) for row in rows),
            "compiler_valid_cases": sum(bool(row.get("compiler_valid")) for row in rows),
            "dry_run_valid_cases": sum(bool(row.get("dry_run_valid")) for row in rows),
            "exact_name_price_cases": sum(
                bool(row.get("name_price_columns_exact")) for row in rows
            ),
            "mean_semantic_role_f1": round(
                statistics.fmean(float(row.get("semantic_role_f1", 0.0)) for row in rows),
                4,
            )
            if rows
            else 0.0,
            "mean_latency_seconds": round(
                statistics.fmean(float(row["latency_seconds"]) for row in timed), 3
            )
            if timed
            else 0.0,
        }
    baseline = summary.get(1, {"valid_cases": 0, "mean_semantic_role_f1": 0.0})
    for row_count, values in summary.items():
        values["valid_cases_delta_vs_one"] = (
            values["valid_cases"] - baseline["valid_cases"]
        )
        values["semantic_role_f1_delta_vs_one"] = round(
            values["mean_semantic_role_f1"] - baseline["mean_semantic_role_f1"], 4
        )
    return summary


def choose_sample_rows_winner(summary: dict[int, dict[str, Any]]) -> int | None:
    """Choose quality first; use the smaller context only after an exact tie."""

    if not summary:
        return None
    return max(
        summary,
        key=lambda row_count: (
            int(summary[row_count].get("valid_cases", 0)),
            float(summary[row_count].get("mean_semantic_role_f1", 0.0)),
            -row_count,
        ),
    )
