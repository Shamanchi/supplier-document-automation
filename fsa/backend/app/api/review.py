from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Document, DocumentDecision
from app.repository import RegistryRepository
from app.services.review import ReviewService, UnknownDecision, effective_state

router = APIRouter(prefix="/v1/review", tags=["review"])


def _decision_dict(decision: DocumentDecision) -> dict[str, object]:
    return {
        "id": str(decision.id),
        "action": decision.action,
        "reviewer": decision.reviewer,
        "comment": decision.comment,
        "verdict_at_decision": decision.verdict_at_decision,
        "number_at_decision": decision.number_at_decision,
        "corrected_number": decision.corrected_number,
        "corrected_verdict": decision.corrected_verdict,
        "corrected_valid_until": decision.corrected_valid_until.isoformat()
        if decision.corrected_valid_until
        else None,
        "created_at": decision.created_at.isoformat() if decision.created_at else None,
    }


@router.get("/queue")
async def queue(
    state: str = Query(default="pending", pattern="^(pending|decided|all)$"),
    supplier: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Очередь на подтверждение человеком.

    `pending` — документ никто не смотрел либо подтверждение устарело.
    """
    total, items = await ReviewService(session).queue(
        limit=limit, offset=offset, state=state, supplier=supplier
    )
    return {"total": total, "state": state, "items": items}


@router.get("/summary")
async def summary(session: AsyncSession = Depends(get_session)) -> dict[str, int]:
    return await ReviewService(session).summary()


@router.get("/documents/{document_id}/decisions")
async def decisions(
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """История решений: она append-only, поэтому видно каждое, а не последнее."""
    document = await session.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="документ не найден")
    service = ReviewService(session)
    history = await service.history(document.id)
    check = await service.latest_check(document.id)
    state = effective_state(document, check, history[0] if history else None)
    return {
        "document_id": str(document.id),
        "effective": {
            "verdict": state.verdict,
            "decided": state.decided,
            "action": state.action,
            "reviewer": state.reviewer,
            "decided_at": state.decided_at,
            "stale": state.stale,
            "stale_reason": state.stale_reason,
            "attachable": state.attachable,
            "needs_review": state.needs_review,
        },
        "history": [_decision_dict(item) for item in history],
    }


@router.post("/documents/{document_id}/decision")
async def decide(
    document_id: uuid.UUID,
    payload: dict = Body(...),
    on: date | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """Записать решение: `confirm`, `reject` или `correct` с номером.

    Решения не редактируются: повторный вызов кладёт новое рядом, а актуальным
    становится последнее. История нужна, когда КП с приложенным документом
    оспаривают — вопрос «кто подтвердил» должен иметь ответ.
    """
    document = await session.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="документ не найден")

    service = ReviewService(session)
    try:
        decision, state = await service.decide(
            document,
            action=str(payload.get("action", "")),
            reviewer=str(payload.get("reviewer", "")),
            comment=str(payload.get("comment", "")),
            number=str(payload.get("number", "")),
            repository=RegistryRepository(session),
            on=on,
        )
    except UnknownDecision as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    await session.commit()
    return {
        "decision": _decision_dict(decision),
        "effective": {
            "verdict": state.verdict,
            "action": state.action,
            "attachable": state.attachable,
            "needs_review": state.needs_review,
            "stale": state.stale,
        },
    }
