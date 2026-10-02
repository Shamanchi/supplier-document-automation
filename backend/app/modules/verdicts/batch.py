"""Расчётный вердикт заявки.

Вынесен отдельным листовым модулем: пересчитывать вердикт заявки нужно из
`rules` (после сопоставления), из `parameters` (после ручной правки) и из самого
`verdicts` (после решения по позиции). Тянуть ради этого `ChecksService` во все
три места незачем — здесь только модели и чистая агрегация.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.checks.models import CheckBatch
from app.modules.verdicts.aggregate import aggregate_batch
from app.modules.verdicts.models import ItemVerdict


def effective_verdict(verdict: ItemVerdict | None) -> str | None:
    """Действующий вердикт позиции: решение менеджера, иначе расчёт."""
    if verdict is None:
        return None
    if verdict.manager_verdict != "PENDING":
        return verdict.manager_verdict
    return verdict.computed_verdict


async def refresh_batch_verdict(
    session: AsyncSession, batch_id: uuid.UUID
) -> CheckBatch | None:
    """Пересчитывает вердикт заявки после изменения вердикта позиции.

    Если расчёт изменился, подтверждение заявки снимается: оно относилось к
    другим данным — то же правило, что и у позиции при пересчёте сравнений.
    """
    batch = await session.get(CheckBatch, batch_id)
    if batch is None:
        return None

    item_ids = [item.id for item in batch.items]
    rows = (
        await session.scalars(
            select(ItemVerdict).where(ItemVerdict.check_item_id.in_(item_ids))
        )
    ).all()
    by_item = {row.check_item_id: row for row in rows}
    aggregated = aggregate_batch(
        [effective_verdict(by_item.get(item_id)) for item_id in item_ids]
    )

    changed = (
        batch.final_verdict != aggregated.verdict
        or batch.verdict_basis != aggregated.basis
    )
    if not changed:
        return batch

    if batch.final_verdict != aggregated.verdict:
        batch.manager_verdict = "PENDING"
        batch.verdict_comment = None
        batch.verdict_approved_by = None
        batch.verdict_approved_at = None
    batch.final_verdict = aggregated.verdict
    batch.verdict_basis = aggregated.basis
    batch.verdict_version += 1
    await session.flush()
    return batch
