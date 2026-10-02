"""Resolve the active operator project from a bounded request header."""

from __future__ import annotations

import uuid

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import ORIGINAL_PROJECT_ID, Project


async def get_current_project(
    project_header: str | None = Header(default=None, alias="X-Project-ID"),
    session: AsyncSession = Depends(get_session),
) -> Project:
    try:
        project_id = uuid.UUID(project_header) if project_header else ORIGINAL_PROJECT_ID
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Некорректный идентификатор проекта") from exc
    project = await session.get(Project, project_id)
    if project is None and project_id == ORIGINAL_PROJECT_ID:
        project = Project(
            id=ORIGINAL_PROJECT_ID,
            slug="original-prices",
            name="Оригинальные прайсы",
            description="Проект по умолчанию",
            is_default=True,
        )
        session.add(project)
        await session.flush()
    if project is None:
        raise HTTPException(status_code=404, detail="Проект не найден")
    return project
