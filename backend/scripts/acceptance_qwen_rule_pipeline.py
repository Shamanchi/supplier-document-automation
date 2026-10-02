#!/usr/bin/env python3
"""Checkpointed full-context LLM acceptance for reviewed or full corpora."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR.parent
DEFAULT_CORPUS = APP_DIR.parents[1] / "external_price_corpus"
DEFAULT_CATALOG_SOURCE = APP_DIR / "reports/аудит_признаков_позиций_10_прайсов_2026-07-15.json"
DEFAULT_JSON = APP_DIR / "reports/acceptance_qwen_rules_10_prices_2026-07-15.json"
DEFAULT_MD = APP_DIR / "reports/acceptance_qwen_rules_10_prices_2026-07-15.md"
FILES = [
    "xls/004_Global_Security_KZ.xls",
    "xls/015_UNFIRE01.xls",
    "xls/032_Интернет-магазин_пожарного_обо.xls",
    "xls/043_НПО_Сибирский_Арсенал.xls",
    "xls/054_Пождело_оборудование.xls",
    "xlsx/005_gefest_orositeli.xlsx",
    "xlsx/043_Гефест.xlsx",
    "xlsx/047_Комплектоборудование_KZ.xlsx",
    "xlsx/054_МЧС01_ДВ.xlsx",
    "xlsx/058_ПироХимУрал.xlsx",
]


def _generation_count(row: dict[str, Any] | None) -> int:
    if not row:
        return 0
    history = row.get("telemetry_history")
    return int(isinstance(row.get("telemetry"), dict)) + (
        len(history) if isinstance(history, list) else 0
    )


def _should_schedule_model_call(
    *,
    cached: dict[str, Any] | None,
    cached_passed: bool,
    retry_failed: bool,
    max_generations_per_section: int,
) -> tuple[bool, str]:
    """Prevent accidental re-billing of failed sections across CLI resumes."""

    if cached_passed:
        return False, "passed"
    if cached is None:
        return True, "missing"
    if not retry_failed:
        return False, "cached_failed_retry_disabled"
    if _generation_count(cached) >= max_generations_per_section:
        return False, "generation_cap_reached"
    return True, "explicit_retry"


def _generation_history_entry(row: dict[str, Any]) -> dict[str, Any] | None:
    """Retain the compiler reason that caused a paid regeneration.

    Request telemetry alone can prove cost but cannot explain why a successful
    HTTP response was rejected.  Keep a bounded, content-free outcome beside
    the old operation before the checkpoint row is replaced.
    """

    telemetry = row.get("telemetry")
    if not isinstance(telemetry, dict):
        return None
    proposal = row.get("proposal") if isinstance(row.get("proposal"), dict) else {}
    return {
        "operation_id": telemetry.get("operation_id"),
        "passed": bool(row.get("passed")),
        "error": row.get("error"),
        "proposal_decision": proposal.get("decision"),
        "validation": row.get("validation"),
        "request_key": row.get("key"),
    }


def _require_paid_work_api_key(
    *, work_count: int, api_key: str | None, provider: str
) -> None:
    """Fail once before concurrency can fan one config error out per section."""

    if work_count and not api_key:
        raise RuntimeError(
            f"LLM provider {provider!r} has no API key; "
            "refusing to enqueue per-section configuration failures"
        )


def _selected_files(args: argparse.Namespace) -> list[str]:
    if not args.all_spreadsheets:
        discovered = list(FILES)
    else:
        roots = ("xls", "xlsx", "other")
        discovered = sorted(
            path.relative_to(args.corpus).as_posix()
            for root in roots
            for path in (args.corpus / root).glob("*")
            if path.is_file() and path.suffix.casefold() in {".xls", ".xlsx"}
        )
        if args.exclude_reviewed_ten:
            reviewed = set(FILES)
            discovered = [path for path in discovered if path not in reviewed]
    if args.restrict_load_to_price:
        selected = set(args.price_paths or [])
        discovered = [path for path in discovered if path in selected]
    return discovered


def _catalog(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    by_code: dict[str, dict[str, Any]] = {}
    for candidate in payload.get("candidates", []):
        if (
            candidate.get("business_class") != "product_characteristic"
            or candidate.get("storage_target") != "item_attribute_value"
        ):
            continue
        code = str(candidate.get("code") or "").strip()
        if not code:
            continue
        by_code[code] = {
            "code": code,
            "display_name": str(candidate.get("name_ru") or code),
            "value_kind": str(candidate.get("value_kind") or "text"),
            "canonical_unit": candidate.get("canonical_unit"),
        }
    return [by_code[code] for code in sorted(by_code)]


def _write_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _source_compatible_seed(
    relative_path: str,
    context: dict[str, Any],
    request_indexes: list[dict[tuple[str, str], list[dict[str, Any]]]],
) -> dict[str, Any] | None:
    """Match an older checkpoint by bounded source evidence, not prompt hash."""

    section_start = int(context["section_start_row_index"])
    section_end = int(context["section_end_row_index"])
    source_header_rows = {
        int(row_index)
        for row_index in context.get("column_header_row_indexes", [])
        if isinstance(row_index, int)
    }
    allowed_decisions = {
        "price_rows": {"propose"},
        "name_rows_without_price": {"propose", "catalog_without_price"},
        "ambiguous_tabular_rows": {"propose", "not_price_section"},
    }.get(str(context.get("section_kind")), {"propose"})
    # Later --seed-json arguments override earlier checkpoints.
    source_key = (relative_path, str(context["sheet_name"]))
    for request_index in reversed(request_indexes):
        requests = request_index.get(source_key, [])
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in requests:
            seed_header = row.get("header_row_index")
            if (
                seed_header != context.get("header_row_index")
                and seed_header not in source_header_rows
            ):
                continue
            proposal = row.get("proposal")
            if not isinstance(proposal, dict) or proposal.get("decision") not in allowed_decisions:
                continue
            evidence_rows = {
                int(evidence["row_index"])
                for evidence in proposal.get("evidence", [])
                if isinstance(evidence, dict) and isinstance(evidence.get("row_index"), int)
            }
            interior = {index for index in evidence_rows if section_start <= index < section_end}
            exact_section = row.get("section_id") == context.get("section_id")
            if not interior and not exact_section:
                continue
            score = 10 * len(interior) + (1000 if exact_section else 0)
            mapping = proposal.get("mapping") or {}
            if mapping.get("header_row_index") == context.get("header_row_index"):
                score += 5
            scored.append((score, row))
        if not scored:
            continue
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            top = [row for score, row in scored if score == scored[0][0]]
            mappings = {
                json.dumps(row["proposal"].get("mapping"), sort_keys=True, default=str)
                for row in top
            }
            if len(mappings) != 1:
                return None
        return scored[0][1]
    return None


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Acceptance: LLM → правило → compiler → dry-run",
        "",
        f"Сформировано: `{payload['generated_at']}`.",
        f"Модель: `{payload['model']}`.",
        f"Провайдер: `{payload['provider']}`.",
        "",
        "| Прайс | Секций | Проверено | Успешно | Ошибки | Осталось | Итоговый dry-run | Позиции |",
        "|---|---:|---:|---:|---:|---:|---|---:|",
    ]
    for row in payload.get("prices", []):
        lines.append(
            f"| `{row['price']}` | {row['sections']} | {row['attempted']} | "
            f"{row['passed']} | {row['failed']} | {row['pending']} | "
            f"{'да' if row.get('batch_dry_run_passed') else 'нет'} | "
            f"{row.get('item_count', 0)} |"
        )
    totals = payload.get("totals", {})
    usage = totals.get("usage", {})
    lines.extend(
        [
            "",
            "## Итоги",
            "",
            f"- секций: **{totals.get('sections', 0)}**;",
            f"- проверено моделью: **{totals.get('attempted', 0)}**;",
            f"- прошли: **{totals.get('passed', 0)}**;",
            f"- не прошли: **{totals.get('failed', 0)}**;",
            f"- ещё не запускались: **{totals.get('pending', 0)}**;",
            f"- input tokens: **{usage.get('input_tokens', 0)}**;",
            f"- output tokens: **{usage.get('output_tokens', 0)}**;",
            f"- cached input tokens: **{usage.get('cached_input_tokens', 0)}**;",
            f"- billable input tokens: **{usage.get('billable_input_tokens', 0)}**;",
            f"- суммарная request latency: **{totals.get('duration_ms', 0)} мс**.",
            "",
            "Полные предложения, ошибки compiler и structured telemetry каждой операции находятся в JSON.",
        ]
    )
    return "\n".join(lines) + "\n"


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    sys.path.insert(0, str(BACKEND_DIR))
    from app.config import settings
    from app.llm_providers import resolve_rule_model_config
    from app.llm_telemetry import configure_llm_telemetry_jsonl
    from app.parsing import loaders
    from app.parsing.mapping import MappingDecision
    from app.parsing.rule_proposal import (
        build_tabular_rule_repair_prompt,
        build_tabular_rule_generation_prompt,
        build_tabular_section_contexts,
        compile_batch_proposals,
        compile_section_proposal,
        dry_run_decision,
        parse_proposal,
        prompt_hash,
    )
    from app.rule_proposal_service import _request_rule_proposal_result

    telemetry_jsonl = args.telemetry_jsonl or args.output_json.with_suffix(
        ".telemetry.jsonl"
    )
    configure_llm_telemetry_jsonl(telemetry_jsonl)

    catalog = _catalog(args.catalog_source)
    request_config = resolve_rule_model_config(
        provider=args.provider,
        model=args.model,
        temperature=args.temperature,
        top_p=args.top_p,
        max_output_tokens=args.max_output_tokens,
        timeout_seconds=args.timeout_seconds,
        thinking=args.thinking,
        reasoning_effort=args.reasoning_effort,
    )
    model = request_config.model
    selected_files = _selected_files(args)
    existing: dict[str, Any] = {}
    if args.output_json.exists() and not args.fresh:
        existing = json.loads(args.output_json.read_text(encoding="utf-8"))
    results: dict[str, dict[str, Any]] = {
        row["key"]: row for row in existing.get("requests", []) if row.get("key")
    }
    existing_request_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in results.values():
        if not isinstance(row.get("proposal"), dict):
            continue
        source_key = (str(row.get("price") or ""), str(row.get("sheet_name") or ""))
        existing_request_index.setdefault(source_key, []).append(row)
    seed_request_indexes: list[dict[tuple[str, str], list[dict[str, Any]]]] = [
        existing_request_index
    ]
    for seed_path in args.seed_json:
        seed_payload = json.loads(seed_path.read_text(encoding="utf-8"))
        request_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in seed_payload.get("requests", []):
            if not isinstance(row.get("proposal"), dict):
                continue
            key = (str(row.get("price") or ""), str(row.get("sheet_name") or ""))
            request_index.setdefault(key, []).append(row)
        seed_request_indexes.append(request_index)

    sources: dict[str, tuple[str, list, list[dict[str, Any]]]] = {}
    source_errors: dict[str, str] = {}
    work: list[tuple[str, str, list, dict[str, Any], str]] = []
    scheduling: Counter[str] = Counter()
    for relative_path in selected_files:
        source = args.corpus / relative_path
        try:
            data = source.read_bytes()
            loader = loaders.detect_loader(data, source.name)
            grids = loaders.load_grids_for(loader, data, source.name)
            contexts = build_tabular_section_contexts(grids)
        except Exception as exc:
            source_errors[relative_path] = " ".join(str(exc).split())[:1000]
            continue
        sources[relative_path] = (loader, grids, contexts)
        current_source_keys: set[str] = set()
        for context in contexts:
            if args.section_kind and context.get("section_kind") != args.section_kind:
                continue
            if args.section_ids and context.get("section_id") not in args.section_ids:
                continue
            prompt = build_tabular_rule_generation_prompt(
                source_context=context,
                loader=loader,
                model=model,
                attribute_catalog=catalog,
            )
            key = f"{relative_path}::{context['section_id']}::{prompt_hash(prompt)}"
            current_source_keys.add(key)
            cached = results.get(key)
            if cached is None:
                seed = _source_compatible_seed(
                    relative_path, context, seed_request_indexes
                )
                if seed is not None:
                    old_key = str(seed.get("key") or "")
                    if old_key != key and results.get(old_key) is seed:
                        # Extractor contract changes legitimately alter the
                        # derived key.  Move the compatible checkpoint row
                        # instead of retaining a stale duplicate that would
                        # inflate per-file attempted/failed totals.
                        results.pop(old_key, None)
                    migrated_proposal = dict(seed["proposal"])
                    migrated_proposal["source_section_id"] = context["section_id"]
                    if isinstance(migrated_proposal.get("mapping"), dict):
                        migrated_proposal["mapping"] = {
                            **migrated_proposal["mapping"],
                            "sheet_name": context["sheet_name"],
                            "header_row_index": context["header_row_index"],
                        }
                    cached = {
                        "key": key,
                        "price": relative_path,
                        "section_id": context["section_id"],
                        "sheet_name": context["sheet_name"],
                        "header_row_index": context["header_row_index"],
                        "proposal": migrated_proposal,
                        "passed": False,
                        "seeded_from": [str(path) for path in args.seed_json],
                    }
                    # Preserve paid-operation telemetry across prompt/compiler
                    # migrations.  If this proposal needs a repair, ``one``
                    # moves it into telemetry_history before storing the new
                    # call, so final usage remains the real end-to-end cost.
                    if isinstance(seed.get("telemetry"), dict):
                        cached["telemetry"] = seed["telemetry"]
                    if isinstance(seed.get("telemetry_history"), list):
                        cached["telemetry_history"] = list(seed["telemetry_history"])
                    if isinstance(seed.get("generation_history"), list):
                        cached["generation_history"] = list(seed["generation_history"])
                    results[key] = cached
            cached_passed = False
            if cached is not None and isinstance(cached.get("proposal"), dict):
                # Compiler/parser changes must be testable without paying for
                # the same deterministic model response again.
                try:
                    proposal = parse_proposal(cached["proposal"])
                    compiled = compile_section_proposal(
                        proposal, source_context=context, loader=loader, grids=grids
                    )
                    if compiled.table is None:
                        cached["validation"] = compiled.validation
                        cached["passed"] = bool(compiled.validation.get("passed"))
                        if cached["passed"]:
                            cached.pop("error", None)
                            cached_passed = True
                        else:
                            cached["error"] = f"decision={proposal.decision}"
                    else:
                        _, validation = dry_run_decision(
                            grids, MappingDecision(tables=[compiled.table], auto_ok=True)
                        )
                        cached["validation"] = {**compiled.validation, **validation}
                        cached["passed"] = bool(validation["passed"])
                        if cached["passed"]:
                            cached.pop("error", None)
                            cached_passed = True
                        else:
                            cached["error"] = "section dry-run quality gate failed"
                except Exception as exc:
                    cached["passed"] = False
                    cached["error"] = " ".join(str(exc).split())[:1000]
            should_schedule, schedule_reason = _should_schedule_model_call(
                cached=cached,
                cached_passed=cached_passed,
                retry_failed=args.retry_failed,
                max_generations_per_section=args.max_generations_per_section,
            )
            scheduling[schedule_reason] += 1
            if should_schedule and (
                not args.price_paths or relative_path in args.price_paths
            ):
                if cached is not None and isinstance(cached.get("proposal"), dict):
                    prompt = build_tabular_rule_repair_prompt(
                        base_prompt=prompt,
                        previous_payload=cached["proposal"],
                        verifier_feedback={
                            "error": cached.get("error"),
                            "validation": cached.get("validation"),
                        },
                    )
                work.append((key, relative_path, grids, context, prompt))
        if not args.section_kind and not args.section_ids:
            for stale_key, stale_row in list(results.items()):
                if (
                    stale_row.get("price") == relative_path
                    and stale_key not in current_source_keys
                ):
                    results.pop(stale_key, None)
    if args.one_per_price:
        covered_prices = {
            str(row.get("price"))
            for row in results.values()
            if isinstance(row.get("proposal"), dict)
        }
        selected: list[tuple[str, str, list, dict[str, Any], str]] = []
        newly_covered: set[str] = set()
        for item in work:
            relative_path = item[1]
            if relative_path in covered_prices or relative_path in newly_covered:
                continue
            selected.append(item)
            newly_covered.add(relative_path)
        work = selected
    if args.max_requests is not None:
        work = work[: args.max_requests]
    _require_paid_work_api_key(
        work_count=len(work),
        api_key=request_config.api_key,
        provider=request_config.provider,
    )
    target_completed = len(results) + len(work)

    semaphore = asyncio.Semaphore(args.concurrency)
    checkpoint_lock = asyncio.Lock()

    async def one(item: tuple[str, str, list, dict[str, Any], str]) -> None:
        key, relative_path, grids, context, prompt = item
        loader = sources[relative_path][0]
        row: dict[str, Any] = {
            "key": key,
            "price": relative_path,
            "section_id": context["section_id"],
            "sheet_name": context["sheet_name"],
            "header_row_index": context["header_row_index"],
            "passed": False,
        }
        async with semaphore:
            try:
                response = await _request_rule_proposal_result(
                    prompt=prompt,
                    model=model,
                    telemetry_dimensions={
                        "acceptance": (
                            "remaining_spreadsheets"
                            if args.all_spreadsheets and args.exclude_reviewed_ten
                            else "selected_spreadsheets"
                        ),
                        "price": relative_path,
                        "section_id": context["section_id"],
                    },
                    request_config=request_config,
                )
                row["proposal"] = response.payload
                row["telemetry"] = response.telemetry
                proposal = parse_proposal(response.payload)
                compiled = compile_section_proposal(
                    proposal, source_context=context, loader=loader, grids=grids
                )
                if compiled.table is None:
                    row["validation"] = compiled.validation
                    row["passed"] = bool(compiled.validation.get("passed"))
                    if not row["passed"]:
                        row["error"] = f"decision={proposal.decision}"
                else:
                    _, validation = dry_run_decision(
                        grids, MappingDecision(tables=[compiled.table], auto_ok=True)
                    )
                    row["validation"] = {**compiled.validation, **validation}
                    row["passed"] = bool(validation["passed"])
                    if not row["passed"]:
                        row["error"] = "section dry-run quality gate failed"
            except Exception as exc:  # retain a concise checkpoint diagnostic
                telemetry = getattr(exc, "telemetry", None)
                if telemetry:
                    row["telemetry"] = telemetry
                row["error"] = " ".join(str(exc).split())[:1000]
        async with checkpoint_lock:
            previous = results.get(key) or {}
            telemetry_history = list(previous.get("telemetry_history") or [])
            if previous.get("telemetry"):
                telemetry_history.append(previous["telemetry"])
            if telemetry_history:
                row["telemetry_history"] = telemetry_history
            generation_history = list(previous.get("generation_history") or [])
            history_entry = _generation_history_entry(previous)
            if history_entry is not None:
                generation_history.append(history_entry)
            if generation_history:
                row["generation_history"] = generation_history
            results[key] = row
            checkpoint = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "model": model,
                "provider": request_config.provider,
                "request_settings": request_config.persisted_settings(),
                "requests": list(results.values()),
            }
            _write_checkpoint(args.output_json, checkpoint)
            done = len(results)
            print(
                f"[{done}/{target_completed}] {relative_path} "
                f"{context['section_id']} passed={row['passed']}",
                flush=True,
            )

    tasks = [asyncio.create_task(one(item)) for item in work]
    if tasks:
        await asyncio.gather(*tasks)

    price_rows: list[dict[str, Any]] = []
    for relative_path in selected_files:
        if relative_path in source_errors:
            price_rows.append(
                {
                    "price": relative_path,
                    "sections": 0,
                    "attempted": 0,
                    "passed": 0,
                    "failed": 0,
                    "pending": 0,
                    "batch_dry_run_passed": False,
                    "item_count": 0,
                    "source_error": source_errors[relative_path],
                }
            )
            continue
        loader, grids, contexts = sources[relative_path]
        price_results = [
            row for row in results.values() if row.get("price") == relative_path
        ]
        passed = [row for row in price_results if row.get("passed")]
        failed = [row for row in price_results if not row.get("passed")]
        summary: dict[str, Any] = {
            "price": relative_path,
            "sections": len(contexts),
            "attempted": len(price_results),
            "passed": len(passed),
            "failed": len(failed),
            "pending": len(contexts) - len(price_results),
            "batch_dry_run_passed": False,
            "item_count": 0,
        }
        if len(passed) == len(contexts):
            by_section = {row["section_id"]: row for row in passed}
            decisions = {
                str(row.get("proposal", {}).get("decision") or "") for row in passed
            }
            if not contexts or decisions <= {"catalog_without_price", "not_price_section"}:
                # A readable supplier card/catalog with no price data is a
                # successful classification, not a failed parser run.  There
                # is intentionally no executable table and therefore zero
                # persisted positions.
                summary["batch_dry_run_passed"] = True
                summary["batch_validation"] = {
                    "passed": True,
                    "item_count": 0,
                    "table_count": 0,
                    "reason": "no executable price table",
                }
            else:
                try:
                    decision = compile_batch_proposals(
                        (
                            (by_section[context["section_id"]]["proposal"], context)
                            for context in contexts
                        ),
                        loader=loader,
                        grids=grids,
                    )
                    _, validation = dry_run_decision(grids, decision)
                    summary["batch_dry_run_passed"] = bool(validation["passed"])
                    summary["item_count"] = validation["item_count"]
                    summary["batch_validation"] = validation
                except Exception as exc:
                    summary["batch_error"] = " ".join(str(exc).split())[:1000]
        price_rows.append(summary)

    usage_totals: Counter[str] = Counter()
    duration_ms = 0
    seen_operations: set[str] = set()
    historical_operations: set[str] = set()
    llm_http_requests = 0
    llm_http_attempts = 0
    llm_transport_retries = 0
    llm_preflight_failures = 0
    for row in results.values():
        telemetry_events = list(row.get("telemetry_history") or [])
        for telemetry in telemetry_events:
            operation_id = str(telemetry.get("operation_id") or "")
            if operation_id:
                historical_operations.add(operation_id)
        telemetry_events.append(row.get("telemetry") or {})
        for telemetry in telemetry_events:
            operation_id = str(telemetry.get("operation_id") or "")
            if operation_id and operation_id in seen_operations:
                continue
            if operation_id:
                seen_operations.add(operation_id)
            http_attempts = int(telemetry.get("http_attempts") or 0)
            llm_http_requests += int(http_attempts > 0)
            llm_http_attempts += http_attempts
            llm_transport_retries += int(telemetry.get("retry_count") or 0)
            llm_preflight_failures += int(
                http_attempts == 0 and bool(telemetry.get("error_type"))
            )
            duration_ms += int(telemetry.get("duration_ms") or 0)
            for key, value in (telemetry.get("usage") or {}).items():
                if isinstance(value, int):
                    usage_totals[key] += value
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "provider": request_config.provider,
        "request_settings": request_config.persisted_settings(),
        "scope": {
            "all_spreadsheets": bool(args.all_spreadsheets),
            "exclude_reviewed_ten": bool(args.exclude_reviewed_ten),
            "selected_files": len(selected_files),
            "loaded_files": len(sources),
            "source_errors": len(source_errors),
            "telemetry_jsonl": str(telemetry_jsonl),
            "scheduling": dict(scheduling),
        },
        "catalog_size": len(catalog),
        "prices": price_rows,
        "totals": {
            "sections": sum(row["sections"] for row in price_rows),
            "attempted": sum(row["attempted"] for row in price_rows),
            "passed": sum(row["passed"] for row in price_rows),
            "failed": sum(row["failed"] for row in price_rows),
            "pending": sum(row["pending"] for row in price_rows),
            "prices_batch_passed": sum(bool(row["batch_dry_run_passed"]) for row in price_rows),
            "prices_selected": len(selected_files),
            "prices_loaded": len(sources),
            "prices_source_error": len(source_errors),
            "duration_ms": duration_ms,
            # Backward-compatible event count.  Do not describe it as retry:
            # it includes base requests, historical regenerations and local
            # preflight failures.
            "llm_operations": len(seen_operations),
            "llm_event_records": len(seen_operations),
            "llm_http_requests": llm_http_requests,
            "llm_http_attempts": llm_http_attempts,
            "llm_transport_retries": llm_transport_retries,
            "llm_preflight_failures": llm_preflight_failures,
            "llm_historical_regenerations": len(historical_operations),
            "usage": dict(usage_totals),
        },
        "requests": sorted(results.values(), key=lambda row: (row["price"], row["section_id"])),
    }
    _write_checkpoint(args.output_json, payload)
    args.output_md.write_text(_render_markdown(payload), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument(
        "--all-spreadsheets",
        action="store_true",
        help="Discover every .xls/.xlsx in corpus xls, xlsx and other directories.",
    )
    parser.add_argument(
        "--exclude-reviewed-ten",
        action="store_true",
        help="Exclude the ten reviewed benchmark files from --all-spreadsheets.",
    )
    parser.add_argument("--catalog-source", type=Path, default=DEFAULT_CATALOG_SOURCE)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--output-md", type=Path, default=DEFAULT_MD)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--temperature", type=float, default=0)
    parser.add_argument("--top-p", type=float, default=1)
    parser.add_argument("--max-output-tokens", type=int, default=None)
    parser.add_argument("--timeout-seconds", type=int, default=None)
    parser.add_argument("--thinking", choices=("enabled", "disabled"), default="disabled")
    parser.add_argument("--reasoning-effort", choices=("high", "max"), default="high")
    parser.add_argument(
        "--max-requests",
        type=int,
        default=None,
        help="Run only the next N missing calls; the next invocation resumes the checkpoint.",
    )
    parser.add_argument(
        "--one-per-price",
        action="store_true",
        help="Add one missing representative live call for every uncovered price.",
    )
    parser.add_argument(
        "--section-kind",
        choices=("price_rows", "name_rows_without_price", "ambiguous_tabular_rows"),
        default=None,
        help="Run only one extractor context kind (useful for focused acceptance).",
    )
    parser.add_argument(
        "--section-id",
        dest="section_ids",
        action="append",
        default=None,
        help="Run only this exact section id; repeat the flag for several sections.",
    )
    parser.add_argument(
        "--price",
        dest="price_paths",
        action="append",
        default=None,
        help="Limit paid/repaired calls to this corpus price; repeat for several files.",
    )
    parser.add_argument(
        "--restrict-load-to-price",
        action="store_true",
        help=(
            "Load/revalidate only paths supplied through --price. The checkpoint still "
            "retains requests for the rest of the corpus; use a final unrestricted local "
            "run to rebuild the complete per-file summary."
        ),
    )
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help=(
            "Explicitly regenerate cached failed sections. Without this flag a resume "
            "never creates another paid call for the same failed section."
        ),
    )
    parser.add_argument(
        "--max-generations-per-section",
        type=int,
        default=2,
        help="Hard cross-run cap including telemetry_history (default: 2).",
    )
    parser.add_argument(
        "--telemetry-jsonl",
        type=Path,
        default=None,
        help=(
            "Append every content-free LLM event immediately. Defaults to the output "
            "JSON path with suffix .telemetry.jsonl."
        ),
    )
    parser.add_argument(
        "--seed-json",
        type=Path,
        action="append",
        default=[],
        help=(
            "Revalidate source-compatible proposals from an older checkpoint before paying "
            "again; repeat to merge checkpoints, later files win."
        ),
    )
    args = parser.parse_args()
    if args.max_generations_per_section < 1:
        parser.error("--max-generations-per-section must be at least 1")
    result = asyncio.run(_run(args))
    print(json.dumps(result["totals"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
