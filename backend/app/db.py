"""Async SQLAlchemy engine/session management.

The FastAPI app uses a module-level engine. Celery tasks run their own
event loop per task (asyncio.run), so they create a fresh engine bound
to that loop and dispose it afterwards — see make_task_engine().

`Base` (шаг миграции 02) — общий declarative base для всех модулей:
прайс-контур фазы 2 и модули фазы 3 регистрируют таблицы в одном
метаданных, иначе SQLAlchemy не увидит FK между ними. `app.models`
реэкспортирует его для обратной совместимости.
"""
from collections.abc import AsyncIterator

from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings


class Base(DeclarativeBase):
    """Общий declarative base: app.models и app.modules.* строятся на нём."""


engine: AsyncEngine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


def make_task_engine() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Fresh engine + sessionmaker for a Celery task's private event loop."""
    task_engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    return task_engine, async_sessionmaker(task_engine, expire_on_commit=False)
