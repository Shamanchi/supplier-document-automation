#!/usr/bin/env python3
"""Run the production mixed-section rule generator without persistence.

The command is intentionally source-agnostic: it accepts any supported workbook,
uses the same structural contexts, prompts, providers and source compilers as the
API, then executes the combined recipe over the complete source file.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from hashlib import sha256
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", default="deepseek")
    parser.add_argument("--model")
    parser.add_argument("--concurrency", type=int, default=3)
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    from app.llm_providers import resolve_rule_model_config
    from app.parsing import loaders
    from app.parsing.mapping import MappingDecision
    from app.parsing.matrix_unfold import MATRIX_PROPOSAL_CONTRACT, parse_matrix_regions
    from app.parsing.rule_proposal import compile_batch_proposals
    from app.parsing.rule_proposal_handlers import build_rule_section_specs, handler_for_contract
    from app.parsing.rules import recipe_from_decision, validation_metrics
    from app.parsing.workbook_layout import parse_workbook_layout
    from app.rule_proposal_service import _request_rule_proposal_result

    data = args.source.read_bytes()
    loader = loaders.detect_loader(data, args.source.name)
    grids = loaders.load_grids_for(loader, data, args.source.name)
    request_config = resolve_rule_model_config(
        provider=args.provider,
        model=args.model,
        temperature=0,
        top_p=1,
        thinking="disabled",
        reasoning_effort="high",
    )
    specs = build_rule_section_specs(
        grids=grids,
        loader=loader,
        model=request_config.model,
        attribute_catalog=[],
    )
    semaphore = asyncio.Semaphore(max(1, args.concurrency))
    outputs: dict[str, dict[str, Any]] = {}
    cached_outputs: dict[str, dict[str, Any]] = {}
    if args.output.exists():
        previous = json.loads(args.output.read_text(encoding="utf-8"))
        if previous.get("source_sha256") == sha256(data).hexdigest():
            cached_outputs = {
                str(row["section_id"]): row
                for row in previous.get("attempts", [])
                if isinstance(row, dict) and row.get("section_id")
            }

    async def one(spec: dict[str, Any]) -> None:
        handler = handler_for_contract(spec["proposal_contract"])
        prompt = spec["prompt"]
        cached = cached_outputs.get(spec["section_id"])
        if cached and cached.get("passed") and isinstance(cached.get("proposal"), dict):
            outputs[spec["section_id"]] = cached
            print(
                f"[{len(outputs)}/{len(specs)}] {spec['section_id']} passed=True cached=True",
                flush=True,
            )
            return
        row: dict[str, Any] = {
            "section_id": spec["section_id"],
            "proposal_contract": spec["proposal_contract"],
            "passed": False,
        }
        if cached and isinstance(cached.get("proposal"), dict):
            row["proposal"] = cached["proposal"]
            prompt = handler.build_repair_prompt(
                base_prompt=spec["prompt"],
                previous_payload=cached["proposal"],
                verifier_feedback={
                    "error": cached.get("error"),
                    "validation": cached.get("validation"),
                },
            )
        async with semaphore:
            for semantic_attempt in range(2):
                try:
                    response = await _request_rule_proposal_result(
                        prompt=prompt,
                        model=request_config.model,
                        request_config=request_config,
                        proposal_contract=spec["proposal_contract"],
                        telemetry_dimensions={
                            "acceptance": "generated_workbook_rule",
                            "section_id": spec["section_id"],
                        },
                    )
                    proposal = handler.parse(response.payload)
                    row["proposal"] = proposal.model_dump(mode="json")
                    row["usage"] = response.usage
                    if spec["proposal_contract"] == MATRIX_PROPOSAL_CONTRACT:
                        if proposal.decision != "propose":
                            raise ValueError(f"matrix decision={proposal.decision}")
                        compiled = handler.compile(
                            proposal,
                            source_context=spec["input_context"],
                            grids=grids,
                        )
                        validation = validation_metrics(parse_matrix_regions(grids, [compiled]))
                    else:
                        compiled = handler.compile(
                            proposal,
                            source_context=spec["input_context"],
                            loader=loader,
                            grids=grids,
                        )
                        if compiled.table is None:
                            validation = compiled.validation
                        else:
                            from app.parsing.rule_proposal import dry_run_decision

                            _, validation = dry_run_decision(
                                grids,
                                MappingDecision(tables=[compiled.table], auto_ok=True),
                            )
                    row["validation"] = validation
                    if validation.get("passed"):
                        row["passed"] = True
                        break
                    if semantic_attempt == 1:
                        raise ValueError("section quality gate failed")
                    prompt = handler.build_repair_prompt(
                        base_prompt=spec["prompt"],
                        previous_payload=response.payload,
                        verifier_feedback=validation,
                    )
                except Exception as exc:  # keep all sections observable
                    row["error"] = " ".join(str(exc).split())[:1000]
                    if semantic_attempt == 1:
                        break
                    if "proposal" not in row:
                        break
                    prompt = handler.build_repair_prompt(
                        base_prompt=spec["prompt"],
                        previous_payload=row["proposal"],
                        verifier_feedback={"source_compiler_error": row["error"]},
                    )
            outputs[spec["section_id"]] = row
            print(
                f"[{len(outputs)}/{len(specs)}] {spec['section_id']} passed={row['passed']}",
                flush=True,
            )

    await asyncio.gather(*(one(spec) for spec in specs))
    failed = [row for row in outputs.values() if not row["passed"]]
    attempts = []
    for spec in specs:
        context = spec["input_context"]
        attempts.append(
            {
                **outputs[spec["section_id"]],
                "price": args.source.name,
                "sheet_name": context.get("sheet_name"),
                "header_row_index": context.get("header_row_index"),
                "section_kind": context.get("section_kind"),
                "section_start_row_index": context.get("section_start_row_index"),
                "section_end_row_index": context.get("section_end_row_index"),
            }
        )
    report: dict[str, Any] = {
        "source_sha256": sha256(data).hexdigest(),
        "loader": loader,
        "provider": request_config.provider,
        "model": request_config.model,
        "sections": len(specs),
        "passed_sections": len(specs) - len(failed),
        "failed_sections": len(failed),
        "attempts": attempts,
    }
    if failed:
        return report

    tabular = [
        (outputs[spec["section_id"]]["proposal"], spec["input_context"])
        for spec in specs
        if spec["proposal_contract"] != MATRIX_PROPOSAL_CONTRACT
    ]
    decision = compile_batch_proposals(tabular, loader=loader, grids=grids)
    matrix_regions = [
        handler_for_contract(MATRIX_PROPOSAL_CONTRACT).compile(
            handler_for_contract(MATRIX_PROPOSAL_CONTRACT).parse(
                outputs[spec["section_id"]]["proposal"]
            ),
            source_context=spec["input_context"],
            grids=grids,
        )
        for spec in specs
        if spec["proposal_contract"] == MATRIX_PROPOSAL_CONTRACT
    ]
    tabular_recipe = recipe_from_decision(decision, grids)
    recipe = {
        "format": "workbook_layout_v1",
        "profile": "generated_matrix_v1",
        "sheets": tabular_recipe.get("sheets", []),
        "options": tabular_recipe.get("options", {}),
        "matrix_regions": matrix_regions,
    }
    result = parse_workbook_layout(grids, recipe)
    matrix_section_ids = {
        str(region.get("source_section_id"))
        for region in matrix_regions
        if region.get("source_section_id")
    }
    report["recipe"] = recipe
    report["dry_run"] = validation_metrics(result)
    report["item_count"] = len(result.items)
    report["matrix_item_count"] = sum(
        item.source_section_id in matrix_section_ids
        for item in result.items
    )
    report["unique_item_rows"] = len(
        {(item.source_sheet, item.source_row) for item in result.items}
    )
    report["multi_item_physical_rows"] = sum(
        count > 1
        for count in Counter(
            (item.source_sheet, item.source_row) for item in result.items
        ).values()
    )
    report["issue_codes"] = dict(Counter(issue.code for issue in result.issues))
    return report


def main() -> int:
    args = _arguments()
    report = asyncio.run(_run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: report.get(key) for key in (
        "sections", "passed_sections", "failed_sections", "item_count", "matrix_item_count", "dry_run"
    )}, ensure_ascii=False, default=str))
    return 0 if report.get("failed_sections") == 0 and report.get("dry_run", {}).get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
