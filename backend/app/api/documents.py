# -*- coding: utf-8 -*-
"""Загрузка паспортов и сертификатов с автопривязкой к каноническим товарам.

``POST /documents`` принимает PDF, сохраняет его в объектное хранилище
(бакет ``product-docs``), распознаёт класс и наименование изделия по текстовому
слою и автоматически создаёт подтверждённые ``document_links`` к канонам по
обозначениям изделий.  Благодаря этим связям документ попадает в колонку
«Паспорт/сертификат» новых расчётов (см. ``matching/calculations.py``).

Если документ с таким sha256 уже загружен, повторный POST не создаёт дубликат,
а лишь дополняет привязки (например, после появления новых канонов).
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.auth import access_user_from_request
from app.db import get_session
from app.document_recognition import (
    DOC_CERTIFICATE,
    DOC_OTHER,
    DOC_PASSPORT,
    recognize_pdf,
)
from app.matching.document_binding import auto_bind_document, BindResult
from app.models import (
    CanonicalProduct,
    DocumentLink,
    ProductDocument,
    Supplier,
)

router = APIRouter(prefix="/documents", tags=["documents"])

_MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _document_dict(document: ProductDocument) -> dict[str, object]:
    return {
        "id": str(document.id),
        "kind": document.kind,
        "title": document.title,
        "registration_number": document.registration_number,
        "storage_key": document.storage_key,
        "sha256": document.sha256,
        "source": document.source,
        "created_at": (
            document.created_at.isoformat() if document.created_at else None
        ),
    }


async def _confirmed_by(
    request: Request, session: AsyncSession
) -> str:
    """Читаемый идентификатор действующего пользователя для confirmed_by."""
    try:
        user = access_user_from_request(request)
    except HTTPException:
        return "document_upload"
    try:
        # Канон фазы 3: таблица пользователей живёт в modules.identity
        # (легаси-модуль identity удалён шагом миграции 02).
        from app.modules.identity.models import User as IdentityUser

        identity = await session.get(IdentityUser, uuid.UUID(user.username))
        if identity is not None and identity.email:
            return identity.email
    except (ValueError, TypeError):
        pass
    return user.username


async def _resolve_supplier(
    session: AsyncSession,
    *,
    supplier_id: str | None,
    supplier_name: str | None,
) -> Supplier | None:
    supplier_uuid = (supplier_id or "").strip() or None
    name = (supplier_name or "").strip() or None
    if not supplier_uuid and not name:
        return None
    if supplier_uuid:
        try:
            parsed = uuid.UUID(supplier_uuid)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="supplier_id не является UUID"
            ) from exc
        supplier = await session.get(Supplier, parsed)
        if supplier is None:
            raise HTTPException(status_code=404, detail="Поставщик не найден")
        return supplier
    supplier = (
        await session.execute(
            select(Supplier)
            .where(Supplier.name.ilike(f"%{name}%"))
            .order_by(Supplier.name)
            .limit(1)
        )
    ).scalar_one_or_none()
    if supplier is None:
        raise HTTPException(status_code=404, detail="Поставщик не найден")
    return supplier


@router.post("", status_code=201)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    supplier_id: str | None = Form(default=None),
    supplier: str | None = Form(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Загрузить PDF и автоматически привязать его к канонам."""
    filename = file.filename or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=422, detail="Поддерживаются только PDF-файлы"
        )
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Пустой файл")
    if len(data) > _MAX_DOCUMENT_BYTES:
        raise HTTPException(
            status_code=422, detail="Файл больше 64 МБ — не поддерживается"
        )

    sha256 = hashlib.sha256(data).hexdigest()
    supplier_row = await _resolve_supplier(
        session, supplier_id=supplier_id, supplier_name=supplier
    )
    supplier_uuid = supplier_row.id if supplier_row is not None else None
    confirmed_by = await _confirmed_by(request, session)

    existing = (
        await session.execute(
            select(ProductDocument).where(ProductDocument.sha256 == sha256)
        )
    ).scalar_one_or_none()
    if existing is not None:
        outcome = await auto_bind_document(
            session,
            document=existing,
            product_name=existing.title or filename,
            supplier_id=supplier_uuid,
            confirmed_by=confirmed_by,
        )
        await session.commit()
        return {
            "status": "duplicate",
            "document": _document_dict(existing),
            **_bind_payload(outcome),
        }

    recognition = recognize_pdf(data, file=filename)
    if recognition.error:
        raise HTTPException(
            status_code=422,
            detail=f"Не удалось открыть/распознать PDF: {recognition.error}",
        )
    if not recognition.has_text_layer:
        raise HTTPException(
            status_code=422,
            detail=(
                "no_text_layer: в PDF нет текстового слоя — нужен OCR; "
                "автопривязка по наименованию невозможна"
            ),
        )
    if recognition.doc_class == DOC_OTHER:
        raise HTTPException(
            status_code=422,
            detail="Не удалось определить тип документа (паспорт/сертификат)",
        )

    product_name = (recognition.product_name or "").strip()
    if recognition.doc_class == DOC_PASSPORT:
        kind = "passport"
        source = "passport_extraction"
        title = product_name or filename
    else:
        kind = "certificate"
        source = "certificate_extraction"
        title = (
            recognition.registration_number
            or product_name
            or filename
        )

    storage_key = storage.put_product_document(
        data=data,
        sha256=sha256,
        filename=filename,
        content_type=file.content_type or "application/pdf",
    )
    document = ProductDocument(
        kind=kind,
        title=title,
        registration_number=recognition.registration_number or None,
        storage_key=storage_key,
        sha256=sha256,
        source=source,
    )
    session.add(document)
    await session.flush()

    outcome = await auto_bind_document(
        session,
        document=document,
        product_name=title,
        supplier_id=supplier_uuid,
        confirmed_by=confirmed_by,
    )
    if not outcome.matched and product_name != title:
        # Наименование не дало кандидатов — пробуем имя файла как источник
        # обозначения (например, «pasport_mppn-vzr-10-i-ge-u2.pdf»).
        fallback = await auto_bind_document(
            session,
            document=document,
            product_name=Path(filename).stem,
            supplier_id=supplier_uuid,
            confirmed_by=confirmed_by,
        )
        outcome.matched.extend(fallback.matched)
        outcome.created += fallback.created
        outcome.existing += fallback.existing

    await session.commit()
    return {
        "status": "created",
        "document": _document_dict(document),
        "recognition": {
            "doc_class": recognition.doc_class,
            "page_count": recognition.page_count,
            "product_name": product_name,
            "registration_number": recognition.registration_number,
        },
        **_bind_payload(outcome),
    }


def _bind_payload(outcome: BindResult) -> dict[str, object]:
    return {
        "created_links": outcome.created,
        "existing_links": outcome.existing,
        "candidates": [
            {
                "canonical_id": str(item.canonical_id),
                "title": item.title,
                "stage": item.stage,
            }
            for item in outcome.matched
        ],
    }


@router.get("")
async def list_documents(
    kind: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Список документов с числом подтверждённых привязок."""
    filters = []
    if kind:
        filters.append(ProductDocument.kind == kind)
    if q:
        pattern = f"%{q.strip()}%"
        filters.append(
            ProductDocument.title.ilike(pattern)
            | ProductDocument.storage_key.ilike(pattern)
        )
    total = (
        await session.execute(
            select(func.count(ProductDocument.id)).where(*filters)
        )
    ).scalar_one()
    documents = (
        await session.execute(
            select(ProductDocument)
            .where(*filters)
            .order_by(ProductDocument.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()

    counts: dict[uuid.UUID, int] = {}
    if documents:
        rows = (
            await session.execute(
                select(
                    DocumentLink.document_id,
                    func.count(DocumentLink.id),
                )
                .where(DocumentLink.document_id.in_([doc.id for doc in documents]))
                .group_by(DocumentLink.document_id)
            )
        ).all()
        counts = {document_id: count for document_id, count in rows}

    return {
        "total": total,
        "items": [
            {
                **_document_dict(document),
                "links_count": counts.get(document.id, 0),
            }
            for document in documents
        ],
    }


@router.get("/{document_id}")
async def get_document(
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Документ и его привязки к канонам."""
    document = await session.get(ProductDocument, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Документ не найден")
    rows = (
        await session.execute(
            select(DocumentLink, CanonicalProduct)
            .join(
                CanonicalProduct,
                CanonicalProduct.id == DocumentLink.canonical_product_id,
            )
            .where(DocumentLink.document_id == document_id)
            .order_by(DocumentLink.created_at)
        )
    ).all()
    return {
        **_document_dict(document),
        "links": [
            {
                "canonical_id": str(canonical.id),
                "canonical_title": canonical.title,
                "matched_by": link.matched_by,
                "confidence": link.confidence,
                "confirmed_by": link.confirmed_by,
                "confirmed_at": (
                    link.confirmed_at.isoformat() if link.confirmed_at else None
                ),
                "evidence": link.evidence or {},
            }
            for link, canonical in rows
        ],
    }


@router.get("/{document_id}/file")
async def download_document_file(
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Отдать PDF документа (из MinIO либо из локальной папки репозитория)."""
    document = await session.get(ProductDocument, document_id)
    if document is None or not document.storage_key:
        raise HTTPException(status_code=404, detail="Файл документа не найден")

    try:
        content = storage.get_product_document(document.storage_key)
        return Response(content=content, media_type="application/pdf")
    except Exception:
        # Stage-2 скрипты сохраняют storage_key как путь к файлу в репозитории
        # (например, OCR_prices/…/Паспорта/…pdf) — отдаём файл с диска.
        relative = document.storage_key.replace("\\", "/")
        candidates = [
            Path(relative),
            _REPO_ROOT / relative.lstrip("/"),
        ]
        for candidate in candidates:
            if candidate.is_file():
                return Response(
                    content=candidate.read_bytes(),
                    media_type="application/pdf",
                )
    raise HTTPException(status_code=404, detail="Файл документа не найден")
