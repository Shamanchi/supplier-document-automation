#!/usr/bin/env python3
"""Compare two checkpointed acceptance runs on the exact same source sections."""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR.parent
DEFAULT_CORPUS = APP_DIR.parents[1] / "external_price_corpus"


def _events(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *(event for event in (row.get("telemetry_history") or []) if event),
        *(event for event in [row.get("telemetry")] if event),
    ]


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    events = [event for row in rows for event in _events(row)]
    usage: Counter[str] = Counter()
    for event in events:
        for key, value in (event.get("usage") or {}).items():
            if isinstance(value, int):
                usage[key] += value
    durations = [int(event.get("duration_ms") or 0) for event in events]
    input_tokens = usage["input_tokens"]
    return {
        "sections": len(rows),
        "passed": sum(bool(row.get("passed")) for row in rows),
        "failed": sum(not bool(row.get("passed")) for row in rows),
        "operations": len(events),
        "application_retries": max(0, len(events) - len(rows)),
        "http_attempts": sum(int(event.get("http_attempts") or 0) for event in events),
        "http_retries": sum(int(event.get("retry_count") or 0) for event in events),
        "duration_ms": sum(durations),
        "latency_ms": {
            "mean": round(sum(durations) / len(durations), 2) if durations else 0,
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
            "max": max(durations, default=0),
        },
        "usage": dict(usage),
        "cache_hit_ratio": round(usage["cached_input_tokens"] / input_tokens, 6)
        if input_tokens
        else 0,
        "per_operation": {
            key: round(value / len(events), 2) if events else 0
            for key, value in {
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "total_tokens": usage["total_tokens"],
                "cached_input_tokens": usage["cached_input_tokens"],
                "billable_input_tokens": usage["billable_input_tokens"],
            }.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("qwen", type=Path)
    parser.add_argument("deepseek", type=Path)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    args = parser.parse_args()

    sys.path.insert(0, str(BACKEND_DIR))
    from app.parsing import loaders
    from app.parsing.rule_proposal import build_tabular_section_contexts, context_hash
    from scripts.acceptance_qwen_rule_pipeline import FILES

    qwen = json.loads(args.qwen.read_text(encoding="utf-8"))
    deepseek = json.loads(args.deepseek.read_text(encoding="utf-8"))
    deepseek_by_key = {
        (row["price"], row["section_id"]): row for row in deepseek["requests"]
    }

    legacy_to_current: dict[tuple[str, str], str] = {}
    for relative_path in FILES:
        source = args.corpus / relative_path
        data = source.read_bytes()
        loader = loaders.detect_loader(data, source.name)
        grids = loaders.load_grids_for(loader, data, source.name)
        for context in build_tabular_section_contexts(grids):
            if context.get("section_kind") != "price_rows":
                continue
            identity = {
                "sheet_index": context["sheet_index"],
                "sheet_name": context["sheet_name"],
                "header_row_index": context["header_row_index"],
                "section_start_row_index": context["section_start_row_index"],
                "section_end_row_index": context["section_end_row_index"],
                "category_row_indexes": context["category_row_indexes"],
                "header_values": context["header_values"],
            }
            legacy_id = "sec_" + context_hash(identity)[:20]
            legacy_to_current[(relative_path, legacy_id)] = context["section_id"]

    matched: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for row in qwen["requests"]:
        old_key = (row["price"], row["section_id"])
        current_id = legacy_to_current.get(old_key)
        current = deepseek_by_key.get((row["price"], current_id)) if current_id else None
        if current is None:
            missing.append({"price": row["price"], "section_id": row["section_id"]})
        else:
            matched.append(current)

    payload = {
        "matched_sections": len(matched),
        "missing_sections": missing,
        "qwen_same_sections": _aggregate(qwen["requests"]),
        "deepseek_same_sections": _aggregate(matched),
        "deepseek_full": _aggregate(deepseek["requests"]),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
