#!/usr/bin/env python3
"""Пакетная загрузка сертификатов и паспортов: S3 → OCR → сверка с реестром.

Обход идемпотентен: документ, уже распознанный ранее, повторно через движок не
гоняется — в корпусе один и тот же сертификат лежит у нескольких поставщиков,
и платить за него дважды незачем.

    python3 scripts/import_documents.py --root "../../OCR_prices/Прайсы/прайсы"
    python3 scripts/import_documents.py --root ... --limit 20 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import storage  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.engines import EngineEndpoint, default_base_url  # noqa: E402
from app.ocr_client import OCRClient  # noqa: E402
from app.repository import RegistryRepository  # noqa: E402
from app.services.documents import DocumentService, UnsupportedDocument  # noqa: E402

SUPPORTED_SUFFIXES = {".pdf", ".png"}


def supplier_of(path: Path, root: Path) -> str:
    """Поставщик — первый каталог относительно корня обхода."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        return ""
    return relative.parts[0] if len(relative.parts) > 1 else ""


def collect(root: Path, limit: int | None) -> list[Path]:
    files = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )
    return files[:limit] if limit else files


async def run(args: argparse.Namespace) -> int:
    root = args.root.resolve()
    files = collect(root, args.limit)
    print(f"файлов к обработке: {len(files)} (корень {root})")
    if args.dry_run:
        return 0

    if not args.no_storage:
        storage.ensure_buckets()

    client = OCRClient(
        EngineEndpoint(
            name=settings.ocr_engine,
            base_url=settings.ocr_engine_url
            or default_base_url(settings.ocr_engine, settings.ocr_engine_host),
            service_key=settings.ocr_engine_key,
        ),
        timeout=settings.ocr_request_timeout,
    )

    on = args.on or date.today()
    stats: Counter[str] = Counter()
    started = time.monotonic()

    try:
        for position, path in enumerate(files, start=1):
            payload = path.read_bytes()
            async with SessionLocal() as session:
                service = DocumentService(
                    session, client, store=not args.no_storage
                )
                try:
                    outcome = await service.ingest(
                        payload,
                        filename=path.name,
                        source_path=str(path.relative_to(root)),
                        supplier=supplier_of(path, root),
                        force=args.force,
                    )
                except UnsupportedDocument:
                    stats["unsupported"] += 1
                    continue

                check = await service.check(
                    outcome.document, RegistryRepository(session), on=on
                )
                await session.commit()

                stats["всего"] += 1
                stats["новых" if outcome.created else "повторных"] += 1
                if outcome.reused:
                    stats["без повторного OCR"] += 1
                stats[f"источник:{outcome.document.text_source}"] += 1
                stats[f"статус:{outcome.document.status}"] += 1
                if check is not None:
                    stats[f"вердикт:{check.verdict}"] += 1

            if position % 25 == 0 or position == len(files):
                elapsed = time.monotonic() - started
                print(f"  {position}/{len(files)} за {elapsed:.0f}s")
    finally:
        await client.close()

    print("\nитог:")
    for key, value in sorted(stats.items()):
        print(f"  {key:28} {value}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--force", action="store_true", help="перераспознать заново")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--no-storage", action="store_true", help="не класть файлы в S3"
    )
    parser.add_argument("--on", type=date.fromisoformat)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
