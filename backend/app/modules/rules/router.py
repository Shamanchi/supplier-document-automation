"""Результаты сравнения и их ручной разбор (docs/api/comparisons).

Чтение — таблица позиции, карточка строки и кандидаты пары. Правка —
`override` статуса с обязательным комментарием, его отмена, ручной выбор пары
и пересчёт позиции; вся их логика в `rules.review`.

Клиент отображает `effectiveStatus` и обязан читать именно его: после override
он расходится с `computedStatus`, и расчёт остаётся видимым рядом с решением
менеджера. Версия строки едет в `ETag`, а условие правки — в `expectedVersion`
(для `DELETE`, у которого тела нет, — в `If-Match`).

Второй approver для override контрактом не описан и не сделан; текущее правило
— те же роли, что и на запись в проверки, см. docs/todo/pipeline.md.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status as http_status
from pydantic import Field
from sqlalchemy import select

from app.core.concurrency import format_etag, require_if_match
from app.core.errors import ApiError
from app.core.schemas import BaseSchema
from app.modules.catalogs.models import CanonicalParameter
from app.modules.checks.service import ChecksService
from app.modules.identity.dependencies import CurrentUser, SessionDep, SettingsDep
from app.modules.order_matching.models import MatchCandidate
from app.modules.rules.models import Comparison
from app.modules.rules.review import ComparisonReviewService

item_router = APIRouter(prefix="/check-items", tags=["comparisons"])
router = APIRouter(prefix="/comparisons", tags=["comparisons"])

IfMatch = Annotated[int, Depends(require_if_match)]

#: Статусы, по которым разрешено фильтровать таблицу (comparisons §Список).
COMPARISON_STATUSES = (
    "COMPLIANT",
    "NON_COMPLIANT",
    "REVIEW_REQUIRED",
    "NOT_FOUND_IN_FACT",
    "NOT_REQUIRED",
)


class CanonicalParameterRef(BaseSchema):
    id: UUID
    code: str
    name: str


class ComparisonResponse(BaseSchema):
    id: UUID
    canonical_parameter: CanonicalParameterRef | None = None
    requirement_parameter_id: UUID | None = None
    fact_parameter_id: UUID | None = None
    row_kind: str
    operator: str | None = None
    computed_status: str
    effective_status: str
    explanation: dict | None = None
    lineage_chain: list | None = None
    overridden: bool
    stale: bool
    version: int


class CandidateResponse(BaseSchema):
    fact_parameter_id: UUID | None = None
    method: str
    score: str | None = None
    rank: int | None = None
    eligible: bool
    rejection_reasons: list[str]


class CandidatesResponse(BaseSchema):
    comparison_id: UUID
    candidates: list[CandidateResponse]


class OverrideRequest(BaseSchema):
    expected_version: int = Field(ge=1)
    status: str
    reason_code: str | None = None
    comment: str | None = None


class SelectCandidateRequest(BaseSchema):
    expected_version: int = Field(ge=1)
    fact_parameter_id: UUID
    comment: str | None = None
    #: Пересчитать правило сразу. `false` оставляет пару без статуса — строка
    #: дождётся общего пересчёта позиции.
    recalculate: bool = True


def get_checks_service(session: SessionDep, settings: SettingsDep) -> ChecksService:
    return ChecksService(session, settings)


def get_review_service(
    session: SessionDep, settings: SettingsDep
) -> ComparisonReviewService:
    return ComparisonReviewService(session, ChecksService(session, settings))


ServiceDep = Annotated[ChecksService, Depends(get_checks_service)]
ReviewDep = Annotated[ComparisonReviewService, Depends(get_review_service)]


@item_router.get("/{item_id}/comparisons", response_model=list[ComparisonResponse])
async def item_comparisons(
    item_id: UUID,
    session: SessionDep,
    service: ServiceDep,
    user: CurrentUser,
    status: Annotated[str | None, Query()] = None,
    overridden: Annotated[bool | None, Query()] = None,
    include_stale: Annotated[bool, Query(alias="includeStale")] = False,
) -> list[ComparisonResponse]:
    """Таблица сопоставления позиции.

    По умолчанию отдаются только актуальные строки: после пересчёта прежние
    помечаются `stale`, и показывать их вперемешку с новыми нельзя.
    """
    await service.get_item(item_id)
    if status is not None and status not in COMPARISON_STATUSES:
        raise ApiError(
            "INVALID_COMPARISON_STATUS",
            f"Неизвестный статус сравнения: {status}",
            status_code=422,
        )

    query = select(Comparison).where(Comparison.check_item_id == item_id)
    if not include_stale:
        query = query.where(Comparison.stale.is_(False))
    if status is not None:
        query = query.where(Comparison.effective_status == status)
    if overridden is not None:
        query = query.where(Comparison.overridden.is_(overridden))

    rows = list(
        (
            await session.scalars(
                query.order_by(Comparison.row_kind, Comparison.created_at)
            )
        ).all()
    )
    parameters = await _parameters(session, rows)
    return [_response(row, parameters) for row in rows]


@router.get("/{comparison_id}", response_model=ComparisonResponse)
async def get_comparison(
    comparison_id: UUID,
    session: SessionDep,
    service: ServiceDep,
    user: CurrentUser,
) -> ComparisonResponse:
    comparison = await _comparison(session, service, comparison_id)
    parameters = await _parameters(session, [comparison])
    return _response(comparison, parameters)


@router.get("/{comparison_id}/candidates", response_model=CandidatesResponse)
async def comparison_candidates(
    comparison_id: UUID,
    session: SessionDep,
    service: ServiceDep,
    user: CurrentUser,
) -> CandidatesResponse:
    """Кандидаты пары, включая отвергнутых: без них ручной выбор — гадание."""
    comparison = await _comparison(session, service, comparison_id)
    candidates = (
        await session.scalars(
            select(MatchCandidate)
            .where(
                MatchCandidate.check_item_id == comparison.check_item_id,
                MatchCandidate.requirement_parameter_id
                == comparison.requirement_parameter_id,
            )
            .order_by(MatchCandidate.rank)
        )
    ).all()
    return CandidatesResponse(
        comparison_id=comparison.id,
        candidates=[
            CandidateResponse(
                fact_parameter_id=candidate.fact_parameter_id,
                method=candidate.method,
                score=None if candidate.score is None else str(candidate.score),
                rank=candidate.rank,
                eligible=candidate.eligible,
                rejection_reasons=list(candidate.rejection_reasons or []),
            )
            for candidate in candidates
        ],
    )


@router.post("/{comparison_id}/override", response_model=ComparisonResponse)
async def override_comparison(
    comparison_id: UUID,
    payload: OverrideRequest,
    session: SessionDep,
    review: ReviewDep,
    response: Response,
    user: CurrentUser,
) -> ComparisonResponse:
    """Ручная смена статуса строки.

    `computedStatus` и объяснение правила остаются нетронутыми: менеджер
    решает иначе, но расчёт из истории проверки не исчезает.
    """
    comparison = await review.override(
        comparison_id,
        expected_version=payload.expected_version,
        status=payload.status,
        reason_code=payload.reason_code,
        comment=payload.comment,
        user=user,
    )
    await session.commit()
    response.headers["ETag"] = format_etag(comparison.version)
    return _response(comparison, await _parameters(session, [comparison]))


@router.delete("/{comparison_id}/override", response_model=ComparisonResponse)
async def cancel_override(
    comparison_id: UUID,
    session: SessionDep,
    review: ReviewDep,
    response: Response,
    user: CurrentUser,
    if_match: IfMatch,
) -> ComparisonResponse:
    """Отмена ручного статуса: строка возвращается к расчётному."""
    comparison = await review.cancel_override(
        comparison_id, expected_version=if_match, user=user
    )
    await session.commit()
    response.headers["ETag"] = format_etag(comparison.version)
    return _response(comparison, await _parameters(session, [comparison]))


@router.post("/{comparison_id}/select-candidate", response_model=ComparisonResponse)
async def select_candidate(
    comparison_id: UUID,
    payload: SelectCandidateRequest,
    session: SessionDep,
    review: ReviewDep,
    response: Response,
    user: CurrentUser,
) -> ComparisonResponse:
    """Ручной выбор факта для требования, когда автоматика не выбрала пару."""
    comparison = await review.select_candidate(
        comparison_id,
        expected_version=payload.expected_version,
        fact_parameter_id=payload.fact_parameter_id,
        comment=payload.comment,
        recalculate=payload.recalculate,
        user=user,
    )
    await session.commit()
    response.headers["ETag"] = format_etag(comparison.version)
    return _response(comparison, await _parameters(session, [comparison]))


@item_router.post(
    "/{item_id}/comparisons/recalculate",
    response_model=list[ComparisonResponse],
    status_code=http_status.HTTP_200_OK,
)
async def recalculate_comparisons(
    item_id: UUID,
    session: SessionDep,
    review: ReviewDep,
    user: CurrentUser,
) -> list[ComparisonResponse]:
    """Пересчёт позиции после правок ТХ или каталога.

    Ручные статусы переживают пересчёт: обновляется расчётная часть, а
    `effectiveStatus` остаётся за активным override.
    """
    rows = await review.recalculate(item_id, user)
    await session.commit()
    parameters = await _parameters(session, rows)
    return [_response(row, parameters) for row in rows]


async def _comparison(
    session, service: ChecksService, comparison_id: UUID
) -> Comparison:
    comparison = await session.get(Comparison, comparison_id)
    if comparison is None:
        raise ApiError(
            "COMPARISON_NOT_FOUND",
            "Строка сопоставления не найдена",
            status_code=404,
            details={"comparisonId": str(comparison_id)},
        )
    # Доступ проверяется по позиции: своей модели доступа у сравнения нет.
    await service.get_item(comparison.check_item_id)
    return comparison


async def _parameters(
    session, rows: list[Comparison]
) -> dict[UUID, CanonicalParameter]:
    ids = {row.canonical_parameter_id for row in rows if row.canonical_parameter_id}
    if not ids:
        return {}
    found = (
        await session.scalars(
            select(CanonicalParameter).where(CanonicalParameter.id.in_(ids))
        )
    ).all()
    return {parameter.id: parameter for parameter in found}


def _response(
    comparison: Comparison, parameters: dict[UUID, CanonicalParameter]
) -> ComparisonResponse:
    parameter = parameters.get(comparison.canonical_parameter_id)
    return ComparisonResponse(
        id=comparison.id,
        canonical_parameter=(
            CanonicalParameterRef(
                id=parameter.id, code=parameter.code, name=parameter.display_name
            )
            if parameter
            else None
        ),
        requirement_parameter_id=comparison.requirement_parameter_id,
        fact_parameter_id=comparison.fact_parameter_id,
        row_kind=comparison.row_kind,
        operator=comparison.operator,
        computed_status=comparison.computed_status,
        effective_status=comparison.effective_status,
        explanation=comparison.explanation,
        lineage_chain=comparison.lineage_chain,
        overridden=comparison.overridden,
        stale=comparison.stale,
        version=comparison.version,
    )
