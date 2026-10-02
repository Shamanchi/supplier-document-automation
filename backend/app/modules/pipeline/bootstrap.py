"""Регистрация версии pipeline при первом старте.

`pipeline_runs.pipeline_definition_id` обязателен: без зарегистрированной версии
FSM запустить позицию нельзя вообще. Сам FSM зафиксирован каноном (§18
PHASE3_CANONICAL_ARCHITECTURE.md, `VERIFYING_SOURCES` — отдельный hard gate
между извлечением и нормализацией).

Версии каталога здесь НЕ создаются: справочники — доменные данные, придумывать
их нельзя. Пока опубликованной версии нет, запуск отвечает
`422 CATALOG_VERSION_NOT_PUBLISHED` — это честнее пустого каталога-заглушки.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.checks.models import PipelineDefinition

logger = logging.getLogger(__name__)

#: Версия FSM. Таблица защищена триггером `prevent_modify`: изменить
#: зарегистрированную версию нельзя, только зарегистрировать новую.
PIPELINE_VERSION = "pipeline-1.0.0"

#: Этапы и переходы. Порядок линейный; ветвления появятся вместе с
#: маршрутизацией по `documents.profile`. Исполнитель доводит прогон до
#: `NORMALIZING`; `MATCHING` и `REPORTING` ждут контура сравнения.
PIPELINE_FSM: dict = {
    "stages": [
        "INGESTING",
        "EXTRACTING",
        "VERIFYING_SOURCES",
        "NORMALIZING",
        "MATCHING",
        "REPORTING",
    ],
    "transitions": [
        {"from": "INGESTING", "to": "EXTRACTING"},
        {"from": "EXTRACTING", "to": "VERIFYING_SOURCES"},
        {"from": "VERIFYING_SOURCES", "to": "NORMALIZING"},
        {"from": "NORMALIZING", "to": "MATCHING"},
        {"from": "MATCHING", "to": "REPORTING"},
    ],
}


async def ensure_pipeline_definition(session: AsyncSession) -> bool:
    """Регистрирует версию FSM, если её ещё нет. True — если создана."""
    existing = await session.scalar(
        select(PipelineDefinition.id).where(
            PipelineDefinition.version == PIPELINE_VERSION
        )
    )
    if existing:
        return False

    session.add(PipelineDefinition(version=PIPELINE_VERSION, fsm=PIPELINE_FSM))
    await session.flush()
    logger.info("Зарегистрирована версия pipeline: %s", PIPELINE_VERSION)
    return True
