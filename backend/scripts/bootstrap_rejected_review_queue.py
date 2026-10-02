#!/usr/bin/env python3
"""Materialize a bounded offline retry set as normal import-review queue rows."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mimetypes
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import func, select

from app import storage
from app.db import SessionLocal
from app.import_auth import _internal_ui_client
from app.models import (
    PriceFile,
    PriceImport,
    PriceImportEvent,
    PriceImportReview,
    PriceImportStatus,
    PriceVersion,
    RuleGenerationAttempt,
    Supplier,
    VersionStatus,
)
from app.parsing import loaders
from app.parsing.rule_proposal import build_tabular_section_contexts
from app.review_bootstrap import select_retry_entries
from app.review_service import section_context_hash, sync_review_sections


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _prepare(selected: list[dict], corpus: Path) -> tuple[list[dict], list[dict]]:
    prepared: list[dict] = []
    skipped: list[dict] = []
    by_price: dict[str, list[dict]] = {}
    for row in selected:
        by_price.setdefault(str(row["price"]), []).append(row)
    for relative_path, rows in by_price.items():
        source = corpus / relative_path
        if not source.is_file():
            skipped.extend(
                {"price": relative_path, "section_id": row["section_id"], "reason": "source_missing"}
                for row in rows
            )
            continue
        data = source.read_bytes()
        try:
            loader = loaders.detect_loader(data, source.name)
            grids = loaders.load_grids_for(loader, data, source.name)
            contexts = {
                str(context["section_id"]): context
                for context in build_tabular_section_contexts(grids)
            }
        except Exception as exc:  # noqa: BLE001 - reported without source content
            skipped.extend(
                {
                    "price": relative_path,
                    "section_id": row["section_id"],
                    "reason": f"source_error:{type(exc).__name__}",
                }
                for row in rows
            )
            continue
        file_sha256 = hashlib.sha256(data).hexdigest()
        for row in rows:
            section_id = str(row["section_id"])
            context = contexts.get(section_id)
            if context is None:
                skipped.append(
                    {"price": relative_path, "section_id": section_id, "reason": "section_identity_mismatch"}
                )
                continue
            prepared.append(
                {
                    "price": relative_path,
                    "source": source,
                    "data": data,
                    "file_sha256": file_sha256,
                    "loader": loader,
                    "context": context,
                    "checkpoint": row,
                }
            )
    return prepared, skipped


async def _write(prepared: list[dict], checkpoint: dict) -> dict[str, int]:
    storage.ensure_buckets()
    created = 0
    existing = 0
    async with SessionLocal() as session:
        client = await _internal_ui_client(session)
        by_price: dict[str, list[dict]] = {}
        for row in prepared:
            by_price.setdefault(row["price"], []).append(row)
        for relative_path, rows in by_price.items():
            first = rows[0]
            sha256 = first["file_sha256"]
            external_id = f"review-bootstrap:{sha256}"
            supplier = (
                await session.execute(
                    select(Supplier).where(
                        Supplier.service_client_id == client.id,
                        Supplier.external_id == external_id,
                    )
                )
            ).scalar_one_or_none()
            if supplier is None:
                supplier = Supplier(
                    service_client_id=client.id,
                    external_id=external_id,
                    code=f"review-{client.id.hex[:8]}-{sha256[:16]}",
                    name=Path(relative_path).stem[:255],
                )
                session.add(supplier)
                await session.flush()
            idempotency_key = f"review-bootstrap-{sha256[:48]}"
            price_import = (
                await session.execute(
                    select(PriceImport).where(
                        PriceImport.service_client_id == client.id,
                        PriceImport.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if price_import is None:
                content_type = mimetypes.guess_type(first["source"].name)[0] or "application/octet-stream"
                storage_key = storage.put_price_file(
                    first["data"], sha256, first["source"].name, content_type
                )
                price_file = PriceFile(
                    supplier_id=supplier.id,
                    original_filename=first["source"].name,
                    storage_key=storage_key,
                    sha256=sha256,
                    size_bytes=len(first["data"]),
                    mime=content_type,
                )
                session.add(price_file)
                await session.flush()
                version_no = int(
                    await session.scalar(
                        select(func.coalesce(func.max(PriceVersion.version_no), 0)).where(
                            PriceVersion.supplier_id == supplier.id
                        )
                    )
                    or 0
                ) + 1
                version = PriceVersion(
                    supplier_id=supplier.id,
                    price_file_id=price_file.id,
                    version_no=version_no,
                    status=VersionStatus.validation_failed.value,
                    loader=first["loader"],
                )
                session.add(version)
                await session.flush()
                price_import = PriceImport(
                    service_client_id=client.id,
                    supplier_id=supplier.id,
                    price_file_id=price_file.id,
                    price_version_id=version.id,
                    idempotency_key=idempotency_key,
                    request_fingerprint=sha256,
                    status=PriceImportStatus.review_required.value,
                    stage=PriceImportStatus.review_required.value,
                    progress_percent=100,
                    loader=first["loader"],
                    finished_at=datetime.now(timezone.utc),
                )
                session.add(price_import)
                await session.flush()
                session.add(
                    PriceImportEvent(
                        price_import_id=price_import.id,
                        event_type="review_sections_bootstrapped",
                        to_status=PriceImportStatus.review_required.value,
                        payload={"file_sha256": sha256, "section_count": len(rows)},
                    )
                )
            review = (
                await session.execute(
                    select(PriceImportReview).where(
                        PriceImportReview.price_import_id == price_import.id
                    )
                )
            ).scalar_one_or_none()
            if review is None:
                review = PriceImportReview(
                    price_import_id=price_import.id,
                    created_by="bootstrap",
                    updated_by="bootstrap",
                )
                session.add(review)
                await session.flush()
            version_id = price_import.price_version_id
            attempts: list[RuleGenerationAttempt] = []
            for prepared_row in rows:
                context = prepared_row["context"]
                source_row = prepared_row["checkpoint"]
                section_id = str(context["section_id"])
                attempt = (
                    await session.execute(
                        select(RuleGenerationAttempt).where(
                            RuleGenerationAttempt.price_version_id == version_id,
                            RuleGenerationAttempt.section_id == section_id,
                        )
                    )
                ).scalar_one_or_none()
                if attempt is None:
                    attempt = RuleGenerationAttempt(
                        price_version_id=version_id,
                        price_import_id=price_import.id,
                        batch_id=uuid.uuid5(uuid.NAMESPACE_URL, f"review-bootstrap:{sha256}"),
                        section_id=section_id,
                        status="rejected",
                        provider=str(checkpoint.get("provider") or "deepseek"),
                        model=str(checkpoint.get("model") or "deepseek-v4-flash"),
                        request_settings=checkpoint.get("request_settings") or {},
                        prompt_version=str(context.get("schema_version") or "bootstrap"),
                        prompt_id="review-bootstrap",
                        source_file_sha256=sha256,
                        source_context_hash=section_context_hash(context),
                        input_context=context,
                        prompt="",
                        proposal=source_row.get("proposal"),
                        validation=source_row.get("validation"),
                        error_message=source_row.get("error"),
                    )
                    session.add(attempt)
                    created += 1
                else:
                    existing += 1
                attempts.append(attempt)
            await session.flush()
            await sync_review_sections(
                session,
                review=review,
                contexts=[row["context"] for row in rows],
                attempts=attempts,
            )
        await session.commit()
    return {"created": created, "existing": existing}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--telemetry", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--operation-count", type=int, default=105)
    parser.add_argument("--exclude-section-id", action="append", default=[])
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    checkpoint = json.loads(args.checkpoint.read_text(encoding="utf-8"))
    selected = select_retry_entries(
        checkpoint,
        _load_jsonl(args.telemetry),
        operation_count=args.operation_count,
        excluded_section_ids=set(args.exclude_section_id),
    )
    prepared, skipped = _prepare(selected, args.corpus)
    write_result = asyncio.run(_write(prepared, checkpoint)) if args.write and not skipped else {}
    result = {
        "expected": len(selected),
        "validated": len(prepared),
        "skipped": skipped,
        "write": write_result,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
