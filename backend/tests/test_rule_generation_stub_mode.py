"""Deterministic rule-generation stub (RULE_GENERATION_STUB_MODE).

Spec-derived behaviour tests:
- RULE_GENERATION_STUB_MODE=true (dev) returns a deterministic, schema-
  conformant proposal without calling an external API, even when no provider
  key is configured;
- with the stub flag off and no key the established configuration error is
  preserved;
- with the stub flag off and a real key the provider workflow is unchanged;
- a section attempt for a price file without a rule reaches a normal terminal
  state (never ``failed`` / provider-not-configured) under stub mode.
"""
from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.llm_providers import rule_generation_stub_enabled
from app.models import (
    Base,
    PriceFile,
    PriceImport,
    PriceVersion,
    RuleGenerationAttempt,
    ServiceClient,
    Supplier,
)
from app.parsing.grid import Cell, SheetGrid
from app.parsing.rule_proposal import context_hash
from app.parsing.rule_proposal_handlers import (
    TABULAR_PROPOSAL_CONTRACT,
    build_rule_section_contexts,
    handler_for_contract,
)
from app import import_service
from app import rule_proposal_service as service


@pytest.fixture(autouse=True)
def _disable_phoenix_telemetry(monkeypatch):
    """Keep stub-mode tests hermetic: no OTel exporter thread/network at exit."""
    monkeypatch.setattr(service.settings, "phoenix_enabled", False)


class _ExplodingAsyncClient:
    """Raise if any code path tries to open an HTTP connection."""

    def __init__(self, *_args, **_kwargs):
        raise AssertionError("stub mode must not create an HTTP client")

    async def __aenter__(self):
        raise AssertionError("stub mode must not open an HTTP client")

    async def __aexit__(self, *_args):
        return False

    async def post(self, *_args, **_kwargs):
        raise AssertionError("stub mode must not POST to an LLM provider")


class _RecordingAsyncClient:
    def __init__(self, response_body: dict):
        self.response_body = response_body
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return httpx.Response(200, json=self.response_body)


def _grid(*, item_name: str = "Клапан") -> SheetGrid:
    return SheetGrid(
        name="Прайс",
        rows=[
            [Cell("Наименование"), Cell("Артикул"), Cell("Цена")],
            [Cell(item_name), Cell("A-1"), Cell("1200")],
        ],
    )


def _section_context(grid: SheetGrid) -> dict:
    return build_rule_section_contexts([grid])[0]


def test_settings_flag_is_env_ready_and_exposed_by_allowlist_module() -> None:
    configured = Settings(rule_generation_stub_mode=True)
    assert configured.rule_generation_stub_mode is True
    assert rule_generation_stub_enabled(configured) is True
    assert rule_generation_stub_enabled(Settings(rule_generation_stub_mode=False)) is False
    # pydantic-settings accepts the env spelling RULE_GENERATION_STUB_MODE=true.
    assert Settings(rule_generation_stub_mode="true").rule_generation_stub_mode is True


def test_stub_payload_is_schema_valid_and_deterministic_without_api_key(monkeypatch) -> None:
    monkeypatch.setattr(service.settings, "rule_generation_stub_mode", True)
    monkeypatch.setattr(service.settings, "llm_provider", "openai")
    monkeypatch.setattr(service.settings, "openai_api_key", None)
    monkeypatch.setattr(service.httpx, "AsyncClient", _ExplodingAsyncClient)

    async def scenario() -> tuple[dict, dict]:
        first = await service._request_rule_proposal_result(
            prompt="classify the section",
            model="gpt-4o",
            proposal_contract=TABULAR_PROPOSAL_CONTRACT,
        )
        second = await service._request_rule_proposal_result(
            prompt="classify the section",
            model="gpt-4o",
            proposal_contract=TABULAR_PROPOSAL_CONTRACT,
        )
        return first, second

    first, second = asyncio.run(scenario())
    assert first.payload == second.payload
    handler_for_contract(TABULAR_PROPOSAL_CONTRACT).parse(first.payload)
    assert all(
        first.usage[key] == 0 for key in ("input_tokens", "output_tokens", "total_tokens")
    )
    assert first.telemetry["success"] is True
    assert first.telemetry["http_attempts"] == 0
    assert first.telemetry["provider"] in {"openai", "yandexgpt", "deepseek"}


def test_stub_payload_from_section_context_is_schema_valid_and_stable(monkeypatch) -> None:
    monkeypatch.setattr(service.settings, "rule_generation_stub_mode", True)
    monkeypatch.setattr(service.settings, "llm_provider", "openai")
    monkeypatch.setattr(service.settings, "openai_api_key", None)
    context = _section_context(_grid())

    payload = service.build_stub_rule_proposal_payload(
        source_context=context,
        loader="xlsx",
        proposal_contract=TABULAR_PROPOSAL_CONTRACT,
    )
    again = service.build_stub_rule_proposal_payload(
        source_context=context,
        loader="xlsx",
        proposal_contract=TABULAR_PROPOSAL_CONTRACT,
    )
    assert payload == again
    parsed = handler_for_contract(TABULAR_PROPOSAL_CONTRACT).parse(payload)
    assert parsed.decision in {
        "propose",
        "catalog_without_price",
        "insufficient_evidence",
        "not_price_section",
    }
    assert parsed.requires_manual_review is True
    assert parsed.source_section_id == context["section_id"]


def test_missing_key_without_stub_mode_preserves_configuration_error(monkeypatch) -> None:
    monkeypatch.setattr(service.settings, "rule_generation_stub_mode", False)
    monkeypatch.setattr(service.settings, "llm_provider", "openai")
    monkeypatch.setattr(service.settings, "openai_api_key", None)
    monkeypatch.setattr(service.httpx, "AsyncClient", _ExplodingAsyncClient)

    with pytest.raises(service.RuleModelError) as excinfo:
        asyncio.run(
            service._request_rule_proposal_result(
                prompt="classify the section",
                model="gpt-4o",
                proposal_contract=TABULAR_PROPOSAL_CONTRACT,
            )
        )
    assert excinfo.value.telemetry["error_type"] == "configuration_error"
    assert "ключ" in str(excinfo.value).lower()


def test_real_key_with_stub_off_still_calls_provider(monkeypatch) -> None:
    monkeypatch.setattr(service.settings, "rule_generation_stub_mode", False)
    monkeypatch.setattr(service.settings, "llm_provider", "openai")
    monkeypatch.setattr(service.settings, "openai_api_key", "real-secret-key")
    inner_payload = {
        "schema_version": "price-rule-proposal-v1",
        "decision": "insufficient_evidence",
        "rule_name": "Provider answer",
        "loader": "xlsx",
        "source_section_id": "section-1",
        "mapping": None,
        "discovered_attributes": [],
        "evidence": [],
        "unresolved": ["provider said so"],
        "confidence": 0.1,
        "requires_manual_review": True,
    }
    response_body = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(inner_payload)},
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    client = _RecordingAsyncClient(response_body)
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda *_a, **_k: client)

    async def scenario() -> dict:
        result = await service._request_rule_proposal_result(
            prompt="classify the section",
            model="gpt-4o",
            proposal_contract=TABULAR_PROPOSAL_CONTRACT,
        )
        return result.payload

    payload = asyncio.run(scenario())
    assert payload == inner_payload
    assert len(client.calls) == 1
    url, kwargs = client.calls[0]
    assert url.endswith("/chat/completions")
    assert kwargs["headers"]["Authorization"] == "Bearer real-secret-key"
    assert kwargs["json"]["model"] == "gpt-4o"


async def _seed_import_rows(factory, *, data: bytes, suffix: str):
    session = factory()
    client = ServiceClient(
        name=f"Client {suffix}",
        key_prefix=f"prefix-{suffix}",
        key_digest=sha256(("secret-" + suffix).encode()).hexdigest(),
        is_internal=True,
    )
    session.add(client)
    await session.flush()
    supplier = Supplier(code=f"supplier-{suffix}", name=f"Supplier {suffix}")
    session.add(supplier)
    await session.flush()
    price_file = PriceFile(
        supplier_id=supplier.id,
        original_filename=f"synthetic-{suffix}.xlsx",
        storage_key=f"synthetic/{suffix}",
        sha256=sha256(data).hexdigest(),
        size_bytes=len(data),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    session.add(price_file)
    await session.flush()
    version = PriceVersion(
        supplier_id=supplier.id,
        price_file_id=price_file.id,
        version_no=1,
    )
    session.add(version)
    await session.flush()
    price_import = PriceImport(
        service_client_id=client.id,
        supplier_id=supplier.id,
        price_file_id=price_file.id,
        price_version_id=version.id,
        idempotency_key=f"key-{suffix}",
        request_fingerprint=sha256(("fingerprint-" + suffix).encode()).hexdigest(),
        provider="openai",
    )
    session.add(price_import)
    await session.commit()
    ids = (client.id, supplier.id, price_file.id, version.id, price_import.id)
    await session.close()
    return ids


async def _create_attempts_with_stub(monkeypatch, *, stub_mode: bool):
    data = b"demo-xlsx-bytes"
    grid = _grid()
    engine, factory = await _database()
    monkeypatch.setattr(import_service.settings, "rule_generation_stub_mode", stub_mode)
    monkeypatch.setattr(import_service.settings, "openai_api_key", None)
    monkeypatch.setattr(import_service.loaders, "detect_loader", lambda *_args: "xlsx")
    monkeypatch.setattr(
        import_service.loaders,
        "load_grids_for",
        lambda _loader, _data, _filename: [grid],
    )
    ids = await _seed_import_rows(factory, data=data, suffix="gate")
    session = factory()
    price_import = await session.get(PriceImport, ids[4])
    version = await session.get(PriceVersion, ids[3])
    price_file = await session.get(PriceFile, ids[2])
    try:
        attempts = await import_service._create_attempts(
            session,
            price_import=price_import,
            version=version,
            price_file=price_file,
            data=data,
        )
        outcome = [attempt.status for attempt in attempts]
    finally:
        await session.close()
        await engine.dispose()
    return outcome


def test_create_attempts_skips_missing_key_guard_in_stub_mode(monkeypatch) -> None:
    """Demo gate: with the flag on, a file without a rule and without any key
    still creates queued section attempts instead of LLM_PROVIDER_NOT_CONFIGURED."""

    statuses = asyncio.run(_create_attempts_with_stub(monkeypatch, stub_mode=True))
    assert statuses
    assert all(status == service.ATTEMPT_QUEUED for status in statuses)


def test_create_attempts_without_key_and_stub_off_still_raises(monkeypatch) -> None:
    """Established behaviour: without the flag a missing key keeps blocking."""

    async def scenario():
        try:
            await _create_attempts_with_stub(monkeypatch, stub_mode=False)
        except import_service.LlmProviderNotConfigured:
            return True
        return False

    assert asyncio.run(scenario()) is True


async def _database():
    engine = create_async_engine("sqlite+aiosqlite://")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, factory


async def _add_attempt(factory, *, grid: SheetGrid, data: bytes, suffix: str):
    context = build_rule_section_contexts([grid])[0]
    session = factory()
    supplier = Supplier(code=f"supplier-{suffix}", name=f"Supplier {suffix}")
    session.add(supplier)
    await session.flush()
    price_file = PriceFile(
        supplier_id=supplier.id,
        original_filename=f"synthetic-{suffix}.xlsx",
        storage_key=f"synthetic/{suffix}",
        sha256=sha256(data).hexdigest(),
        size_bytes=len(data),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    session.add(price_file)
    await session.flush()
    version = PriceVersion(
        supplier_id=supplier.id,
        price_file_id=price_file.id,
        version_no=1,
    )
    session.add(version)
    await session.flush()
    attempt = RuleGenerationAttempt(
        price_version_id=version.id,
        batch_id=uuid.uuid4(),
        section_id=context["section_id"],
        status=service.ATTEMPT_QUEUED,
        provider="openai",
        model="gpt-4o",
        request_settings={"temperature": 0, "top_p": 1},
        prompt_version=str(context.get("proposal_contract", TABULAR_PROPOSAL_CONTRACT)),
        prompt_id=f"prompt-{suffix}",
        source_file_sha256=sha256(data).hexdigest(),
        source_context_hash=context_hash(context),
        input_context=context,
        prompt="Classify this bounded synthetic section",
    )
    session.add(attempt)
    await session.commit()
    attempt_id = attempt.id
    await session.close()
    return attempt_id


def test_stub_mode_section_attempt_ends_reviewable_without_provider(monkeypatch) -> None:
    """Demo: a new price file without a rule never fails on missing LLM key."""

    data = b"demo-xlsx-bytes"
    grid = _grid()

    async def scenario() -> str:
        engine, factory = await _database()
        monkeypatch.setattr(service.settings, "rule_generation_stub_mode", True)
        monkeypatch.setattr(service.settings, "durable_rule_generation_enabled", False)
        monkeypatch.setattr(service.settings, "openai_api_key", None)
        monkeypatch.setattr(service.storage, "get_price_file", lambda _key: data)
        monkeypatch.setattr(service.loaders, "detect_loader", lambda *_args: "xlsx")
        monkeypatch.setattr(
            service.loaders,
            "load_grids_for",
            lambda _loader, _data, _filename: [grid],
        )
        monkeypatch.setattr(service.httpx, "AsyncClient", _ExplodingAsyncClient)
        attempt_id = await _add_attempt(factory, grid=grid, data=data, suffix="demo")
        status = await service.run_rule_generation_attempt(factory(), attempt_id)
        attempt = await factory().get(RuleGenerationAttempt, attempt_id)
        outcome = attempt.status
        message = attempt.error_message
        await engine.dispose()
        return status, outcome, message

    status, outcome, message = asyncio.run(scenario())
    # Status returned by the runner and the persisted attempt must agree and be
    # a normal reviewable outcome - never the provider configuration failure.
    assert status == outcome
    assert status in {
        service.ATTEMPT_PROPOSED,
        service.ATTEMPT_CATALOG_WITHOUT_PRICE,
        service.ATTEMPT_SKIPPED_NON_PRICE,
        service.ATTEMPT_INSUFFICIENT,
        service.ATTEMPT_REJECTED,
    }
    assert "ключ" not in (message or "").lower()
    assert "LLM_PROVIDER_NOT_CONFIGURED" not in (message or "")
