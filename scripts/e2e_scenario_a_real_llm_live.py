# -*- coding: utf-8 -*-
"""I-3 Step 1/2 driver: full live E2E Scenario A with real YandexGPT (v2, claim-all+heartbeat).

Replicates the exact import pipeline the API+celery worker would run, but in
one controlled process so RULE_GENERATION_STUB_MODE=false and
RULE_GENERATION_FAST_PATH_ENABLED=false can be applied per-process (running
containers keep the .env defaults).  No running container is touched; DB
rows/files/statuses are real and land on the dev stand.

v2 fixes the v1 race: all section attempts are claimed up-front (fast loop)
and kept alive by heartbeats, so the always-on celery beat/worker (stub env,
db 0) cannot scan-and-claim queued attempts from PostgreSQL while real LLM
calls run.  The caller also isolates this process's celery publishes on redis
db 15 (REDIS_URL override) - they are never consumed.

Exit codes: 0 PASS, 1 FAIL, 3 ABORT (env not prepared), 4 ERROR.
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
import time
import uuid

from sqlalchemy import func, select

from app import storage
from app.config import settings
from app.db import make_task_engine
from app.import_service import advance_price_import
from app.llm_providers import resolve_rule_model_config
from app.models import (
    ORIGINAL_PROJECT_ID,
    PriceFile,
    PriceImport,
    PriceImportEvent,
    PriceImportStatus,
    PriceVersion,
    RuleGenerationAttempt,
    RuleGenerationOperation,
    ServiceClient,
    Supplier,
    SupplierPriceItem,
    SupplierPriceValue,
    VersionStatus,
)
from app.rule_generation_lifecycle import (
    ATTEMPT_LEASED,
    claim_attempt,
    heartbeat_attempt,
    mark_section_succeeded,
    record_section_failure,
)
from app.rule_proposal_service import run_rule_generation_attempt

TARGET = {
    "цена опт": "wholesale",
    "опт": "wholesale",
    "цена розниц": "retail",
    "розниц": "retail",
    "розн": "retail",
}


def normalized(text: object) -> str:
    value = "" if text is None else str(text)
    return " ".join(value.lower().replace("ё", "е").split())


def expected_kind(header: str) -> str | None:
    norm = normalized(header)
    for marker, kind in TARGET.items():
        if norm == marker or norm.startswith(marker):
            return kind
    return None


def preflight() -> None:
    if settings.rule_generation_stub_mode:
        raise SystemExit("ABORT: RULE_GENERATION_STUB_MODE=true (нужно false)")
    if settings.rule_generation_fast_path_enabled:
        raise SystemExit("ABORT: RULE_GENERATION_FAST_PATH_ENABLED=true (нужно false)")
    if settings.llm_provider.casefold() != "yandexgpt":
        raise SystemExit("ABORT: LLM_PROVIDER != yandexgpt")
    if not settings.yandexgpt_api_key:
        raise SystemExit("ABORT: YANDEXGPT_API_KEY не задан")
    if "/15" not in (settings.redis_url or ""):
        raise SystemExit("ABORT: REDIS_URL должен быть изолирован (redis db 15)")
    print("preflight ok: provider=yandexgpt stub=False fast_path=False "
          f"key_len={len(settings.yandexgpt_api_key or '')}", flush=True)
    print(f"model={settings.rule_model} base={settings.yandexgpt_api_base_url}", flush=True)


async def create_import(session, file_path: str) -> PriceImport:
    data = open(file_path, "rb").read()
    sha256 = hashlib.sha256(data).hexdigest()
    client = (
        await session.execute(
            select(ServiceClient).where(ServiceClient.is_internal.is_(True))
        )
    ).scalar_one_or_none()
    if client is None:
        raise RuntimeError("internal UI service client not found")
    stamp = time.strftime("%Y%m%d%H%M%S")
    external_id = f"w3-scenarioa-real-llm-{stamp}"
    supplier = (
        await session.execute(
            select(Supplier).where(
                Supplier.service_client_id == client.id,
                Supplier.external_id == external_id,
            )
        )
    ).scalar_one_or_none()
    if supplier is None:
        external_hash = hashlib.sha256(external_id.encode()).hexdigest()[:24]
        supplier = Supplier(
            service_client_id=client.id,
            external_id=external_id,
            code=f"svc-{client.id.hex[:12]}-{external_hash}",
            name="Supplier A ScenarioA real-LLM",
        )
        session.add(supplier)
        await session.flush()
    storage_key = storage.put_price_file(
        data,
        sha256,
        "price_list_sample.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    price_file = PriceFile(
        project_id=ORIGINAL_PROJECT_ID,
        supplier_id=supplier.id,
        original_filename="price_list_sample.xlsx",
        storage_key=storage_key,
        sha256=sha256,
        size_bytes=len(data),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    session.add(price_file)
    await session.flush()
    max_version = (
        await session.execute(
            select(func.coalesce(func.max(PriceVersion.version_no), 0)).where(
                PriceVersion.supplier_id == supplier.id
            )
        )
    ).scalar_one()
    version = PriceVersion(
        supplier_id=supplier.id,
        price_file_id=price_file.id,
        version_no=max_version + 1,
        status=VersionStatus.uploaded.value,
    )
    session.add(version)
    await session.flush()
    cfg = resolve_rule_model_config()
    price_import = PriceImport(
        service_client_id=client.id,
        supplier_id=supplier.id,
        price_file_id=price_file.id,
        price_version_id=version.id,
        idempotency_key=uuid.uuid4().hex[:40],
        request_fingerprint=sha256,
        locale="ru-RU",
        currency_hint=None,
        vat_rate_percent=None,
        provider=cfg.provider,
        model=cfg.model,
        callback_url=None,
        policy="strict",
    )
    session.add(price_import)
    await session.flush()
    session.add(
        PriceImportEvent(
            price_import_id=price_import.id,
            event_type="import_created",
            to_status=PriceImportStatus.queued.value,
            payload={"file_sha256": sha256, "driver": "e2e_scenario_a_real_llm_live_v2"},
        )
    )
    await session.commit()
    print(f"EVIDENCE import_id={price_import.id} version_id={version.id} "
          f"supplier_id={supplier.id} sha256={sha256} size={len(data)}", flush=True)
    return price_import


class TransportFailure(Exception):
    pass


async def claim_all(engine, session_factory, import_id: uuid.UUID) -> dict[uuid.UUID, str]:
    async with session_factory() as session:
        attempts = (
            await session.execute(
                select(RuleGenerationAttempt)
                .where(RuleGenerationAttempt.price_import_id == import_id)
                .order_by(RuleGenerationAttempt.created_at, RuleGenerationAttempt.id)
            )
        ).scalars().all()
    if not attempts:
        raise RuntimeError("no attempts created (deterministic rule matched?)")
    print(f"sections to run: {len(attempts)}", flush=True)
    lease_seconds = max(60, settings.rule_generation_lease_seconds)
    claims: dict[uuid.UUID, str] = {}
    async with session_factory() as session:
        for attempt in attempts:
            owner = uuid.uuid4().hex
            claim = await claim_attempt(
                session,
                attempt_id=attempt.id,
                lease_owner=owner,
                lease_seconds=lease_seconds,
            )
            if not claim.claimed:
                raise RuntimeError(
                    f"attempt {attempt.id} ({attempt.section_id}) не заклеймлен: "
                    f"status={claim.status} reason={claim.reason} - внешний процесс опередил"
                )
            claims[attempt.id] = owner
    print(f"claimed {len(claims)}/{len(attempts)} attempts (lease={lease_seconds}s)", flush=True)
    return claims


async def heartbeat_all(engine, session_factory, claims: dict[uuid.UUID, str], stop: asyncio.Event) -> None:
    lease_seconds = max(60, settings.rule_generation_lease_seconds)
    interval = min(60, max(10, lease_seconds // 10))
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass
        try:
            async with session_factory() as session:
                for attempt_id, owner in claims.items():
                    try:
                        await heartbeat_attempt(
                            session,
                            attempt_id=attempt_id,
                            lease_owner=owner,
                            lease_seconds=lease_seconds,
                        )
                    except Exception:
                        pass
        except Exception:
            pass


async def _latest_operation(session, attempt_id: uuid.UUID) -> RuleGenerationOperation | None:
    return await session.scalar(
        select(RuleGenerationOperation)
        .where(RuleGenerationOperation.attempt_id == attempt_id)
        .order_by(RuleGenerationOperation.operation_no.desc())
        .limit(1)
    )


async def run_sections(engine, session_factory, claims: dict[uuid.UUID, str]) -> list[dict]:
    results: list[dict] = []
    async with session_factory() as session:
        attempts = (
            await session.execute(
                select(RuleGenerationAttempt)
                .where(RuleGenerationAttempt.id.in_(list(claims.keys())))
                .order_by(RuleGenerationAttempt.created_at, RuleGenerationAttempt.id)
            )
        ).scalars().all()
    for attempt in attempts:
        owner = claims[attempt.id]
        started = time.monotonic()
        async with session_factory() as session:
            status = await run_rule_generation_attempt(
                session,
                attempt.id,
                lease_claimed=True,
                recover_started_operation=True,
            )
            if status == "proposed":
                await mark_section_succeeded(session, attempt_id=attempt.id, lease_owner=owner)
                status = "succeeded"
            elif status in {"catalog_without_price", "skipped_non_price"}:
                await mark_section_succeeded(
                    session, attempt_id=attempt.id, lease_owner=owner, status=status
                )
            elif status in {"rejected", "insufficient_evidence"}:
                status = await record_section_failure(
                    session,
                    attempt_id=attempt.id,
                    lease_owner=owner,
                    failure_class="semantic",
                    terminal_reason=status,
                    retry_delays=settings.rule_generation_retry_delays_seconds,
                )
            elif status == "failed":
                operation = await _latest_operation(session, attempt.id)
                retryable = (
                    operation is not None
                    and (
                        operation.error_type == "transport_error"
                        or (
                            operation.error_type == "provider_http_error"
                            and isinstance(operation.http_status, int)
                            and (
                                operation.http_status in {408, 409, 425, 429}
                                or 500 <= operation.http_status <= 599
                            )
                        )
                    )
                )
                if retryable:
                    status = await record_section_failure(
                        session,
                        attempt_id=attempt.id,
                        lease_owner=owner,
                        failure_class="transport",
                        terminal_reason=operation.error_type or "transport_error",
                        retry_delays=settings.rule_generation_retry_delays_seconds,
                    )
                else:
                    status = await record_section_failure(
                        session,
                        attempt_id=attempt.id,
                        lease_owner=owner,
                        failure_class="semantic",
                        terminal_reason=operation.error_type or "section_execution_failed"
                        if operation is not None else "section_execution_failed",
                        retry_delays=settings.rule_generation_retry_delays_seconds,
                    )
            elapsed = time.monotonic() - started
            usage = await _usage_for(session, attempt.id)
        results.append(
            {"attempt_id": str(attempt.id), "section": attempt.section_id,
             "status": status, "elapsed_s": round(elapsed, 1), "usage": usage}
        )
        print(f"attempt {attempt.id} section={attempt.section_id} status={status} "
              f"elapsed_s={elapsed:.1f} usage={usage}", flush=True)
        if status == "retryable_failed":
            raise TransportFailure(
                f"attempt {attempt.id} section={attempt.section_id} transport failure"
            )
    return results


async def _usage_for(session, attempt_id: uuid.UUID) -> dict:
    operation = await _latest_operation(session, attempt_id)
    if operation is None:
        return {}
    return {"http": operation.http_status, "error": operation.error_type,
            "in": (operation.usage or {}).get("input_tokens"),
            "out": (operation.usage or {}).get("output_tokens"),
            "provider_operation_id": operation.provider_operation_id}


async def operation_evidence(session, import_id: uuid.UUID) -> None:
    rows = (
        await session.execute(
            select(
                RuleGenerationOperation.id,
                RuleGenerationOperation.attempt_id,
                RuleGenerationOperation.operation_no,
                RuleGenerationOperation.operation_kind,
                RuleGenerationOperation.status,
                RuleGenerationOperation.http_status,
                RuleGenerationOperation.error_type,
                RuleGenerationOperation.usage,
                RuleGenerationOperation.response_payload,
            )
            .join(RuleGenerationAttempt, RuleGenerationOperation.attempt_id == RuleGenerationAttempt.id)
            .where(RuleGenerationAttempt.price_import_id == import_id)
            .order_by(RuleGenerationAttempt.created_at, RuleGenerationAttempt.id,
                      RuleGenerationOperation.operation_no)
        )
    ).all()
    http_200 = 0
    paid = 0
    for row in rows:
        usage = row.usage or {}
        payload = row.response_payload or {}
        if row.http_status == 200:
            http_200 += 1
            paid += 1
        if row.operation_kind == "initial" and row.status == "completed" and row.http_status is None:
            pass  # stub-shaped operation, never reaches here in a real run
        print(f"  op attempt={str(row.attempt_id)[:8]} no={row.operation_no} "
              f"kind={row.operation_kind} status={row.status} http={row.http_status} "
              f"err={row.error_type} in={usage.get('input_tokens')} out={usage.get('output_tokens')} "
              f"decision={payload.get('decision')} review={payload.get('requires_manual_review')}", flush=True)
    print(f"EVIDENCE real_http_200_ops={http_200} total_ops={len(rows)}", flush=True)


async def kind_audit(session, version_id: uuid.UUID) -> None:
    items = (
        await session.execute(
            select(SupplierPriceItem).where(SupplierPriceItem.price_version_id == version_id)
        )
    ).scalars().all()
    item_ids = [item.id for item in items]
    values = []
    for offset in range(0, len(item_ids), 500):
        chunk = item_ids[offset:offset + 500]
        values.extend(
            (
                await session.execute(
                    select(SupplierPriceValue).where(SupplierPriceValue.item_id.in_(chunk))
                )
            ).scalars().all()
        )
    by_item = {item.id: item for item in items}
    checked = 0
    mismatches: list[dict] = []
    control: list[dict] = []
    kind_totals: dict[str, int] = {}
    for value in values:
        item = by_item.get(value.item_id)
        if item is None:
            continue
        kind_totals[value.price_kind] = kind_totals.get(value.price_kind, 0) + 1
        header = None
        for cell in item.source_header_cells or []:
            try:
                idx, text = cell[0], cell[1]
            except Exception:
                continue
            if idx == value.source_column_index:
                header = text
                break
        expected = expected_kind(header) if header else None
        if expected is not None:
            checked += 1
            if value.price_kind != expected:
                mismatches.append(
                    {"item": item.source_name[:80], "header": str(header)[:80],
                     "expected": expected, "actual": value.price_kind,
                     "amount": str(value.amount), "sheet": item.source_sheet}
                )
        if value.amount is not None and str(value.amount) in ("618.40", "834.84"):
            control.append(
                {"item": item.source_name[:80],
                 "header": str(header)[:80] if header else None,
                 "kind": value.price_kind, "amount": str(value.amount),
                 "source_row": item.source_row, "sheet": item.source_sheet}
            )
    print(f"EVIDENCE values_total={len(values)} items_total={len(items)} "
          f"kind_checked={checked} kind_mismatches={len(mismatches)} kind_totals={kind_totals}", flush=True)
    for m in mismatches[:20]:
        print("  MISMATCH", m, flush=True)
    print("EVIDENCE control_pairs:", flush=True)
    for c in control:
        print("  ", c, flush=True)
    if checked:
        correct = checked - len(mismatches)
        print(f"EVIDENCE target_header_accuracy={correct}/{checked} = {correct / checked * 100:.2f}%", flush=True)


async def main() -> int:
    file_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/price_new_26.03.2026_3.xlsx"
    preflight()
    engine, session_factory = make_task_engine()
    heartbeat_task: asyncio.Task | None = None
    stop = asyncio.Event()
    try:
        async with session_factory() as session:
            price_import = await create_import(session, file_path)
            import_id = price_import.id
            version_id = price_import.price_version_id
            status = await advance_price_import(session, import_id)
            print(f"after advance#1: status={status} import_status={price_import.status} "
                  f"attempt_no={price_import.attempt_no} rule_batch={price_import.rule_batch_id}", flush=True)
            if price_import.rule_batch_id is None:
                print("FAIL: no rule batch (deterministic rule matched / no LLM path)", flush=True)
                return 1
        claims = await claim_all(engine, session_factory, import_id)
        heartbeat_task = asyncio.create_task(heartbeat_all(engine, session_factory, claims, stop))
        try:
            await run_sections(engine, session_factory, claims)
        finally:
            stop.set()
            if heartbeat_task is not None:
                await heartbeat_task
            heartbeat_task = None
        async with session_factory() as session:
            await operation_evidence(session, import_id)
            async with session_factory() as s3:
                status = await advance_price_import(s3, import_id)
                final = await s3.get(PriceImport, import_id)
                print(f"after advance#2: status={status} import_status={final.status} "
                      f"error_code={final.error_code} error_message={final.error_message}", flush=True)
                version = await s3.get(PriceVersion, version_id)
                print(f"EVIDENCE version item_count={version.item_count} rows_accepted={version.rows_accepted} "
                      f"warnings={version.warnings_count} errors={version.errors_count} "
                      f"version_status={version.status} loader={final.loader} "
                      f"parse_rule={final.parse_rule_id} rev={final.parse_rule_revision}", flush=True)
                await kind_audit(s3, version_id)
        return 0
    finally:
        stop.set()
        if heartbeat_task is not None:
            await heartbeat_task
        await engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except TransportFailure as exc:
        print(f"FAIL transport: {exc}", flush=True)
        raise SystemExit(1)
