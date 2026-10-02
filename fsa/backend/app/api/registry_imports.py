from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.config import settings
from app.db import get_session
from app.models import (
    RegistryImport,
    RegistryImportKind,
    RegistryImportStatus,
)
from app.services.registry_import import RegistryFileError, detect_kind, sha256_of
from app.tasks import import_registry_file

router = APIRouter(prefix="/v1/registry/imports", tags=["registry-imports"])


def _as_dict(record: RegistryImport) -> dict[str, object]:
    return {
        "id": str(record.id),
        "filename": record.filename,
        "kind": record.kind,
        "status": record.status,
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
        "certificates_loaded": record.certificates_loaded,
        "numbers_loaded": record.numbers_loaded,
        "error": record.error,
        "note": record.note,
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
    }


@router.post("")
async def upload_registry_file(
    file: UploadFile = File(...),
    note: str = Form(default=""),
    kind: str | None = Form(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Принять файл реестра и поставить импорт в очередь.

    Автоматической выкачки набора нет намеренно: файлы кладут руками, сервису
    нужен только приём. Вид содержимого определяется по самому файлу —
    расширение служит лишь подсказкой.
    """
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="пустой файл")
    if len(payload) > settings.registry_max_upload_bytes:
        raise HTTPException(status_code=413, detail="файл слишком большой")

    try:
        detected = RegistryImportKind(kind) if kind else detect_kind(file.filename or "", payload)
    except (RegistryFileError, ValueError) as error:
        raise HTTPException(status_code=415, detail=str(error)) from error

    digest = sha256_of(payload)
    storage_key = storage.put_document(
        payload, digest, file.filename or "registry", "application/octet-stream"
    )

    record = RegistryImport(
        source=file.filename or "upload",
        filename=file.filename or "registry",
        kind=detected.value,
        status=RegistryImportStatus.queued.value,
        storage_key=storage_key,
        size_bytes=len(payload),
        sha256=digest,
        note=note,
    )
    session.add(record)
    await session.commit()

    task = import_registry_file.delay(str(record.id))
    record.task_id = getattr(task, "id", "") or ""
    await session.commit()

    return {"import": _as_dict(record), "task_id": record.task_id}


@router.get("")
async def list_imports(
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    total = await session.scalar(select(func.count()).select_from(RegistryImport))
    rows = await session.scalars(
        select(RegistryImport).order_by(RegistryImport.started_at.desc()).limit(limit)
    )
    return {"total": total or 0, "items": [_as_dict(row) for row in rows]}


@router.get("/{import_id}")
async def get_import(
    import_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    record = await session.get(RegistryImport, import_id)
    if record is None:
        raise HTTPException(status_code=404, detail="загрузка не найдена")
    return _as_dict(record)
