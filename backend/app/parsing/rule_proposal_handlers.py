"""Closed dispatcher for section-scoped rule proposal contracts."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Callable

from app.parsing.matrix_unfold import (
    MATRIX_PROMPT_VERSION,
    MATRIX_PROPOSAL_CONTRACT,
    MATRIX_SYSTEM_PROMPT,
    build_matrix_rule_generation_prompt,
    build_matrix_section_contexts,
    compile_matrix_proposal,
    matrix_proposal_json_schema,
    parse_matrix_proposal,
)
from app.parsing.rule_proposal import (
    RULE_PROPOSAL_PROMPT_VERSION,
    RULE_PROPOSAL_SYSTEM_PROMPT,
    build_tabular_rule_generation_prompt,
    build_tabular_rule_repair_prompt,
    build_tabular_section_contexts,
    compile_section_proposal,
    parse_proposal,
    rule_proposal_json_schema,
)
from app.parsing.section_diagnostics import build_section_diagnostics


TABULAR_PROPOSAL_CONTRACT = RULE_PROPOSAL_PROMPT_VERSION


@dataclass(frozen=True)
class RuleProposalHandler:
    contract: str
    prompt_version: str
    schema_name: str
    system_prompt: str
    json_schema: Callable[[], dict[str, Any]]
    parse: Callable[[object], object]
    compile: Callable[..., object]
    build_repair_prompt: Callable[..., str]


def _matrix_repair_prompt(
    *, base_prompt: str, previous_payload: object, verifier_feedback: object
) -> str:
    import json

    return (
        base_prompt
        + "\nПредыдущее matrix-предложение отклонено source compiler. "
        "Верни полный исправленный JSON, сохрани section_id и block_ref. "
        "Не добавляй физические refs: исправь только семантику осей и типа цены.\n"
        + json.dumps(
            {"previous": previous_payload, "verifier_feedback": verifier_feedback},
            ensure_ascii=False,
            default=str,
        )[:18000]
    )


_HANDLERS = {
    TABULAR_PROPOSAL_CONTRACT: RuleProposalHandler(
        contract=TABULAR_PROPOSAL_CONTRACT,
        prompt_version=RULE_PROPOSAL_PROMPT_VERSION,
        schema_name="price_rule_proposal",
        system_prompt=RULE_PROPOSAL_SYSTEM_PROMPT,
        json_schema=rule_proposal_json_schema,
        parse=parse_proposal,
        compile=compile_section_proposal,
        build_repair_prompt=build_tabular_rule_repair_prompt,
    ),
    MATRIX_PROPOSAL_CONTRACT: RuleProposalHandler(
        contract=MATRIX_PROPOSAL_CONTRACT,
        prompt_version=MATRIX_PROMPT_VERSION,
        schema_name="matrix_rule_proposal",
        system_prompt=MATRIX_SYSTEM_PROMPT,
        json_schema=matrix_proposal_json_schema,
        parse=parse_matrix_proposal,
        compile=compile_matrix_proposal,
        build_repair_prompt=_matrix_repair_prompt,
    ),
}


def handler_for_contract(contract: object) -> RuleProposalHandler:
    value = str(contract or TABULAR_PROPOSAL_CONTRACT)
    try:
        return _HANDLERS[value]
    except KeyError as exc:
        raise ValueError(f"Неизвестный proposal contract: {value}") from exc


def _overlaps_matrix(context: dict[str, Any], matrices: list[dict[str, Any]]) -> bool:
    sheet_index = context.get("sheet_index")
    start = context.get("section_start_row_index")
    end = context.get("section_end_row_index")
    if not isinstance(start, int) or not isinstance(end, int):
        return False
    return any(
        matrix.get("sheet_index") == sheet_index
        and start < int(matrix["region_end_row_index"])
        and int(matrix["region_start_row_index"]) < end
        for matrix in matrices
    )


def _prompt_id(system_prompt: str, prompt: str) -> str:
    return sha256(f"{system_prompt}\0{prompt}".encode("utf-8")).hexdigest()[:16]


def build_rule_section_specs(
    *,
    grids,
    loader: str,
    model: str,
    attribute_catalog: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    from app.parsing.rule_proposal import context_hash

    contexts = build_rule_section_contexts(grids)
    tabular_contexts = [
        context
        for context in contexts
        if context.get("proposal_contract") != MATRIX_PROPOSAL_CONTRACT
    ]
    matrix_contexts = [
        context
        for context in contexts
        if context.get("proposal_contract") == MATRIX_PROPOSAL_CONTRACT
    ]
    rows: list[tuple[dict[str, Any], RuleProposalHandler, str]] = []
    for context in tabular_contexts:
        handler = handler_for_contract(TABULAR_PROPOSAL_CONTRACT)
        prompt = build_tabular_rule_generation_prompt(
            source_context=context,
            loader=loader,
            model=model,
            attribute_catalog=attribute_catalog,
        )
        rows.append((context, handler, prompt))
    for context in matrix_contexts:
        handler = handler_for_contract(MATRIX_PROPOSAL_CONTRACT)
        prompt = build_matrix_rule_generation_prompt(
            source_context=context,
            loader=loader,
            model=model,
            attribute_catalog=attribute_catalog,
        )
        rows.append((context, handler, prompt))
    rows.sort(
        key=lambda row: (
            int(row[0].get("sheet_index", 0)),
            int(
                row[0].get(
                    "region_start_row_index",
                    row[0].get("section_start_row_index", 0),
                )
            ),
        )
    )
    return [
        {
            "section_id": context["section_id"],
            "proposal_contract": handler.contract,
            "prompt_version": handler.prompt_version,
            "source_context_hash": context_hash(context),
            "input_context": context,
            "prompt": prompt,
            "prompt_id": _prompt_id(handler.system_prompt, prompt),
            # Stage 6A telemetry only.  It must not filter specs or alter the
            # prompt/context sent through the existing provider workflow.
            "diagnostics": build_section_diagnostics(
                context, loader=loader, prompt=prompt
            ),
        }
        for context, handler, prompt in rows
    ]


def build_rule_section_contexts(grids) -> list[dict[str, Any]]:
    matrix_contexts = build_matrix_section_contexts(grids)
    tabular_contexts = [
        context
        for context in build_tabular_section_contexts(grids)
        if not _overlaps_matrix(context, matrix_contexts)
    ]
    return [*tabular_contexts, *matrix_contexts]
