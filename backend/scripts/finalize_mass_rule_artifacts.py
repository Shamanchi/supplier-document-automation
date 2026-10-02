#!/usr/bin/env python3
"""Merge targeted row audits and rebuild acceptance aggregates from requests.

The acceptance runner intentionally preserves all checkpointed request records
when a restricted retry is executed.  Its top-level ``prices`` and ``totals``
then describe only that retry.  This utility reconstructs the all-file summary
from the preserved request records and the final row audit without calling an
LLM or changing any proposal.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


AUDIT_TOTAL_FIELDS = (
    "nonempty_physical_rows",
    "parsed_items",
    "parsed_item_rows",
    "unique_item_rows",
    "multi_item_physical_rows",
    "extractor_contexts",
    "panel_contexts",
    "logical_candidate_rows",
    "panel_physical_candidate_rows",
    "panel_logical_rows",
    "unique_extractor_header_rows",
    "header_rows",
    "catalog_without_price_rows",
    "not_price_section_rows",
    "parser_issue_rows",
    "pending_model_rows",
    "pending_model_candidate_rows",
    "pending_rows_already_parsed",
    "unclassified_rows",
    "unclassified_price_like_rows",
)


def merge_audits(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    by_price = {row["price"]: row for row in base.get("prices", [])}
    by_price.update({row["price"]: row for row in overrides.get("prices", [])})
    prices = [by_price[key] for key in sorted(by_price)]
    totals = {
        field: sum(int(row.get(field) or 0) for row in prices)
        for field in AUDIT_TOTAL_FIELDS
    }
    totals["prices"] = len(prices)
    totals["source_errors"] = sum(bool(row.get("source_error")) for row in prices)
    return {
        "generated_on": overrides.get("generated_on") or base.get("generated_on"),
        "method": base.get("method"),
        "prices": prices,
        "totals": totals,
    }


def telemetry_totals(requests: list[dict[str, Any]]) -> dict[str, Any]:
    events: dict[str, dict[str, Any]] = {}
    historical_operation_ids: set[str] = set()
    for request in requests:
        candidates = list(request.get("telemetry_history") or [])
        historical_operation_ids.update(
            str(event.get("operation_id"))
            for event in candidates
            if isinstance(event, dict) and event.get("operation_id")
        )
        if request.get("telemetry"):
            candidates.append(request["telemetry"])
        for event in candidates:
            operation_id = str(event.get("operation_id") or "")
            if operation_id:
                events[operation_id] = event
    usage_fields = (
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_input_tokens",
        "billable_input_tokens",
    )
    usage = {
        field: sum(int((event.get("usage") or {}).get(field) or 0) for event in events.values())
        for field in usage_fields
    }
    return {
        "llm_operations": len(events),
        "llm_event_records": len(events),
        "llm_http_requests": sum(
            int(event.get("http_attempts") or 0) > 0 for event in events.values()
        ),
        "llm_http_attempts": sum(
            int(event.get("http_attempts") or 0) for event in events.values()
        ),
        "llm_transport_retries": sum(
            int(event.get("retry_count") or 0) for event in events.values()
        ),
        "llm_preflight_failures": sum(
            int(event.get("http_attempts") or 0) == 0
            and bool(event.get("error_type"))
            for event in events.values()
        ),
        "llm_historical_regenerations": len(historical_operation_ids),
        "duration_ms": sum(
            int(event.get("duration_ms") or 0) for event in events.values()
        ),
        "usage": usage,
    }


def rebuild_acceptance(
    acceptance: dict[str, Any], audit: dict[str, Any]
) -> dict[str, Any]:
    requests = acceptance.get("requests", [])
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for request in requests:
        grouped[str(request["price"])].append(request)
    audit_by_price = {row["price"]: row for row in audit.get("prices", [])}
    prices: list[dict[str, Any]] = []
    for price in sorted(set(grouped) | set(audit_by_price)):
        rows = grouped.get(price, [])
        passed = sum(row.get("passed") is True for row in rows)
        failed = len(rows) - passed
        checked = audit_by_price.get(price, {})
        prices.append(
            {
                "price": price,
                "sections": len(rows),
                "attempted": len(rows),
                "passed": passed,
                "failed": failed,
                "pending": 0,
                "item_count": int(checked.get("parsed_items") or 0),
                "batch_dry_run_passed": bool(rows) and failed == 0,
                "source_error": checked.get("source_error"),
            }
        )
    telemetry = telemetry_totals(requests)
    adjustment = acceptance.get("telemetry_adjustment") or {}
    adjustment_operations = int(adjustment.get("operation_count") or 0)
    telemetry["llm_operations"] += adjustment_operations
    telemetry["llm_event_records"] += adjustment_operations
    # The adjustment is explicitly a paid live operation removed by a source
    # migration, not a local preflight failure.
    telemetry["llm_http_requests"] += adjustment_operations
    telemetry["llm_http_attempts"] += adjustment_operations
    telemetry["duration_ms"] += int(adjustment.get("duration_ms") or 0)
    usage = telemetry["usage"]
    for field in usage:
        usage[field] += int((adjustment.get("usage") or {}).get(field) or 0)
    total_passed = sum(row.get("passed") is True for row in requests)
    acceptance["prices"] = prices
    acceptance["totals"] = {
        "sections": len(requests),
        "attempted": len(requests),
        "passed": total_passed,
        "failed": len(requests) - total_passed,
        "pending": 0,
        "prices_batch_passed": sum(row["batch_dry_run_passed"] for row in prices),
        "prices_selected": len(prices),
        "prices_loaded": sum(not row.get("source_error") for row in prices),
        "prices_source_error": sum(bool(row.get("source_error")) for row in prices),
        "duration_ms": telemetry["duration_ms"],
        "llm_operations": telemetry["llm_operations"],
        "llm_event_records": telemetry["llm_event_records"],
        "llm_http_requests": telemetry["llm_http_requests"],
        "llm_http_attempts": telemetry["llm_http_attempts"],
        "llm_transport_retries": telemetry["llm_transport_retries"],
        "llm_preflight_failures": telemetry["llm_preflight_failures"],
        "llm_historical_regenerations": telemetry["llm_historical_regenerations"],
        "usage": usage,
    }
    return acceptance


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--acceptance", type=Path, required=True)
    parser.add_argument("--base-audit", type=Path, required=True)
    parser.add_argument("--override-audit", type=Path, required=True)
    parser.add_argument("--output-audit", type=Path, required=True)
    parser.add_argument("--telemetry-adjustment", type=Path)
    args = parser.parse_args()

    acceptance = json.loads(args.acceptance.read_text(encoding="utf-8"))
    if args.telemetry_adjustment:
        acceptance["telemetry_adjustment"] = json.loads(
            args.telemetry_adjustment.read_text(encoding="utf-8")
        )
    base = json.loads(args.base_audit.read_text(encoding="utf-8"))
    overrides = json.loads(args.override_audit.read_text(encoding="utf-8"))
    audit = merge_audits(base, overrides)
    acceptance = rebuild_acceptance(acceptance, audit)
    args.output_audit.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.acceptance.write_text(
        json.dumps(acceptance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"acceptance": acceptance["totals"], "audit": audit["totals"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
