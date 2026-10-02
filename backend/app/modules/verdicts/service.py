"""Решение менеджера по вердикту позиции и заявки.

Вердикт считает система, но подтверждает человек, и эти две вещи не смешиваются:
`computed_verdict` остаётся нетронутым, решение пишется в `manager_verdict`.
При расхождении комментарий обязателен (verdicts §Подтверждение) — иначе в
истории проверки останется «менеджер решил иначе» без единого объяснения.

У заявки та же пара, но в колонках `check_batches`: расчёт — `final_verdict`,
решение — `manager_verdict`. Расчёт заявки — худший из действующих вердиктов
позиций, и пересчитывается он на каждом изменении вердикта позиции: подтверждать
заявку по устаревшему расчёту нельзя.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.modules.checks.models import CheckBatch, CheckItem
from app.modules.checks.service import ChecksService
from app.modules.identity.models import User
from app.modules.verdicts.batch import refresh_batch_verdict
from app.modules.verdicts.models import ItemVerdict

#: Статусы позиции, в которых менеджер вправе подтвердить вердикт.
DECIDABLE_STATUSES = ("READY_FOR_MANAGER_REVIEW", "REVIEW_REQUIRED")

#: Статусы позиции, при которых её вердикт считается закрытым менеджером.
ITEM_DECIDED_STATUSES = ("VERDICT_APPROVED", "REPORTING", "COMPLETED")

#: Что менеджер может выбрать. `PENDING` — состояние «не решил», а не решение.
ALLOWED_VERDICTS = ("COMPLIANT", "PARTIAL", "NON_COMPLIANT")


class VerdictsService:
    def __init__(self, session: AsyncSession, checks: ChecksService) -> None:
        self._session = session
        self._checks = checks

    async def get(self, item_id: uuid.UUID) -> ItemVerdict:
        await self._checks.get_item(item_id)
        verdict = await self._session.scalar(
            select(ItemVerdict).where(ItemVerdict.check_item_id == item_id)
        )
        if verdict is None:
            raise ApiError(
                "VERDICT_NOT_READY",
                "Вердикт появится после сравнения характеристик",
                status_code=422,
                details={"checkItemId": str(item_id)},
            )
        return verdict

    async def decide(
        self,
        item_id: uuid.UUID,
        *,
        verdict_value: str,
        comment: str | None,
        expected_version: int,
        user: User,
    ) -> tuple[ItemVerdict, CheckItem]:
        """Фиксирует решение менеджера и переводит позицию дальше по FSM."""
        self._checks.assert_can_write(user)
        item = await self._checks.get_item(item_id)
        verdict = await self.get(item_id)

        if item.status not in DECIDABLE_STATUSES:
            raise ApiError(
                "VERDICT_NOT_READY",
                f"Позиция ещё не готова к решению менеджера, статус: {item.status}",
                status_code=422,
            )
        if verdict_value not in ALLOWED_VERDICTS:
            raise ApiError(
                "INVALID_VERDICT",
                f"Недопустимое значение вердикта: {verdict_value}",
                status_code=422,
            )
        if expected_version != verdict.version:
            raise ApiError(
                "EDIT_CONFLICT",
                "Вердикт изменился: перечитайте позицию и повторите решение",
                status_code=409,
                details={"expectedVersion": verdict.version},
            )
        if verdict_value != verdict.computed_verdict and not (comment or "").strip():
            raise ApiError(
                "COMMENT_REQUIRED",
                "Расхождение с расчётным вердиктом требует комментария",
                status_code=422,
            )

        verdict.manager_verdict = verdict_value
        verdict.comment = comment
        verdict.approved_by = user.id
        verdict.approved_at = datetime.now(UTC)
        verdict.version += 1
        await self._checks.mark_verdict_approved(item)
        await refresh_batch_verdict(self._session, item.batch_id)
        return verdict, item

    # --- вердикт заявки -------------------------------------------------

    async def get_batch(self, batch_id: uuid.UUID) -> CheckBatch:
        """Вердикт заявки; расчёт пересчитывается на чтение.

        Позиции меняются не только через этот модуль (правка параметров,
        пересчёт сравнений), поэтому дешевле подтвердить расчёт при чтении,
        чем ловить все места, где он мог устареть.
        """
        batch = await self._checks.get_batch(batch_id)
        return await refresh_batch_verdict(self._session, batch.id) or batch

    async def decide_batch(
        self,
        batch_id: uuid.UUID,
        *,
        verdict_value: str,
        comment: str | None,
        expected_version: int,
        user: User,
    ) -> CheckBatch:
        """Фиксирует решение менеджера по заявке целиком."""
        self._checks.assert_can_write(user)
        batch = await self.get_batch(batch_id)

        undecided = [
            item.name or str(item.position_no)
            for item in batch.items
            if item.status not in ITEM_DECIDED_STATUSES
        ]
        if not batch.items or undecided:
            raise ApiError(
                "VERDICT_NOT_READY",
                "Вердикт заявки подтверждается после решения по каждой позиции",
                status_code=422,
                details={"pendingItems": undecided},
            )
        if verdict_value not in ALLOWED_VERDICTS:
            raise ApiError(
                "INVALID_VERDICT",
                f"Недопустимое значение вердикта: {verdict_value}",
                status_code=422,
            )
        if expected_version != batch.verdict_version:
            raise ApiError(
                "EDIT_CONFLICT",
                "Вердикт заявки изменился: перечитайте заявку и повторите решение",
                status_code=409,
                details={"expectedVersion": batch.verdict_version},
            )
        if verdict_value != batch.final_verdict and not (comment or "").strip():
            raise ApiError(
                "COMMENT_REQUIRED",
                "Расхождение с расчётным вердиктом требует комментария",
                status_code=422,
            )

        batch.manager_verdict = verdict_value
        batch.verdict_comment = comment
        batch.verdict_approved_by = user.id
        batch.verdict_approved_at = datetime.now(UTC)
        batch.verdict_version += 1
        await self._session.flush()
        return batch
