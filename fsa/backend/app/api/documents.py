from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.config import settings
from app.db import get_session
from app.models import Document, DocumentCheck
from app.repository import RegistryRepository
from app.services.review import ReviewService, effective_state
from app.services.documents import DocumentService, UnsupportedDocument
from app.tasks import recheck_documents as recheck_documents_task
from app.tasks import recognize_document

router = APIRouter(prefix="/v1/documents", tags=["documents"])


def _service(request: Request, session: AsyncSession) -> DocumentService:
    return DocumentService(session, getattr(request.app.state, "ocr_client", None))


def _as_dict(document: Document, check: DocumentCheck | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": str(document.id),
        "sha256": document.sha256,
        "filename": document.filename,
        "source_path": document.source_path,
        "supplier": document.supplier,
        "media_type": document.media_type,
        "size_bytes": document.size_bytes,
        "storage_key": document.storage_key,
        "status": document.status,
        "doc_class": document.doc_class,
        "text_source": document.text_source,
        "engine": document.engine,
        "page_count": document.page_count,
        "mean_confidence": document.mean_confidence,
        "registration_number": document.registration_number_key,
        "error": document.error,
        "created_at": document.created_at.isoformat() if document.created_at else None,
        "recognized_at": (
            document.recognized_at.isoformat() if document.recognized_at else None
        ),
    }
    if check is not None:
        payload["check"] = {
            "verdict": check.verdict,
            "matched_by": check.matched_by,
            "checked_on": check.checked_on.isoformat(),
            "valid_until": check.valid_until.isoformat() if check.valid_until else None,
            "needs_manual_review": check.needs_manual_review,
            "review_reasons": check.review_reasons,
            "successor_score": check.successor_score,
        }
    return payload


@router.post("/upload")
async def upload(
    request: Request,
    file: UploadFile = File(...),
    source_path: str = Form(default=""),
    supplier: str = Form(default=""),
    force: bool = Form(default=False),
    background: bool = Form(default=False),
    on: date | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Загрузить документ, распознать при необходимости и сверить с реестром.

    Повторная загрузка того же файла не запускает OCR заново: идемпотентность
    держится на sha256 содержимого.

    `background=true` кладёт распознавание в очередь Celery и отвечает сразу.
    Это нужно для пачек: OCR скана занимает секунды, и держать на них
    HTTP-соединение незачем.
    """
    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="пустой документ")
    if len(payload) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="файл слишком большой")

    service = _service(request, session)
    if background:
        stored = await service.store_only(
            payload,
            filename=file.filename or "document",
            source_path=source_path,
            supplier=supplier,
        )
        await session.commit()
        task = recognize_document.delay(
            str(stored.document.id), (on or date.today()).isoformat()
        )
        return {
            "created": stored.created,
            "queued": True,
            "task_id": getattr(task, "id", "") or "",
            "document": _as_dict(stored.document),
        }

    try:
        outcome = await service.ingest(
            payload,
            filename=file.filename or "document",
            source_path=source_path,
            supplier=supplier,
            force=force,
        )
    except UnsupportedDocument as error:
        raise HTTPException(status_code=415, detail=str(error)) from error

    check = await service.check(
        outcome.document, RegistryRepository(session), on=on or date.today()
    )
    await session.commit()
    return {
        "created": outcome.created,
        "recognized": outcome.recognized,
        "reused": outcome.reused,
        "queued": False,
        "document": _as_dict(outcome.document, check),
    }


@router.post("/recheck-all")
async def recheck_all(
    on: date | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=5000),
) -> dict[str, object]:
    """Поставить в очередь пересверку всех распознанных документов."""
    task = recheck_documents_task.delay((on or date.today()).isoformat(), limit)
    return {"queued": True, "task_id": getattr(task, "id", "") or "", "limit": limit}


@router.get("")
async def list_documents(
    status: str | None = Query(default=None),
    verdict: str | None = Query(default=None),
    supplier: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    query = select(Document)
    if status:
        query = query.where(Document.status == status)
    if supplier:
        query = query.where(Document.supplier == supplier)
    if verdict:
        query = query.where(
            Document.id.in_(
                select(DocumentCheck.document_id).where(DocumentCheck.verdict == verdict)
            )
        )

    total = await session.scalar(
        select(func.count()).select_from(query.subquery())
    )
    rows = list(
        await session.scalars(
            query.order_by(Document.created_at.desc()).limit(limit).offset(offset)
        )
    )
    latest = {
        check.document_id: check
        for check in await session.scalars(
            select(DocumentCheck)
            .where(DocumentCheck.document_id.in_([row.id for row in rows] or [uuid.uuid4()]))
            .order_by(DocumentCheck.checked_on.desc())
        )
    }
    return {
        "total": total or 0,
        "items": [_as_dict(row, latest.get(row.id)) for row in rows],
    }


@router.get("/summary")
async def summary(session: AsyncSession = Depends(get_session)) -> dict[str, object]:
    """Сводка для дашборда: сколько документов и с какими вердиктами."""
    by_status = {
        row[0]: row[1]
        for row in await session.execute(
            select(Document.status, func.count()).group_by(Document.status)
        )
    }
    by_class = {
        row[0]: row[1]
        for row in await session.execute(
            select(Document.doc_class, func.count()).group_by(Document.doc_class)
        )
    }
    by_verdict = {
        row[0]: row[1]
        for row in await session.execute(
            select(DocumentCheck.verdict, func.count()).group_by(DocumentCheck.verdict)
        )
    }
    return {"by_status": by_status, "by_class": by_class, "by_verdict": by_verdict}


@router.get("/{document_id}")
async def get_document(
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    document = await session.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="документ не найден")
    check = await session.scalar(
        select(DocumentCheck)
        .where(DocumentCheck.document_id == document.id)
        .order_by(DocumentCheck.checked_on.desc())
        .limit(1)
    )
    payload = _as_dict(document, check)
    # Решение человека приходит вместе с карточкой: без него менеджер не
    # видит, подтверждал ли документ кто-то до него.
    service = ReviewService(session)
    decision = await service.latest_decision(document.id)
    state = effective_state(document, check, decision)
    payload["review"] = {
        "verdict": state.verdict,
        "decided": state.decided,
        "action": state.action,
        "reviewer": state.reviewer,
        "decided_at": state.decided_at,
        "stale": state.stale,
        "stale_reason": state.stale_reason,
        "attachable": state.attachable,
        "needs_review": state.needs_review,
        "comment": state.comment,
        "corrected_number": state.corrected_number,
    }
    payload["extracted"] = document.extracted
    payload["ocr_text_preview"] = (document.ocr_text or "")[:4000]
    if document.storage_key:
        try:
            payload["download_url"] = storage.presigned_get(document.storage_key)
        except Exception:  # noqa: BLE001 — ссылка не обязательна для карточки
            payload["download_url"] = None
    return payload


@router.post("/{document_id}/recheck")
async def recheck(
    request: Request,
    document_id: uuid.UUID,
    on: date | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Пересверить с реестром без повторного распознавания.

    Нужно потому, что вердикт меняется сам: у 23% актуальных сертификатов
    срок истекает в пределах года, а статус может смениться в любой момент.
    """
    document = await session.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="документ не найден")
    service = _service(request, session)
    check = await service.check(
        document, RegistryRepository(session), on=on or date.today()
    )
    await session.commit()
    return {"document": _as_dict(document, check)}
