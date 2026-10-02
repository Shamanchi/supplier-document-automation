# -*- coding: utf-8 -*-
"""P0-1 Scenario A mini-smoke: real YandexGPT call through the standard path.

Creates a small in-memory price workbook whose sheets repeat the explicit
price headers from the colleagues' report (section 3): "Цена ОПТ", "ОПТ",
"Цена Розница", "Розница".  The file is loaded through the standard xlsx
loader (load_grids_for), bounded section specs are built with
build_rule_section_specs, and each section runs the same per-section model
loop that rule_proposal_service uses (initial call + at most one bounded
semantic repair).  Stub mode and deterministic fast path must be disabled
for a real run:

    docker exec -e PYTHONPATH=/srv \
        -e RULE_GENERATION_STUB_MODE=false \
        -e RULE_GENERATION_FAST_PATH_ENABLED=false \
        supplier-document-automation-price-api-1 \
        python /srv/scripts/smoke_p0_1_real_llm_scenario_a.py [--dry-run] [--sheet "<title>"]

No existing project code is modified, no DB rows are written, and paid calls
are hard-capped (initial per section + at most one repair, 4 total).

Exit codes:
    0 PASS   - real model answers compiled and every checked explicit price
               header kept its deterministic price_kind (0 errors)
    1 FAIL   - a checked price_kind was wrong, or no compiled proof was
               obtained for any target header
    2 SKIP   - no YANDEXGPT_API_KEY configured
    3 ABORT  - process env not prepared (stub/fast-path still enabled or
               provider is not yandexgpt)
    4 ERROR  - provider/network/key/quota error recorded, no paid success
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import time

from openpyxl import Workbook

from app.config import settings
from app.llm_providers import resolve_rule_model_config
from app.parsing.loaders import load_grids_for
from app.parsing.mapping import MappingDecision
from app.parsing.rule_proposal import ProposalError, dry_run_decision
from app.parsing.rule_proposal_handlers import (
    build_rule_section_specs,
    handler_for_contract,
)
from app.rule_proposal_service import (
    RuleModelError,
    _request_rule_proposal_result,
)

MAX_SECTIONS = 3
PAID_CALL_BUDGET = 4
LOADER = "xlsx"
CONCRETE_KINDS = ("wholesale", "retail", "dealer", "purchase")

TARGET_HEADERS = {
    "цена опт": "wholesale",
    "опт": "wholesale",
    "цена розница": "retail",
    "розница": "retail",
}


def build_workbook_bytes() -> bytes:
    """Small real xlsx: two sheets, explicit price headers from report sec. 3."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Прайс Supplier A"
    ws.append(["Наименование", "Цена ОПТ", "Цена Розница"])
    rows1 = [
        ("Клапан пожарный КВЧ-50", 618.40, 834.84),
        ("Гидрант пожарный ГП-100", 4120.00, 5210.00),
        ("Рукав пожарный РПК-50", 235.00, 310.50),
        ("Ствол пожарный СР-70", 690.00, 890.00),
        ("Задвижка ЗКВЧ-80", 3500.00, 4600.00),
        ("Кран пожарный КП-50", 980.00, 1250.00),
        ("Колонка пожарная КПА", 2100.00, 2750.00),
        ("Генератор пены ГПС-600", 2400.00, 3100.00),
    ]
    for row in rows1:
        ws.append(list(row))

    ws2 = wb.create_sheet("Прайс кратко")
    ws2.append(["Наименование", "ОПТ", "Розница"])
    rows2 = [
        ("Клапан КВЧ", 618.40, 834.84),
        ("Гидрант ГП-100", 4120.00, 5210.00),
        ("Рукав РПК-50", 235.00, 310.50),
        ("Ствол СР-70", 690.00, 890.00),
    ]
    for row in rows2:
        ws2.append(list(row))

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def normalized(value: object) -> str:
    text = "" if value is None else str(value)
    return " ".join(text.lower().replace("ё", "е").split())


def expected_from_context(context: dict) -> dict[int, dict]:
    """Deterministic price observations (the P0-1 authoritative source)."""
    result: dict[int, dict] = {}
    for obs in context.get("deterministic_observations") or []:
        if not isinstance(obs, dict):
            continue
        if obs.get("suggested_role") != "price":
            continue
        descriptor = obs.get("price_descriptor")
        if not isinstance(descriptor, dict):
            continue
        kind = descriptor.get("kind")
        if kind not in CONCRETE_KINDS:
            continue
        index = obs.get("index")
        if not isinstance(index, int):
            continue
        result[index] = {
            "kind": kind,
            "header": str(obs.get("header") or ""),
            "source_header_row_index": obs.get("source_header_row_index"),
        }
    return result


def model_kinds_from_payload(payload: object) -> dict[int, str]:
    """Structured price_kind the model itself returned for each column."""
    result: dict[int, str] = {}
    if not isinstance(payload, dict):
        return result
    mapping = payload.get("mapping")
    if not isinstance(mapping, dict):
        return result
    for column in mapping.get("columns") or []:
        if not isinstance(column, dict):
            continue
        descriptor = column.get("price_descriptor")
        if column.get("role") == "price" and isinstance(descriptor, dict):
            index = column.get("index")
            if isinstance(index, int):
                result[index] = str(descriptor.get("kind") or "")
    return result


def payload_summary(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {"shape": type(payload).__name__}
    return {
        "decision": payload.get("decision"),
        "requires_manual_review": payload.get("requires_manual_review"),
        "confidence": payload.get("confidence"),
        "rule_name": str(payload.get("rule_name") or "")[:60],
    }


def price_columns_from_compiled(compiled) -> list[dict]:
    if compiled is None or compiled.table is None:
        return []
    rows = []
    for column in compiled.table.columns:
        if column.role != "price":
            continue
        descriptor = column.price_descriptor or {}
        rows.append(
            {
                "index": column.index,
                "header": column.header or "",
                "kind": descriptor.get("kind"),
            }
        )
    return rows


async def run_one_section(
    *,
    spec: dict,
    grids,
    request_config,
    model: str,
    budget_left: int,
) -> dict:
    handler = handler_for_contract(spec["proposal_contract"])
    context = spec["input_context"]
    base_prompt = spec["prompt"]
    prompt = base_prompt
    calls = 0
    compiled = None
    validation = None
    raw_payload = None
    raw_kinds: dict[int, str] = {}
    error = None
    feedback = None
    calls_log: list[dict] = []

    for correction in range(2):
        if calls >= budget_left:
            break
        started = time.monotonic()
        try:
            result = await _request_rule_proposal_result(
                prompt=prompt,
                model=model,
                request_config=request_config,
                proposal_contract=spec["proposal_contract"],
                telemetry_dimensions={
                    "smoke": "scenario-a",
                    "section_id": str(spec["section_id"]),
                },
                source_context=context,
                loader=LOADER,
            )
        except RuleModelError as exc:
            telemetry = getattr(exc, "telemetry", None) or {}
            calls_log.append(
                {
                    "attempt": correction + 1,
                    "repair": correction > 0,
                    "error": f"RuleModelError: {exc}",
                    "duration_ms": telemetry.get("duration_ms"),
                    "http_status": telemetry.get("http_status_code"),
                    "response_id": telemetry.get("response_id"),
                    "usage": telemetry.get("usage"),
                }
            )
            return {
                "section_id": spec["section_id"],
                "calls": calls + 1,
                "error": f"RuleModelError: {exc}",
                "compiled": None,
                "validation": None,
                "raw_payload": None,
                "raw_kinds": {},
                "calls_log": calls_log,
            }
        calls += 1
        latency_ms = (time.monotonic() - started) * 1000.0
        telemetry = result.telemetry or {}
        raw_payload = result.payload
        raw_kinds = model_kinds_from_payload(raw_payload)
        calls_log.append(
            {
                "attempt": correction + 1,
                "repair": correction > 0,
                "duration_ms": telemetry.get("duration_ms") or round(latency_ms),
                "http_status": telemetry.get("http_status_code"),
                "finish_reason": telemetry.get("finish_reason"),
                "response_id": telemetry.get("response_id"),
                "usage": telemetry.get("usage"),
                "payload": payload_summary(raw_payload),
                "model_kinds": dict(raw_kinds),
            }
        )
        accepted = False
        try:
            proposal = handler.parse(raw_payload)
            compiled = handler.compile(
                proposal,
                source_context=context,
                loader=LOADER,
                grids=grids,
            )
            validation = None
            if compiled.table is not None:
                decision = MappingDecision(tables=[compiled.table], auto_ok=True)
                _, validation = dry_run_decision(grids, decision)
            if validation is not None:
                accepted = bool(validation.get("passed"))
                feedback = validation
            else:
                accepted = bool(compiled.validation.get("passed"))
                feedback = compiled.validation
        except (ProposalError, ValueError) as exc:
            accepted = False
            feedback = {"source_compiler_error": str(exc)[:800]}
            error = str(exc)
            calls_log[-1]["compiler_error"] = str(exc)[:300]
            if correction == 1:
                break
        if accepted or correction == 1:
            if accepted:
                error = None
            break
        prompt = handler.build_repair_prompt(
            base_prompt=base_prompt,
            previous_payload=raw_payload,
            verifier_feedback=feedback,
        )

    return {
        "section_id": spec["section_id"],
        "calls": calls,
        "error": error,
        "compiled": compiled,
        "validation": validation,
        "raw_payload": raw_payload,
        "raw_kinds": raw_kinds,
        "calls_log": calls_log,
    }


def main() -> int:
    argv = list(sys.argv[1:])
    dry_run = "--dry-run" in argv
    sheet_filter = None
    if "--sheet" in argv:
        index = argv.index("--sheet")
        if index + 1 >= len(argv):
            print("ABORT: --sheet требует значение")
            return 3
        sheet_filter = argv[index + 1]
    request_config = resolve_rule_model_config(provider="yandexgpt")

    key = settings.yandexgpt_api_key or ""
    print("=== P0-1 Scenario A: smoke реального LLM (YandexGPT) ===")
    print(f"provider={settings.llm_provider} stub={settings.rule_generation_stub_mode}")
    print(f"fast_path={settings.rule_generation_fast_path_enabled} dry_run={dry_run}")
    if settings.llm_provider.casefold() != "yandexgpt":
        print("ABORT: LLM_PROVIDER != yandexgpt")
        return 3
    if not key:
        print("SKIP: YANDEXGPT_API_KEY не задан")
        return 2
    if settings.rule_generation_stub_mode:
        print("ABORT: RULE_GENERATION_STUB_MODE должен быть false")
        return 3
    if settings.rule_generation_fast_path_enabled:
        print("ABORT: RULE_GENERATION_FAST_PATH_ENABLED должен быть false")
        return 3
    print(f"ключ: len={len(key)}, prefix={key[:4]}… (секрет не печатаем)")
    print(f"folder_id={settings.yandexgpt_folder_id}")
    print(f"модель (на проводе): {request_config.model}")
    endpoint = request_config.api_base_url.rstrip("/") + "/chat/completions"
    print(f"endpoint: {endpoint}")
    print(
        f"timeout_seconds={request_config.timeout_seconds} "
        f"max_output_tokens={request_config.max_output_tokens}"
    )

    grids = load_grids_for(LOADER, build_workbook_bytes(), "smoke_p0_1_mini.xlsx")
    if sheet_filter:
        grids = [grid for grid in grids if grid.name == sheet_filter]
        if not grids:
            print(f"ABORT: лист {sheet_filter!r} не найден")
            return 3
    for grid in grids:
        print(f"лист: {grid.name!r} rows={len(grid.rows)} cols={grid.n_cols}")

    specs = build_rule_section_specs(
        grids=grids, loader=LOADER, model=request_config.model
    )
    if not specs:
        print("ABORT: ни одной секции для генерации (макет mini-файла не подошёл)")
        return 3
    specs = specs[:MAX_SECTIONS]
    print(f"секций для модели: {len(specs)}")

    if dry_run:
        for spec in specs:
            expected = expected_from_context(spec["input_context"])
            for index, info in sorted(expected.items()):
                norm = normalized(info["header"])
                target = TARGET_HEADERS.get(norm)
                marker = "TARGET" if target else "other"
                print(
                    f"  детерм. наблюдение секция={spec['section_id'][:8]} "
                    f"col={index} header={info['header']!r} kind={info['kind']} "
                    f"({marker})"
                )
        print("DRY-RUN: сеть не вызывалась — контексты/наблюдения готовы")
        return 0

    return asyncio.run(run_real(request_config, grids, specs))


async def run_real(request_config, grids, specs) -> int:
    budget_left = PAID_CALL_BUDGET
    results = []
    for spec in specs:
        result = await run_one_section(
            spec=spec,
            grids=grids,
            request_config=request_config,
            model=request_config.model,
            budget_left=budget_left,
        )
        results.append(result)
        budget_left -= result["calls"]
        if budget_left <= 0:
            break

    print("\n=== Результаты по секциям ===")
    paid_calls = sum(r["calls"] for r in results)
    provider_errors = [
        r for r in results if r["error"] and "RuleModelError" in r["error"]
    ]
    compile_failures = [r for r in results if r["compiled"] is None and r["error"]]
    header_verdicts: list[dict] = []
    normalization_warnings: list[str] = []
    model_sample_shown = False

    for result in results:
        section_id = str(result["section_id"])[:8]
        print(f"\n--- секция {section_id} (paid calls={result['calls']}) ---")
        for entry in result["calls_log"]:
            usage = entry.get("usage") or {}
            payload = entry.get("payload") or {}
            suffix = ""
            if payload:
                suffix = (
                    f" decision={payload.get('decision')} "
                    f"review={payload.get('requires_manual_review')} "
                    f"confidence={payload.get('confidence')}"
                )
            if entry.get("repair"):
                suffix += " [semantic repair]"
            print(
                f"  call #{entry.get('attempt')}: "
                f"duration_ms={entry.get('duration_ms')} "
                f"http={entry.get('http_status')} "
                f"finish={entry.get('finish_reason')} "
                f"id={entry.get('response_id')} "
                f"usage_in={usage.get('input_tokens')} "
                f"usage_out={usage.get('output_tokens')}{suffix}"
            )
            model_kinds = entry.get("model_kinds")
            if model_kinds:
                print(
                    "    price_kind от модели (до нормализации): "
                    + json.dumps(model_kinds, ensure_ascii=False)
                )
            compiler_error = entry.get("compiler_error")
            if compiler_error:
                print(f"    compiler_error: {compiler_error}")
        if result["raw_payload"] is not None and not model_sample_shown:
            sample = json.dumps(result["raw_payload"], ensure_ascii=False, default=str)
            print(f"  пример ответа модели (обрезан): {sample[:1200]}")
            model_sample_shown = True
        if result["error"]:
            print(f"  ERROR секции: {result['error']}")
        compiled = result["compiled"]
        if compiled is None:
            continue
        validation = result["validation"]
        spec = next(
            (s for s in specs if s["section_id"] == result["section_id"]), None
        )
        context = spec["input_context"] if spec else None
        expected = expected_from_context(context) if context else {}
        warnings = compiled.validation.get("normalisation_warnings") or []
        normalization_warnings.extend(warnings)
        if warnings:
            print("  normalisation_warnings (P0-1 заменил kind модели):")
            for warn in warnings:
                print(f"    - {warn}")
        if validation is not None:
            print(
                f"  dry-run passed={validation.get('passed')} "
                f"items={validation.get('item_count')}"
            )
        for column in price_columns_from_compiled(compiled):
            norm = normalized(column["header"])
            target_kind = TARGET_HEADERS.get(norm)
            expected_kind = expected.get(column["index"], {}).get("kind")
            model_kind = result["raw_kinds"].get(column["index"])
            final_kind = column["kind"]
            ok = expected_kind is not None and final_kind == expected_kind
            status = "PASS" if ok else "FAIL"
            if target_kind is not None or expected_kind is not None:
                header_verdicts.append(
                    {
                        "section_id": section_id,
                        "index": column["index"],
                        "header": column["header"],
                        "expected": expected_kind,
                        "model_kind": model_kind,
                        "final_kind": final_kind,
                        "status": status,
                    }
                )
            print(
                f"  col={column['index']} header={column['header']!r} "
                f"детерм.={expected_kind} модель={model_kind} "
                f"итог={final_kind} -> {status}"
            )

    print("\n=== Сводка P0-1 по заголовкам ===")
    print("header | детерм. | ответ модели | итог | verdict")
    for verdict in header_verdicts:
        print(
            f"{verdict['header']!r} | {verdict['expected']} | "
            f"{verdict['model_kind']} | {verdict['final_kind']} | "
            f"{verdict['status']}"
        )
    print(f"\npaid calls (реальных вызовов модели): {paid_calls}")
    print(
        f"секций: всего={len(results)} provider_errors={len(provider_errors)} "
        f"compile_failures={len(compile_failures)}"
    )
    print(f"P0-1 normalisation_warnings: {len(normalization_warnings)}")

    failed_headers = [v for v in header_verdicts if v["status"] != "PASS"]
    if provider_errors:
        print(f"\nRESULT: ERROR — {provider_errors[0]['error']}")
        return 4
    if not paid_calls:
        print("\nRESULT: ERROR — модель не вызывалась (бюджет исчерпан ранее)")
        return 4
    if failed_headers:
        print("\nRESULT: FAIL — найдены неверные итоговые price_kind")
        return 1
    if not header_verdicts:
        print("\nRESULT: FAIL — ни одного целевого price-столбца не проверено")
        return 1
    if compile_failures:
        print(
            "\nRESULT: PASS (с оговоркой) — проверенные заголовки верны, но "
            f"{len(compile_failures)} секция(и) не скомпилировалась: "
            f"{compile_failures[0]['error']}"
        )
        return 0
    print(
        "\nRESULT: PASS — ключ валиден, реальный вызов прошёл, "
        "детерминированный price_kind: 0 ошибок"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())