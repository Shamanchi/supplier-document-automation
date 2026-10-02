"""Подбор позиций заявки: канон, каскад, песочница.

Песочница (`/matching/playground`) — не демонстрационная заглушка, а тот же
путь, которым пойдёт расчёт: разбор строки → каскад → документация. Отдельная
«игрушечная» реализация разошлась бы с боевой на первом же изменении порогов.
"""

from __future__ import annotations

import uuid
from datetime import date

from celery.result import AsyncResult
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.import_auth import get_import_admin, get_import_reader
from app.matching.canonical import CanonicalService
from app.matching.cascade import AUTO_ACCEPT, NEEDS_REVIEW, MatchService
from app.matching.certificates import CertificateService
from app.matching.embeddings import EmbeddingIndexService
from app.matching.requests import parse_text
from app.matching.variants import VariantService
from app.models import (
    CanonicalProduct,
    MatchStage,
    MergeStatus,
    PriceFile,
    PriceRequest,
    PriceRequestLine,
    PriceVersion,
    Project,
    Supplier,
    SupplierPriceItem,
    VersionStatus,
)
from app.tasks import rebuild_canonical_layer, rebuild_search_index
from app.worker import celery_app
from app.project_context import get_current_project

router = APIRouter(prefix="/matching", tags=["matching"])

# Каталог компании — основной режим. Проект остаётся границей хранения и
# доступен как явное сужение, а не как случайный фильтр по умолчанию.
ALL_PROJECTS = "all"
CURRENT_PROJECT = "current"


def _project_scope(payload: dict, project: Project) -> uuid.UUID | None:
    """Каталог компании по умолчанию; `current` оставляет только текущий проект."""
    requested = str(payload.get("project", "")).strip().lower()
    return project.id if requested == CURRENT_PROJECT else None


def _candidate_dict(candidate) -> dict[str, object]:
    return {
        "canonical_id": candidate.canonical_id,
        "title": candidate.title,
        "article": candidate.normalized_article,
        "merge_status": candidate.merge_status,
        "stage": candidate.stage,
        "score": candidate.score,
    }


def _certificate_dict(info) -> dict[str, object]:
    return {
        "available": info.available,
        "verdict": info.verdict,
        "registration_number": info.registration_number,
        "valid_until": info.valid_until,
        "manufacturer": info.manufacturer,
        "matched_by": info.matched_by,
        "candidates": info.candidates,
        "needs_manual_review": info.needs_manual_review,
        "review_reasons": list(info.review_reasons),
        "confirmed_by": info.document_confirmed_by,
        "attachable": info.attachable,
        "reason_unavailable": info.reason_unavailable,
    }


@router.post("/canonical/rebuild")
async def rebuild_canonical(
    limit: int = Query(default=0, ge=0, le=200_000),
    background: bool = Query(default=True),
    session: AsyncSession = Depends(get_session),
    _admin=Depends(get_import_admin),
) -> dict[str, object]:
    """Пересобрать канонический слой из разобранных позиций прайсов.

    По умолчанию — фоновой задачей. На боевом каталоге это минуты работы, а
    синхронный запрос столько не живёт: обратный прокси рвёт соединение,
    запрос отменяется вместе с транзакцией, и слой остаётся пустым. Синхронный
    режим оставлен для тестов и небольших выборок (`background=false`).
    """
    if background:
        task = rebuild_canonical_layer.delay(limit)
        return {"mode": "background", "task_id": task.id}
    result = await CanonicalService(session).rebuild(limit=limit)
    await session.commit()
    return {
        "mode": "sync",
        "items": result.items,
        "canonicals": result.canonicals,
        "disputed": result.disputed,
    }


@router.post("/index/rebuild")
async def rebuild_index(
    rebuild: bool = Query(default=False),
    _admin=Depends(get_import_admin),
) -> dict[str, object]:
    """Собрать индекс подбора по всем версиям, участвующим в поиске.

    Без него векторная ступень каскада не отвечает вовсе, и подбор незаметно
    деградирует до сравнения строк.
    """
    task = rebuild_search_index.delay(rebuild)
    return {"mode": "background", "task_id": task.id}


@router.get("/tasks/{task_id}")
async def task_status(task_id: str, _reader=Depends(get_import_reader)) -> dict[str, object]:
    """Состояние фоновой пересборки: запущена, готова или упала."""
    result = AsyncResult(task_id, app=celery_app)
    payload: dict[str, object] = {"task_id": task_id, "state": result.state}
    if result.successful():
        payload["result"] = str(result.result)
    elif result.failed():
        payload["error"] = str(result.result)
    return payload


@router.get("/canonical/stats")
async def canonical_stats(
    session: AsyncSession = Depends(get_session),
    _reader=Depends(get_import_reader),
) -> dict[str, int]:
    return await CanonicalService(session).stats()


@router.get("/canonical")
async def list_canonical(
    q: str = Query(default=""),
    merge_status: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    _reader=Depends(get_import_reader),
) -> dict[str, object]:
    query = select(CanonicalProduct)
    if q:
        query = query.where(CanonicalProduct.normalized_title.ilike(f"%{q.upper()}%"))
    if merge_status:
        query = query.where(CanonicalProduct.merge_status == merge_status)
    rows = list(await session.scalars(query.limit(limit)))
    return {
        "total": len(rows),
        "items": [
            {
                "canonical_id": str(row.id),
                "title": row.title,
                "article": row.normalized_article,
                "merge_status": row.merge_status,
                "sources_count": row.sources_count,
            }
            for row in rows
        ],
    }


@router.post("/canonical/{canonical_id}/merge-status")
async def set_merge_status(
    canonical_id: uuid.UUID,
    payload: dict = Body(...),
    session: AsyncSession = Depends(get_session),
    _admin=Depends(get_import_admin),
) -> dict[str, str]:
    status = str(payload.get("status", ""))
    if status not in {item.value for item in MergeStatus}:
        raise HTTPException(status_code=400, detail="недопустимый статус слияния")
    canonical = await CanonicalService(session).confirm(canonical_id, status=status)
    if canonical is None:
        raise HTTPException(status_code=404, detail="канонический товар не найден")
    await session.commit()
    return {"canonical_id": str(canonical.id), "merge_status": canonical.merge_status}


@router.post("/index/versions/{version_id}")
async def index_version(
    version_id: uuid.UUID,
    rebuild: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    _admin=Depends(get_import_admin),
) -> dict[str, object]:
    """Собрать индекс подбора по версии прайса.

    `rebuild=false` — расширение: досчитываются только новые и изменившиеся
    позиции. `rebuild=true` — пересборка версии целиком: строки прайса могли
    не только измениться, но и исчезнуть.
    """
    result = await EmbeddingIndexService(session).index_version(version_id, rebuild=rebuild)
    await session.commit()
    return {
        "version_id": result.version_id,
        "mode": result.mode,
        "items": result.items,
        "inserted": result.inserted,
        "reused": result.reused,
        "removed": result.removed,
    }


@router.delete("/index/versions/{version_id}")
async def drop_version_index(
    version_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _admin=Depends(get_import_admin),
) -> dict[str, int]:
    """Убрать версию из индекса: её позиции перестают отвечать на заявки."""
    removed = await EmbeddingIndexService(session).drop_version(version_id)
    await session.commit()
    return {"removed": removed}


@router.get("/index/stats")
async def index_stats(
    session: AsyncSession = Depends(get_session),
    _reader=Depends(get_import_reader),
) -> dict[str, object]:
    return await EmbeddingIndexService(session).stats()


@router.get("/catalog/stats")
async def catalog_stats(
    session: AsyncSession = Depends(get_session),
    project: Project = Depends(get_current_project),
    _reader=Depends(get_import_reader),
) -> dict[str, int]:
    """Сверка справочника поставщиков с реально доступным для подбора каталогом."""
    searchable = (VersionStatus.active.value,)
    suppliers_total = await session.scalar(select(func.count(Supplier.id))) or 0
    base = (
        select(
            func.count(func.distinct(PriceVersion.supplier_id)),
            func.count(func.distinct(PriceVersion.id)),
            func.count(SupplierPriceItem.id),
        )
        .select_from(PriceVersion)
        .join(SupplierPriceItem, SupplierPriceItem.price_version_id == PriceVersion.id)
        .where(PriceVersion.status.in_(searchable))
    )
    all_counts = (await session.execute(base)).one()
    current_counts = (
        await session.execute(
            base.join(PriceFile, PriceFile.id == PriceVersion.price_file_id).where(
                PriceFile.project_id == project.id
            )
        )
    ).one()
    return {
        "suppliers_total": int(suppliers_total),
        "suppliers_searchable": int(all_counts[0]),
        "versions_searchable": int(all_counts[1]),
        "positions_searchable": int(all_counts[2]),
        "current_project_suppliers": int(current_counts[0]),
        "current_project_versions": int(current_counts[1]),
        "current_project_positions": int(current_counts[2]),
    }


@router.post("/search")
async def search(
    payload: dict = Body(...),
    session: AsyncSession = Depends(get_session),
    project: Project = Depends(get_current_project),
    _reader=Depends(get_import_reader),
) -> dict[str, object]:
    """Подбор по одной строке — без разбора количества и без сертификатов."""
    project_id = _project_scope(payload, project)
    result = await MatchService(session).match(
        str(payload.get("query", "")),
        supplier=str(payload.get("supplier", "")).strip(),
        project_id=project_id,
        check_batch_id=_check_batch_id(payload),
    )
    await session.commit()
    return {
        "query": result.query,
        "project": ALL_PROJECTS if project_id is None else str(project_id),
        "decision": result.decision,
        "stage": result.stage,
        "candidates": [_candidate_dict(item) for item in result.candidates],
        "thresholds": {"auto": AUTO_ACCEPT, "review": NEEDS_REVIEW},
    }


def _check_batch_id(payload: dict) -> uuid.UUID | None:
    """Заявка, в контексте которой подбираются цены.

    Нужна, чтобы в выборку попал разовый счёт/КП именно этой заявки и не попал
    чужой: `SUPPLIER_QUOTE` вне своей заявки не участвует в подборе.
    """
    raw = str(payload.get("check_batch_id", "")).strip()
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="check_batch_id не UUID") from None


def _variant_dict(variant) -> dict[str, object]:
    return {
        "canonical_id": variant.canonical_id,
        "title": variant.title,
        "article": variant.article,
        "merge_status": variant.merge_status,
        "matched_designation": variant.matched_designation,
        "relevance": variant.relevance,
        "offers": [
            {
                "supplier": offer.supplier,
                "supplier_id": offer.supplier_id,
                "price_item_id": offer.price_item_id,
                "price_version_id": offer.price_version_id,
                "document_type": offer.document_type,
                "name": offer.name,
                "unit": offer.unit,
                "price": float(offer.price) if offer.price is not None else None,
                "currency": offer.currency,
            }
            for offer in variant.offers
        ],
    }


@router.post("/variants")
async def variants(
    payload: dict = Body(...),
    session: AsyncSession = Depends(get_session),
    project: Project = Depends(get_current_project),
    _reader=Depends(get_import_reader),
) -> dict[str, object]:
    """Весь ряд позиций по обозначению из строки заявки.

    Отличается от `/search` контрактом: не «лучший кандидат», а полная выборка
    исполнений — «ОП-4 (з) ВСЕ ЗПУ Россия», «(з) АВСЕ ЗПУ Китай» и так далее у
    всех поставщиков, с ценами. Выбор исполнения за менеджером, и делать его по
    трём кандидатам вслепую неправильно.
    """
    query = str(payload.get("query", ""))
    supplier = str(payload.get("supplier", "")).strip()
    limit = int(payload.get("limit", 100))
    project_id = _project_scope(payload, project)
    batch_id = _check_batch_id(payload)
    found, rows = await VariantService(session).by_query(
        query,
        supplier=supplier,
        project_id=project_id,
        check_batch_id=batch_id,
        limit=max(1, min(limit, 200)),
    )
    return {
        "query": query,
        "project": ALL_PROJECTS if project_id is None else str(project_id),
        "designations": found,
        "total": len(rows),
        "items": [_variant_dict(row) for row in rows],
    }


@router.post("/playground")
async def playground(
    payload: dict = Body(...),
    on: date | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    project: Project = Depends(get_current_project),
    _reader=Depends(get_import_reader),
) -> dict[str, object]:
    """Прогнать текст заявки целиком: разбор → подбор → документация.

    Тот же путь, что у боевого расчёта. Сертификаты запрашиваются только для
    строки, где подбор дал результат: спрашивать документацию для «не найдено»
    бессмысленно.
    """
    text = str(payload.get("text", ""))
    want_certificates = bool(payload.get("with_certificates", True))
    want_variants = bool(payload.get("with_variants", True))
    supplier = str(payload.get("supplier", "")).strip()
    project_id = _project_scope(payload, project)
    batch_id = _check_batch_id(payload)
    checked_on = on or date.today()

    matcher = MatchService(session)
    certificates = CertificateService(session)
    variant_service = VariantService(session)
    lines: list[dict[str, object]] = []

    for parsed in parse_text(text):
        result = await matcher.match(
            parsed.query_name,
            supplier=supplier,
            project_id=project_id,
            check_batch_id=batch_id,
        )
        entry: dict[str, object] = {
            "ordinal": parsed.ordinal,
            "source_line": parsed.source_line,
            "query_name": parsed.query_name,
            "quantity": float(parsed.quantity) if parsed.quantity is not None else None,
            "unit_code": parsed.unit_code,
            "warnings": list(parsed.warnings),
            "decision": result.decision,
            "stage": result.stage,
            "candidates": [_candidate_dict(item) for item in result.candidates],
        }
        if want_variants:
            # Менеджеру нужен весь ряд исполнений, а не тройка лучших: по
            # «ОП-4 закачной» он выбирает между «ВСЕ ЗПУ Россия» и «АВСЕ ЗПУ
            # Китай», и оба варианта должны быть на экране.
            found, rows = await variant_service.by_query(
                parsed.query_name,
                supplier=supplier,
                project_id=project_id,
                check_batch_id=batch_id,
                limit=50,
            )
            entry["designations"] = found
            entry["variants"] = [_variant_dict(row) for row in rows]
        if want_certificates and result.best is not None:
            info = await certificates.for_product(
                product=result.best.title,
                position_ref=f"line-{parsed.ordinal}",
                on=checked_on,
            )
            entry["certificate"] = _certificate_dict(info)
            canonical = await session.get(CanonicalProduct, uuid.UUID(result.best.canonical_id))
            if canonical is not None:
                await certificates.save_link(
                    canonical=canonical,
                    info=info,
                    matched_key=parsed.query_name,
                    checked_on=checked_on,
                )
        lines.append(entry)

    await session.commit()
    return {
        "checked_on": checked_on.isoformat(),
        "supplier": supplier,
        "project": ALL_PROJECTS if project_id is None else str(project_id),
        "certificates_configured": certificates.configured,
        "thresholds": {"auto": AUTO_ACCEPT, "review": NEEDS_REVIEW},
        "lines": lines,
    }


def _request_line_dict(line, result, found, rows, include_variants):
    """Ответ для строки заявки: как песочница плюс то, что записано в БД."""
    entry: dict[str, object] = {
        "line_id": str(line.id),
        "ordinal": line.ordinal,
        "source_line": line.source_line,
        "query_name": line.query_name,
        "quantity": float(line.quantity) if line.quantity is not None else None,
        "unit_code": line.unit_code,
        "warnings": [],
        "decision": result.decision,
        "stage": result.stage,
        "candidates": [_candidate_dict(item) for item in result.candidates],
        "matched_canonical_id": str(line.matched_canonical_id)
        if line.matched_canonical_id is not None
        else None,
        "matched_stage": line.match_stage,
        "matched_score": line.match_score,
    }
    if include_variants:
        entry["designations"] = found
        entry["variants"] = [_variant_dict(row) for row in rows]
    return entry


def _record_match(line, result) -> None:
    """Персист результата подбора в строку заявки.

    Уверенное совпадение (auto/review) закрепляется каноном, чтобы расчёт
    знал, что менеджеру показывать дальше; нераспознанное остаётся без канона,
    но ступень и балл лучшей догадки сохраняются — иначе после подбора не
    видно, что именно искали и почему решили не брать.
    """
    best = result.best
    if result.decision in {"auto", "review"} and best is not None:
        line.matched_canonical_id = uuid.UUID(best.canonical_id)
    else:
        line.matched_canonical_id = None
    line.match_stage = result.stage if best is not None else MatchStage.none.value
    line.match_score = best.score if best is not None else None


@router.post("/request/{request_id}")
async def run_request(
    request_id: uuid.UUID,
    payload: dict = Body(default={}),
    session: AsyncSession = Depends(get_session),
    project: Project = Depends(get_current_project),
    _reader=Depends(get_import_reader),
) -> dict[str, object]:
    """Прогнать подбор по строкам сохранённой заявки и записать результат.

    Тот же путь, что песочница (разбор → каскад → выборка исполнений), но
    источник строк — заявка из БД, а результат не выбрасывается: каждая строка
    получает matched_canonical_id/match_stage/match_score, и по ним расчёт
    превращается в КП.
    """
    request = await session.get(
        PriceRequest, request_id, options=[selectinload(PriceRequest.lines)]
    )
    if request is None:
        raise HTTPException(status_code=404, detail="заявка не найдена")

    want_variants = bool(payload.get("with_variants", True))
    supplier = str(payload.get("supplier", "")).strip()
    project_id = _project_scope(payload, project)

    matcher = MatchService(session)
    variant_service = VariantService(session)
    lines: list[dict[str, object]] = []
    for line in sorted(request.lines, key=lambda row: row.ordinal):
        result = await matcher.match(
            line.query_name,
            supplier=supplier,
            project_id=project_id,
            check_batch_id=request.check_batch_id,
        )
        found: list[str] = []
        rows = []
        if want_variants:
            found, rows = await variant_service.by_query(
                line.query_name,
                supplier=supplier,
                project_id=project_id,
                check_batch_id=request.check_batch_id,
                limit=50,
            )
        _record_match(line, result)
        lines.append(_request_line_dict(line, result, found, rows, want_variants))

    await session.commit()
    return {
        "request_id": str(request.id),
        "supplier": supplier,
        "project": ALL_PROJECTS if project_id is None else str(project_id),
        "thresholds": {"auto": AUTO_ACCEPT, "review": NEEDS_REVIEW},
        "lines": lines,
    }


@router.post("/request/{request_id}/lines/{line_id}/select")
async def select_line_canonical(
    request_id: uuid.UUID,
    line_id: uuid.UUID,
    payload: dict = Body(...),
    session: AsyncSession = Depends(get_session),
    _admin=Depends(get_import_admin),
) -> dict[str, object]:
    """Ручной выбор канона для строки, которую автоподбор не распознал.

    Запись не трогает каскад: это решение менеджера о том, какое изделие имел
    в виду клиент, и оно ложится в строку как match_stage=manual. Строка
    проверяется на принадлежность заявке — чужую строку выбрать нельзя.
    """
    line = await session.scalar(
        select(PriceRequestLine).where(
            PriceRequestLine.id == line_id,
            PriceRequestLine.request_id == request_id,
        )
    )
    if line is None:
        raise HTTPException(status_code=404, detail="строка заявки не найдена")
    try:
        canonical_id = uuid.UUID(str(payload.get("canonical_id", "")))
    except ValueError:
        raise HTTPException(status_code=400, detail="нужен корректный canonical_id")
    canonical = await session.get(CanonicalProduct, canonical_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="канонический товар не найден")

    line.matched_canonical_id = canonical.id
    line.match_stage = "manual"
    line.match_score = None
    payload_out = {
        "request_id": str(request_id),
        "line_id": str(line.id),
        "canonical_id": str(canonical.id),
        "title": canonical.title,
        "matched_stage": line.match_stage,
        "matched_score": line.match_score,
    }
    await session.commit()
    return payload_out


@router.post("/synonyms")
async def remember_choice(
    payload: dict = Body(...),
    session: AsyncSession = Depends(get_session),
    _admin=Depends(get_import_admin),
) -> dict[str, str]:
    """Запомнить выбор менеджера: обучение подбора одним кликом."""
    phrase = str(payload.get("phrase", "")).strip()
    canonical_id = str(payload.get("canonical_id", "")).strip()
    if not phrase or not canonical_id:
        raise HTTPException(status_code=400, detail="нужны phrase и canonical_id")
    canonical = await session.get(CanonicalProduct, uuid.UUID(canonical_id))
    if canonical is None:
        raise HTTPException(status_code=404, detail="канонический товар не найден")
    synonym = await MatchService(session).remember(phrase, canonical.id)
    await session.commit()
    return {
        "synonym_id": str(synonym.id),
        "normalized_phrase": synonym.normalized_phrase,
        "canonical_id": str(canonical.id),
    }
