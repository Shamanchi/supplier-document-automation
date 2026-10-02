"""Corpus-run endpoints: start a directory run, watch progress, read report."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import CorpusRun, CorpusRunFile
from app.schemas import (
    CorpusRunAccepted,
    CorpusRunCreate,
    CorpusRunDetail,
    CorpusRunFileOut,
    CorpusRunOut,
)

router = APIRouter(tags=["corpus-runs"])


@router.post("/corpus-runs", response_model=CorpusRunAccepted, status_code=202)
async def start_corpus_run(
    payload: CorpusRunCreate, session: AsyncSession = Depends(get_session)
) -> CorpusRunAccepted:
    run = CorpusRun(source_path=payload.path, status="running")
    session.add(run)
    await session.commit()

    from app.tasks import run_corpus_task

    async_result = run_corpus_task.delay(str(run.id), payload.path, payload.force)
    return CorpusRunAccepted(corpus_run_id=run.id, job_id=async_result.id)


@router.get("/corpus-runs", response_model=list[CorpusRunOut])
async def list_corpus_runs(session: AsyncSession = Depends(get_session)) -> list[CorpusRunOut]:
    runs = (
        (await session.execute(select(CorpusRun).order_by(CorpusRun.started_at.desc())))
        .scalars()
        .all()
    )
    return [CorpusRunOut.model_validate(r) for r in runs]


@router.get("/corpus-runs/{run_id}", response_model=CorpusRunDetail)
async def get_corpus_run(
    run_id: uuid.UUID,
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> CorpusRunDetail:
    run = await session.get(CorpusRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Прогон корпуса не найден")

    stmt = select(CorpusRunFile).where(CorpusRunFile.corpus_run_id == run_id)
    if status:
        stmt = stmt.where(CorpusRunFile.status == status)
    total = (
        await session.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    files = (
        (
            await session.execute(
                stmt.order_by(CorpusRunFile.filename).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return CorpusRunDetail(
        run=CorpusRunOut.model_validate(run),
        files_total=total,
        files=[CorpusRunFileOut.model_validate(f) for f in files],
    )
