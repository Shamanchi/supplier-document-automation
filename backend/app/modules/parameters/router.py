"""Parameter-level HITL API."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.modules.checks.schemas import ItemParameterResponse
from app.modules.checks.service import ChecksService
from app.modules.identity.dependencies import CurrentUser, SessionDep, SettingsDep
from app.modules.parameters.schemas import (
    ManualParameterCreate,
    ParameterRevisionRequest,
    ParameterStateRequest,
)
from app.modules.parameters.service import ParametersService

item_router = APIRouter(prefix="/check-items", tags=["parameters"])
router = APIRouter(prefix="/parameters", tags=["parameters"])


def get_parameters_service(
    session: SessionDep, settings: SettingsDep
) -> ParametersService:
    return ParametersService(session, ChecksService(session, settings))


ServiceDep = Annotated[ParametersService, Depends(get_parameters_service)]


@item_router.post(
    "/{item_id}/parameters",
    response_model=ItemParameterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_manual_parameter(
    item_id: UUID,
    payload: ManualParameterCreate,
    service: ServiceDep,
    user: CurrentUser,
) -> ItemParameterResponse:
    raw = await service.add_manual(item_id, payload, user)
    return await service.response(raw)


@router.get("/{parameter_id}", response_model=ItemParameterResponse)
async def get_parameter(
    parameter_id: UUID, service: ServiceDep, user: CurrentUser
) -> ItemParameterResponse:
    return await service.response(await service.get(parameter_id))


@router.post(
    "/{parameter_id}/revisions",
    response_model=ItemParameterResponse,
    status_code=status.HTTP_201_CREATED,
)
async def revise_parameter(
    parameter_id: UUID,
    payload: ParameterRevisionRequest,
    service: ServiceDep,
    user: CurrentUser,
) -> ItemParameterResponse:
    raw = await service.revise(
        parameter_id,
        payload,
        set(payload.model_fields_set),
        user,
    )
    return await service.response(raw)


@router.post("/{parameter_id}/exclude", response_model=ItemParameterResponse)
async def exclude_parameter(
    parameter_id: UUID,
    payload: ParameterStateRequest,
    service: ServiceDep,
    user: CurrentUser,
) -> ItemParameterResponse:
    raw = await service.exclude(parameter_id, payload, user)
    return await service.response(raw)


@router.post("/{parameter_id}/restore", response_model=ItemParameterResponse)
async def restore_parameter(
    parameter_id: UUID,
    payload: ParameterStateRequest,
    service: ServiceDep,
    user: CurrentUser,
) -> ItemParameterResponse:
    raw = await service.restore(parameter_id, payload, user)
    return await service.response(raw)
