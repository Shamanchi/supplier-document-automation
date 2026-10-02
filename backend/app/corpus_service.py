"""Corpus run: walk a directory of price files, run the standard
upload+parse pipeline for each file in-process, record per-file results and a
final aggregate report (Markdown + JSON in MinIO).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import storage
from app.models import (
    CorpusRun,
    CorpusRunFile,
    PriceFile,
    PriceVersion,
    Supplier,
    VersionStatus,
)
from app.parse_service import parse_and_persist

logger = logging.getLogger(__name__)

PRICE_EXTENSIONS = {".xlsx", ".xlsm", ".xls", ".pdf", ".docx", ".doc", ".txt", ".csv"}
SKIP_DIRS = {
    "analysis",
    "normalized",
    "other",
    "complex",
    "need_ocr",
    ".venv",
    "__pycache__",
}
SKIP_NAME_MARKERS = ("manifest",)

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

_MIME_BY_EXT = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xls": "application/vnd.ms-excel",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".txt": "text/plain",
    ".csv": "text/csv",
}


def translit(text: str) -> str:
    return "".join(_TRANSLIT.get(ch, _TRANSLIT.get(ch.lower(), ch)) for ch in text)


def supplier_code_from_filename(filename: str) -> str:
    """First two tokens of the transliterated file stem, e.g.
    '103_ССД_противопожарное_оборудование.xlsx' -> '103-ssd'."""
    stem = Path(filename).stem
    slug = translit(stem).lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", slug) if t]
    if not tokens:
        return "unknown"
    return "-".join(tokens[:2])[:64]


def collect_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(p.startswith(".") or p in SKIP_DIRS for p in rel_parts[:-1]):
            continue
        name = path.name
        if name.startswith("."):
            continue
        if any(marker in name.lower() for marker in SKIP_NAME_MARKERS):
            continue
        if path.suffix.lower() not in PRICE_EXTENSIONS:
            continue
        files.append(path)
    return files


_PROBLEM_QUEUE_DIRS = {
    VersionStatus.complex.value: "complex",
    VersionStatus.needs_ocr.value: "need_ocr",
}


def queue_problem_file(root: Path, file_path: Path, status: str) -> Path | None:
    """Copy a follow-up file into a safe, non-recursive corpus queue.

    The source remains untouched: a corpus may be shared with an upstream
    importer, so copying is safer than moving.  ``collect_files`` excludes the
    two queue directories on the next run, preventing retry loops and model
    spend on the same unresolved source.
    """
    queue_name = _PROBLEM_QUEUE_DIRS.get(status)
    if queue_name is None:
        return None
    try:
        relative_path = file_path.relative_to(root)
    except ValueError:
        return None
    destination = root / queue_name / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(file_path, destination)
    return destination


async def run_corpus(
    session_factory: async_sessionmaker[AsyncSession],
    corpus_run_id: uuid.UUID,
    path: str,
    force: bool = False,
) -> str:
    async with session_factory() as session:
        run = await session.get(CorpusRun, corpus_run_id)
        if run is None:
            return "not_found"
        root = Path(path)
        if not root.is_dir():
            run.status = "failed"
            run.error_message = f"Каталог не найден: {path}"
            run.finished_at = datetime.now(timezone.utc)
            await session.commit()
            return run.status

        files = collect_files(root)
        run.total_files = len(files)
        run.status = "running"
        await session.commit()

    for file_path in files:
        # One session per file: a per-file crash must not poison the run.
        async with session_factory() as session:
            started = time.monotonic()
            row = CorpusRunFile(
                corpus_run_id=corpus_run_id,
                filename=str(file_path.name),
                status="failed",
            )
            try:
                await _process_file(session, row, file_path, force)
            except Exception as exc:  # noqa: BLE001 — the run must survive any file
                logger.exception("Corpus run: файл %s не обработан", file_path)
                await session.rollback()
                row = CorpusRunFile(
                    corpus_run_id=corpus_run_id,
                    filename=str(file_path.name),
                    status="failed",
                    message=f"{type(exc).__name__}: {exc}",
                )
            queued_path = queue_problem_file(root, file_path, _base_status(row.status))
            if queued_path is not None:
                suffix = f"Копия направлена в очередь `{queued_path.relative_to(root)}`"
                row.message = f"{row.message}. {suffix}" if row.message else suffix
            row.duration_ms = int((time.monotonic() - started) * 1000)
            session.add(row)
            run = await session.get(CorpusRun, corpus_run_id)
            run.processed += 1
            await session.commit()

    async with session_factory() as session:
        run = await session.get(CorpusRun, corpus_run_id)
        totals, file_rows = await _compute_totals(session, corpus_run_id)
        run.totals = totals
        run.status = "finished"
        run.finished_at = datetime.now(timezone.utc)
        try:
            run.report_storage_key = _store_reports(run, file_rows)
        except Exception:  # noqa: BLE001 — a report upload failure must not fail the run
            logger.exception("Corpus run: не удалось сохранить отчёт в MinIO")
        await session.commit()
        return run.status


async def _process_file(
    session: AsyncSession, row: CorpusRunFile, file_path: Path, force: bool
) -> None:
    data = file_path.read_bytes()
    if not data:
        row.status = "failed"
        row.message = "Пустой файл"
        return
    sha256 = hashlib.sha256(data).hexdigest()
    row.sha256 = sha256

    supplier = await _get_or_create_supplier(session, file_path.name)

    existing = (
        await session.execute(
            select(PriceVersion)
            .join(PriceFile, PriceVersion.price_file_id == PriceFile.id)
            .where(
                PriceVersion.supplier_id == supplier.id,
                PriceFile.sha256 == sha256,
            )
            .order_by(PriceVersion.version_no.desc())
        )
    ).scalars().first()

    if existing is not None and not force:
        row.price_version_id = existing.id
        row.loader = existing.loader
        row.rule_match = existing.rule_match
        row.parse_rule_id = existing.parse_rule_id
        row.status = f"reused:{existing.status}"
        row.item_count = existing.item_count
        row.errors_count = existing.errors_count
        row.warnings_count = existing.warnings_count
        row.quality_score = existing.quality_score
        row.message = "Файл уже загружен (sha256 совпал) — версия переиспользована"
        return

    ext = file_path.suffix.lower()
    mime = _MIME_BY_EXT.get(ext, "application/octet-stream")
    storage_key = storage.put_price_file(data, sha256, file_path.name, mime)
    price_file = PriceFile(
        supplier_id=supplier.id,
        original_filename=file_path.name,
        storage_key=storage_key,
        sha256=sha256,
        size_bytes=len(data),
        mime=mime,
    )
    session.add(price_file)
    await session.flush()

    max_no = (
        await session.execute(
            select(func.coalesce(func.max(PriceVersion.version_no), 0)).where(
                PriceVersion.supplier_id == supplier.id
            )
        )
    ).scalar_one()
    version = PriceVersion(
        supplier_id=supplier.id,
        price_file_id=price_file.id,
        version_no=max_no + 1,
        status=VersionStatus.parsing.value,
    )
    session.add(version)
    await session.flush()

    try:
        await parse_and_persist(session, version, price_file, data)
    except Exception as exc:  # noqa: BLE001 — parse crash lands in the version status
        version.status = VersionStatus.failed.value
        version.error_message = f"{type(exc).__name__}: {exc}"

    row.price_version_id = version.id
    row.loader = version.loader
    row.rule_match = version.rule_match
    row.parse_rule_id = version.parse_rule_id
    row.status = version.status
    row.item_count = version.item_count
    row.errors_count = version.errors_count
    row.warnings_count = version.warnings_count
    row.quality_score = version.quality_score
    row.message = version.error_message


async def _get_or_create_supplier(session: AsyncSession, filename: str) -> Supplier:
    code = supplier_code_from_filename(filename)
    supplier = (
        await session.execute(select(Supplier).where(Supplier.code == code))
    ).scalar_one_or_none()
    if supplier is None:
        supplier = Supplier(code=code, name=Path(filename).stem[:255])
        session.add(supplier)
        await session.flush()
    return supplier


def _base_status(status: str) -> str:
    return status.split(":", 1)[1] if status.startswith("reused:") else status


async def _compute_totals(
    session: AsyncSession, corpus_run_id: uuid.UUID
) -> tuple[dict, list[CorpusRunFile]]:
    rows = (
        (
            await session.execute(
                select(CorpusRunFile)
                .where(CorpusRunFile.corpus_run_id == corpus_run_id)
                .order_by(CorpusRunFile.filename)
            )
        )
        .scalars()
        .all()
    )
    parsed_ok = [r for r in rows if _base_status(r.status) in ("validated", "active")]
    quality = [r.quality_score for r in parsed_ok if r.quality_score is not None]
    totals = {
        "files": len(rows),
        "parsed_ok": len(parsed_ok),
        "by_rule_exact": sum(1 for r in parsed_ok if r.rule_match == "exact"),
        "auto_parsed": sum(1 for r in parsed_ok if r.rule_match in ("none", None)),
        "needs_mapping": sum(1 for r in rows if _base_status(r.status) == "needs_mapping"),
        "complex": sum(1 for r in rows if _base_status(r.status) == "complex"),
        "needs_ocr": sum(1 for r in rows if _base_status(r.status) == "needs_ocr"),
        "unsupported": sum(1 for r in rows if _base_status(r.status) == "unsupported_format"),
        "failed": sum(
            1 for r in rows if _base_status(r.status) in ("failed", "validation_failed")
        ),
        "total_items": sum(r.item_count for r in rows),
        "avg_quality": round(sum(quality) / len(quality), 4) if quality else None,
    }
    return totals, rows


def _store_reports(run: CorpusRun, rows: list[CorpusRunFile]) -> str:
    json_payload = {
        "corpus_run_id": str(run.id),
        "source_path": run.source_path,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "totals": run.totals,
        "files": [
            {
                "filename": r.filename,
                "sha256": r.sha256,
                "loader": r.loader,
                "rule_match": r.rule_match,
                "parse_rule_id": str(r.parse_rule_id) if r.parse_rule_id else None,
                "price_version_id": str(r.price_version_id) if r.price_version_id else None,
                "status": r.status,
                "item_count": r.item_count,
                "errors_count": r.errors_count,
                "warnings_count": r.warnings_count,
                "quality_score": r.quality_score,
                "duration_ms": r.duration_ms,
                "message": r.message,
            }
            for r in rows
        ],
    }
    md = _markdown_report(run, rows)

    md_key = f"reports/corpus_run_{run.id}.md"
    json_key = f"reports/corpus_run_{run.id}.json"
    storage.put_report(md.encode("utf-8"), md_key, "text/markdown")
    storage.put_report(
        json.dumps(json_payload, ensure_ascii=False, indent=2).encode("utf-8"),
        json_key,
        "application/json",
    )
    return md_key


def _markdown_report(run: CorpusRun, rows: list[CorpusRunFile]) -> str:
    t = run.totals or {}
    lines = [
        f"# Отчёт прогона корпуса {run.id}",
        "",
        f"- Каталог: `{run.source_path}`",
        f"- Начат: {run.started_at}",
        f"- Завершён: {run.finished_at}",
        "",
        "## Агрегаты",
        "",
        f"- Файлов: {t.get('files')}",
        f"- Разобрано успешно: {t.get('parsed_ok')}",
        f"- По правилам (exact): {t.get('by_rule_exact')}",
        f"- Через авторазбор: {t.get('auto_parsed')}",
        f"- needs_mapping: {t.get('needs_mapping')}",
        f"- complex: {t.get('complex')}",
        f"- needs_ocr: {t.get('needs_ocr')}",
        f"- unsupported_format: {t.get('unsupported')}",
        f"- failed: {t.get('failed')}",
        f"- Всего позиций: {t.get('total_items')}",
        f"- Средний quality: {t.get('avg_quality')}",
        "",
        "## Пофайловая таблица",
        "",
        "| Файл | Загрузчик | Rule match | Статус | Позиций | Ошибок | Предупр. | Quality | мс | Сообщение |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        msg = (r.message or "").replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {r.filename} | {r.loader or ''} | {r.rule_match or ''} | {r.status} "
            f"| {r.item_count} | {r.errors_count} | {r.warnings_count} "
            f"| {r.quality_score if r.quality_score is not None else ''} "
            f"| {r.duration_ms or ''} | {msg} |"
        )
    lines.append("")
    return "\n".join(lines)
