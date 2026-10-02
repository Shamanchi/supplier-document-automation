"""Durable rule-generation primitives that are not wired into runtime yet."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RuleGenerationArtifact


def _canonical_json(value: Any) -> str:
    """Serialize JSON data deterministically without semantic guessing."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def compute_request_settings_hash(
    request_settings: Mapping[str, Any], *, contract_version: str
) -> str:
    canonical = _canonical_json(
        {
            "contract_version": contract_version,
            "request_settings": dict(request_settings),
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_rule_generation_work_key(
    *,
    source_file_sha256: str,
    source_context_hash: str,
    prompt_version: str,
    provider: str,
    model: str,
    request_settings: Mapping[str, Any],
    contract_version: str,
) -> str:
    """Return the exact, stable identity of one model work unit."""

    canonical = _canonical_json(
        {
            "source_file_sha256": source_file_sha256,
            "source_context_hash": source_context_hash,
            "prompt_version": prompt_version,
            "provider": provider,
            "model": model,
            "request_settings": dict(request_settings),
            "contract_version": contract_version,
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def find_verified_artifact(
    session: AsyncSession, work_key: str
) -> RuleGenerationArtifact | None:
    """Find only an exact verified artifact; no structural or fuzzy reuse."""

    return await session.scalar(
        select(RuleGenerationArtifact).where(
            RuleGenerationArtifact.work_key == work_key
        )
    )


async def create_verified_artifact(
    session: AsyncSession,
    *,
    work_key: str,
    provider: str,
    model: str,
    request_settings_hash: str,
    source_file_sha256: str,
    source_context_hash: str,
    prompt_version: str,
    proposal: dict,
    validation: dict,
    compiler_dry_run_summary: dict,
    provenance: str,
) -> RuleGenerationArtifact:
    """Create once by exact key, safely converging on a concurrent winner."""

    if validation.get("passed") is not True:
        raise ValueError("Verified artifact requires passed validation")
    if compiler_dry_run_summary.get("passed") is not True:
        raise ValueError("Verified artifact requires passed compiler dry-run")

    existing = await find_verified_artifact(session, work_key)
    if existing is not None:
        return existing

    artifact = RuleGenerationArtifact(
        work_key=work_key,
        provider=provider,
        model=model,
        request_settings_hash=request_settings_hash,
        source_file_sha256=source_file_sha256,
        source_context_hash=source_context_hash,
        prompt_version=prompt_version,
        proposal=proposal,
        validation=validation,
        compiler_dry_run_summary=compiler_dry_run_summary,
        provenance=provenance,
    )
    try:
        async with session.begin_nested():
            session.add(artifact)
            await session.flush()
    except IntegrityError:
        winner = await find_verified_artifact(session, work_key)
        if winner is None:  # pragma: no cover - preserves the original DB error
            raise
        return winner
    return artifact
