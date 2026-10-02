"""Project workspaces for isolating price data and category directories."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthUser, require_admin
from app.db import get_session
from app.models import GlobalCategory, PriceFile, PriceVersion, Project, SupplierPriceItem
from app.schemas import ProjectCreate, ProjectOut, ProjectUpdate


router = APIRouter(prefix="/projects", tags=["projects"])


async def _project_out(session: AsyncSession, project: Project) -> ProjectOut:
    price_files_count = int(
        await session.scalar(select(func.count(PriceFile.id)).where(PriceFile.project_id == project.id))
        or 0
    )
    items_count = int(
        await session.scalar(
            select(func.count(SupplierPriceItem.id))
            .join(PriceVersion, PriceVersion.id == SupplierPriceItem.price_version_id)
            .join(PriceFile, PriceFile.id == PriceVersion.price_file_id)
            .where(PriceFile.project_id == project.id)
        )
        or 0
    )
    global_categories_count = int(
        await session.scalar(
            select(func.count(GlobalCategory.id)).where(GlobalCategory.project_id == project.id)
        )
        or 0
    )
    return ProjectOut(
        **ProjectOut.model_validate(project).model_dump(
            exclude={"price_files_count", "items_count", "global_categories_count"}
        ),
        price_files_count=price_files_count,
        items_count=items_count,
        global_categories_count=global_categories_count,
    )


@router.get("", response_model=list[ProjectOut])
async def list_projects(session: AsyncSession = Depends(get_session)) -> list[ProjectOut]:
    projects = (
        await session.execute(select(Project).order_by(Project.is_default.desc(), Project.name))
    ).scalars().all()
    return [await _project_out(session, project) for project in projects]


@router.post("", response_model=ProjectOut, status_code=201)
async def create_project(
    payload: ProjectCreate,
    _admin: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ProjectOut:
    project = Project(
        slug=payload.slug,
        name=payload.name.strip(),
        description=payload.description.strip() if payload.description else None,
    )
    session.add(project)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Проект с таким кодом уже существует") from exc
    return await _project_out(session, project)


@router.put("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    _admin: AuthUser = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ProjectOut:
    project = await session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    if payload.name is not None:
        project.name = payload.name.strip()
    if payload.description is not None:
        project.description = payload.description.strip() or None
    if payload.is_archived is not None:
        if project.is_default and payload.is_archived:
            raise HTTPException(status_code=409, detail="Проект по умолчанию нельзя архивировать")
        project.is_archived = payload.is_archived
    await session.commit()
    return await _project_out(session, project)
