"""Вердикт позиции и заявки (docs/api/verdicts).

Реализованы вердикт позиции, вердикт заявки и решения по ним. История решений
(`/verdicts/{id}/history`) пока не сделана — см. docs/todo/pipeline.md.

У заявки нет своей строки вердикта: расчёт и решение живут в колонках
`check_batches`, поэтому `id` в ответе — идентификатор заявки. Версия для
`expectedVersion` тоже своя (`verdict_version`), а не `version` заявки: пересчёт
вердикта не должен ронять правку реквизитов.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import Field

from app.core.schemas import BaseSchema
from app.modules.checks.models import CheckBatch
from app.modules.checks.service import ChecksService
from app.modules.identity.dependencies import CurrentUser, SessionDep, SettingsDep
from app.modules.verdicts.models import ItemVerdict
from app.modules.verdicts.service import VerdictsService

router = APIRouter(prefix="/check-items", tags=["verdicts"])
batch_router = APIRouter(prefix="/check-batches", tags=["verdicts"])


class VerdictResponse(BaseSchema):
    id: UUID
    scope: str = "CHECK_ITEM"
    scope_id: UUID
    computed_verdict: str
    manager_verdict: str
    basis: dict | None = None
    comment: str | None = None
    approved_by: UUID | None = None
    approved_at: str | None = None
    version: int


class VerdictDecision(BaseSchema):
    expected_version: int = Field(ge=1)
    verdict: str
    comment: str | None = None


def get_verdicts_service(session: SessionDep, settings: SettingsDep) -> VerdictsService:
    return VerdictsService(session, ChecksService(session, settings))


ServiceDep = Annotated[VerdictsService, Depends(get_verdicts_service)]


@router.get("/{item_id}/verdict", response_model=VerdictResponse)
async def get_verdict(
    item_id: UUID, service: ServiceDep, user: CurrentUser
) -> VerdictResponse:
    return _response(await service.get(item_id))


@router.post("/{item_id}/verdict", response_model=VerdictResponse)
async def decide_verdict(
    item_id: UUID,
    payload: VerdictDecision,
    service: ServiceDep,
    session: SessionDep,
    user: CurrentUser,
) -> VerdictResponse:
    """Подтверждение или изменение вердикта позиции менеджером."""
    verdict, _ = await service.decide(
        item_id,
        verdict_value=payload.verdict,
        comment=payload.comment,
        expected_version=payload.expected_version,
        user=user,
    )
    await session.commit()
    return _response(verdict)


@batch_router.get("/{batch_id}/verdict", response_model=VerdictResponse)
async def get_batch_verdict(
    batch_id: UUID, service: ServiceDep, user: CurrentUser
) -> VerdictResponse:
    """Расчётный и подтверждённый вердикт заявки."""
    return _batch_response(await service.get_batch(batch_id))


@batch_router.post("/{batch_id}/verdict", response_model=VerdictResponse)
async def decide_batch_verdict(
    batch_id: UUID,
    payload: VerdictDecision,
    service: ServiceDep,
    session: SessionDep,
    user: CurrentUser,
) -> VerdictResponse:
    """Подтверждение вердикта заявки: доступно после решения по всем позициям."""
    batch = await service.decide_batch(
        batch_id,
        verdict_value=payload.verdict,
        comment=payload.comment,
        expected_version=payload.expected_version,
        user=user,
    )
    await session.commit()
    return _batch_response(batch)


def _batch_response(batch: CheckBatch) -> VerdictResponse:
    return VerdictResponse(
        id=batch.id,
        scope="CHECK_BATCH",
        scope_id=batch.id,
        computed_verdict=batch.final_verdict,
        manager_verdict=batch.manager_verdict,
        basis=batch.verdict_basis,
        comment=batch.verdict_comment,
        approved_by=batch.verdict_approved_by,
        approved_at=(
            batch.verdict_approved_at.isoformat().replace("+00:00", "Z")
            if batch.verdict_approved_at
            else None
        ),
        version=batch.verdict_version,
    )


def _response(verdict: ItemVerdict) -> VerdictResponse:
    return VerdictResponse(
        id=verdict.id,
        scope_id=verdict.check_item_id,
        computed_verdict=verdict.computed_verdict,
        manager_verdict=verdict.manager_verdict,
        basis=verdict.basis,
        comment=verdict.comment,
        approved_by=verdict.approved_by,
        approved_at=(
            verdict.approved_at.isoformat().replace("+00:00", "Z")
            if verdict.approved_at
            else None
        ),
        version=verdict.version,
    )
