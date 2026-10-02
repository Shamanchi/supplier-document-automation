"""Ручной разбор сопоставления (docs/api/comparisons).

Три операции менеджера над строкой таблицы и одна над позицией:

* `override` — сменить статус с обязательным комментарием;
* отмена override — вернуть строку к расчётному статусу, не стирая историю;
* `select-candidate` — назначить пару вручную и пересчитать правило;
* `recalculate` — пересчитать позицию после правок ТХ или каталога.

Разделение `computed_status` и `effective_status` существует ровно ради этого
модуля: расчёт остаётся видимым всегда, решение менеджера живёт рядом, а не
вместо него. Поэтому override не трогает ни `computed_status`, ни
`explanation` — он лишь добавляет активную запись в `comparison_overrides`,
из которой и берётся `effective_status`.

Пересчёт идёт по снимку правил того прогона, который построил строку: правила
могли смениться, но результат, показанный менеджеру, обязан воспроизводиться
(§15.8).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.modules.checks.models import CheckItem, PipelineRun
from app.modules.checks.service import WRITE_ROLES, ChecksService
from app.modules.extraction.models import RawParameter
from app.modules.identity.models import AuditEvent, User
from app.modules.order_matching.models import MatchCandidate
from app.modules.order_matching.service import Candidate, Side
from app.modules.normalization.models import NormalizedParameter
from app.modules.rules.engine import Policy, RuleOutcome, default_rules
from app.modules.rules.models import Comparison, ComparisonOverride, RuleSnapshot
from app.modules.rules.service import (
    build_lineage,
    evaluate_pair,
    parameter_policies,
    save_computed_verdict,
    verdict_entries,
)
from app.modules.verdicts.aggregate import aggregate
from app.modules.verdicts.service import DECIDABLE_STATUSES

#: Статусы, которые менеджер вправе выставить вручную.
OVERRIDABLE_STATUSES = (
    "COMPLIANT",
    "NON_COMPLIANT",
    "REVIEW_REQUIRED",
    "NOT_FOUND_IN_FACT",
    "NOT_REQUIRED",
)

#: Ручная пара всегда достоверна: её выбрал человек, а не мера похожести.
MANUAL_SCORE = Decimal("1.0")


class ComparisonReviewService:
    def __init__(self, session: AsyncSession, checks: ChecksService) -> None:
        self._session = session
        self._checks = checks

    # --- override -------------------------------------------------------

    async def override(
        self,
        comparison_id: uuid.UUID,
        *,
        expected_version: int,
        status: str,
        reason_code: str | None,
        comment: str | None,
        user: User,
    ) -> Comparison:
        """Ручная смена статуса строки с обязательным комментарием."""
        comparison, _ = await self._editable(comparison_id, expected_version, user)
        if status not in OVERRIDABLE_STATUSES:
            raise ApiError(
                "INVALID_COMPARISON_STATUS",
                f"Неизвестный статус сравнения: {status}",
                status_code=422,
            )
        text = (comment or "").strip()
        if not text:
            raise ApiError(
                "COMMENT_REQUIRED",
                "Ручная смена статуса требует комментария",
                status_code=422,
            )

        await self._deactivate_overrides(comparison.id)
        self._session.add(
            ComparisonOverride(
                id=uuid.uuid4(),
                comparison_id=comparison.id,
                old_status=comparison.effective_status,
                new_status=status,
                reason_code=reason_code,
                comment=text,
                actor_id=user.id,
            )
        )
        comparison.effective_status = status
        comparison.overridden = True
        comparison.version += 1
        await self._audit(user, "comparison.overridden", comparison.id, text)
        await self._refresh_verdict(comparison.check_item_id)
        return comparison

    async def cancel_override(
        self, comparison_id: uuid.UUID, *, expected_version: int, user: User
    ) -> Comparison:
        """Отмена override: строка возвращается к расчётному статусу.

        Прежняя запись не удаляется, а деактивируется — вопрос «кто и почему
        менял статус» обязан иметь ответ и после отмены.
        """
        comparison, _ = await self._editable(comparison_id, expected_version, user)
        active = await self._active_override(comparison.id)
        if active is None:
            raise ApiError(
                "OVERRIDE_NOT_FOUND",
                "У строки нет активной ручной смены статуса",
                status_code=422,
                details={"comparisonId": str(comparison.id)},
            )

        active.active = False
        comparison.effective_status = comparison.computed_status
        comparison.overridden = False
        comparison.version += 1
        await self._audit(
            user, "comparison.override_cancelled", comparison.id, active.comment
        )
        await self._refresh_verdict(comparison.check_item_id)
        return comparison

    # --- ручная пара ----------------------------------------------------

    async def select_candidate(
        self,
        comparison_id: uuid.UUID,
        *,
        expected_version: int,
        fact_parameter_id: uuid.UUID,
        comment: str | None,
        recalculate: bool,
        user: User,
    ) -> Comparison:
        """Назначение пары вручную, когда автоматика её не выбрала."""
        comparison, item = await self._editable(comparison_id, expected_version, user)
        if comparison.requirement_parameter_id is None:
            raise ApiError(
                "CANDIDATE_NOT_ELIGIBLE",
                "У строки нет требования, к которому можно подобрать факт",
                status_code=422,
            )

        requirement = await self._side(comparison.requirement_parameter_id)
        fact = await self._side(fact_parameter_id)
        if requirement is None or fact is None:
            raise ApiError(
                "CANDIDATE_NOT_ELIGIBLE",
                "Параметр не найден среди разобранных характеристик позиции",
                status_code=422,
            )
        self._assert_candidate_usable(item, requirement, fact)

        await self._save_manual_candidate(item.id, requirement, fact)
        comparison.fact_parameter_id = fact.normalized.id
        comparison.row_kind = "PAIRED"
        comparison.version += 1
        if recalculate:
            outcome = await self._recompute(comparison, item, requirement, fact)
        else:
            # Пара назначена, но считать её менеджер просил отдельно: строка
            # обязана честно сказать, что показанный статус уже не про эти
            # данные.
            outcome = RuleOutcome(
                status="REVIEW_REQUIRED",
                operator=comparison.operator,
                explanation={
                    **(comparison.explanation or {}),
                    "issue": "RECALCULATION_PENDING",
                    "result": None,
                },
            )
            comparison.computed_status = outcome.status
            comparison.explanation = outcome.explanation
            if not comparison.overridden:
                comparison.effective_status = outcome.status

        await self._audit(
            user,
            "comparison.candidate_selected",
            comparison.id,
            (comment or "").strip() or None,
        )
        await self._refresh_verdict(item.id)
        return comparison

    # --- пересчёт -------------------------------------------------------

    async def recalculate(self, item_id: uuid.UUID, user: User) -> list[Comparison]:
        """Пересчёт актуальных строк позиции по тому же снимку правил.

        Ручные статусы переживают пересчёт: `computed_status` обновляется, а
        `effective_status` остаётся за активным override, пока менеджер его не
        отменит.
        """
        self._assert_can_override(user)
        item = await self._checks.get_item(item_id)
        self._assert_reviewable(item)

        rows = await self._rows(item_id)
        if not rows:
            raise ApiError(
                "NO_COMPARISONS",
                "Для позиции нет актуальных результатов сравнения",
                status_code=422,
                details={"checkItemId": str(item_id)},
            )

        for comparison in rows:
            if comparison.requirement_parameter_id is None:
                # Лишний факт сравнивать не с чем: строка справочная.
                continue
            requirement = await self._side(comparison.requirement_parameter_id)
            if requirement is None:
                continue
            fact = (
                await self._side(comparison.fact_parameter_id)
                if comparison.fact_parameter_id
                else None
            )
            await self._recompute(comparison, item, requirement, fact)
            comparison.version += 1

        await self._audit(user, "comparisons.recalculated", item.id, None)
        await self._refresh_verdict(item.id)
        return rows

    # --- внутреннее -----------------------------------------------------

    @staticmethod
    def _assert_can_override(user: User) -> None:
        if not WRITE_ROLES & set(user.role_names):
            raise ApiError(
                "OVERRIDE_NOT_ALLOWED",
                "Недостаточно прав для ручного разбора сопоставления",
                status_code=403,
            )

    @staticmethod
    def _assert_reviewable(item: CheckItem) -> None:
        """Разбор возможен, пока вердикт не подтверждён.

        После `VERDICT_APPROVED` на строки уже вправе опираться отчёт, и менять
        их задним числом нельзя — только новым сравнением.
        """
        if item.status not in DECIDABLE_STATUSES:
            raise ApiError(
                "INVALID_STATE_TRANSITION",
                "Разбирать сопоставление можно до подтверждения вердикта,"
                f" статус позиции: {item.status}",
                status_code=409,
            )

    async def _editable(
        self, comparison_id: uuid.UUID, expected_version: int, user: User
    ) -> tuple[Comparison, CheckItem]:
        """Строка, которую менеджеру разрешено править прямо сейчас."""
        self._assert_can_override(user)
        comparison = await self._session.get(Comparison, comparison_id)
        if comparison is None:
            raise ApiError(
                "COMPARISON_NOT_FOUND",
                "Строка сопоставления не найдена",
                status_code=404,
                details={"comparisonId": str(comparison_id)},
            )
        item = await self._checks.get_item(comparison.check_item_id)
        self._assert_reviewable(item)
        if comparison.stale:
            raise ApiError(
                "COMPARISON_STALE",
                "Строка устарела после пересчёта: перечитайте таблицу",
                status_code=409,
                details={"comparisonId": str(comparison.id)},
            )
        if expected_version != comparison.version:
            raise ApiError(
                "EDIT_CONFLICT",
                "Строка изменилась: перечитайте таблицу и повторите действие",
                status_code=409,
                details={"currentVersion": comparison.version},
            )
        return comparison, item

    def _assert_candidate_usable(
        self, item: CheckItem, requirement: Side, fact: Side
    ) -> None:
        """Проверка пары до её назначения (comparisons §Выбор кандидата)."""
        if fact.raw.check_item_id != item.id or fact.raw.role != "FACT":
            raise ApiError(
                "CANDIDATE_NOT_ELIGIBLE",
                "Факт принадлежит другой позиции или другому документу",
                status_code=422,
            )
        if not fact.normalized.normalized_ok:
            raise ApiError(
                "CANDIDATE_NOT_ELIGIBLE",
                "Значение не нормализовано и сравниваться не может",
                status_code=422,
                details={"factParameterId": str(fact.normalized.id)},
            )
        units = {
            requirement.normalized.canonical_unit_id,
            fact.normalized.canonical_unit_id,
        }
        if requirement.normalized.value_kind != fact.normalized.value_kind or (
            None not in units and len(units) > 1
        ):
            raise ApiError(
                "CANDIDATE_DIMENSION_MISMATCH",
                "Тип значения или единица факта не совпадают с требованием",
                status_code=422,
                details={
                    "requirementValueKind": requirement.normalized.value_kind,
                    "factValueKind": fact.normalized.value_kind,
                },
            )

    async def _recompute(
        self,
        comparison: Comparison,
        item: CheckItem,
        requirement: Side,
        fact: Side | None,
    ) -> RuleOutcome:
        """Заново считает правило строки и обновляет расчётную часть."""
        run = (
            await self._session.get(PipelineRun, comparison.pipeline_run_id)
            if comparison.pipeline_run_id
            else None
        )
        rules = await self._rules(run)
        policies = await parameter_policies(
            self._session, item, run.catalog_version_id if run else None
        )
        parameter_id = requirement.canonical_parameter_id
        policy = policies.get(parameter_id, Policy()) if parameter_id else Policy()

        outcome = evaluate_pair(requirement, fact, policy=policy, rules=rules)
        comparison.operator = outcome.operator
        comparison.computed_status = outcome.status
        comparison.explanation = {
            **outcome.explanation,
            "parameterName": requirement.normalized.canonical_name,
        }
        comparison.row_kind = "PAIRED" if fact is not None else "MISSING_FACT"
        if not comparison.overridden:
            comparison.effective_status = outcome.status
        if run is not None:
            chosen = (
                Candidate(fact=fact, method="MANUAL", score=MANUAL_SCORE, rank=1)
                if fact is not None
                else None
            )
            comparison.lineage_chain = build_lineage(requirement, chosen, run, outcome)
        return outcome

    async def _rules(self, run: PipelineRun | None) -> dict:
        """Снимок правил прогона; без него — умолчания той же версии."""
        if run is not None and run.rule_snapshot_id is not None:
            snapshot = await self._session.get(RuleSnapshot, run.rule_snapshot_id)
            if snapshot is not None:
                return snapshot.rules
        return default_rules()

    async def _save_manual_candidate(
        self, check_item_id: uuid.UUID, requirement: Side, fact: Side
    ) -> None:
        """Ручная пара тоже кандидат: иначе история выбора обрывается."""
        self._session.add(
            MatchCandidate(
                check_item_id=check_item_id,
                requirement_parameter_id=requirement.normalized.id,
                fact_parameter_id=fact.normalized.id,
                method="MANUAL",
                score=MANUAL_SCORE,
                rank=0,
                eligible=True,
                rejection_reasons=[],
            )
        )
        await self._session.flush()

    async def _side(self, normalized_parameter_id: uuid.UUID | None) -> Side | None:
        if normalized_parameter_id is None:
            return None
        row = (
            await self._session.execute(
                select(NormalizedParameter, RawParameter)
                .join(
                    RawParameter,
                    RawParameter.id == NormalizedParameter.raw_parameter_id,
                )
                .where(NormalizedParameter.id == normalized_parameter_id)
            )
        ).first()
        if row is None:
            return None
        normalized, raw = row
        return Side(normalized=normalized, raw=raw)

    async def _rows(self, check_item_id: uuid.UUID) -> list[Comparison]:
        return list(
            (
                await self._session.scalars(
                    select(Comparison)
                    .where(
                        Comparison.check_item_id == check_item_id,
                        Comparison.stale.is_(False),
                    )
                    .order_by(Comparison.row_kind, Comparison.created_at)
                )
            ).all()
        )

    async def _active_override(
        self, comparison_id: uuid.UUID
    ) -> ComparisonOverride | None:
        return await self._session.scalar(
            select(ComparisonOverride).where(
                ComparisonOverride.comparison_id == comparison_id,
                ComparisonOverride.active.is_(True),
            )
        )

    async def _deactivate_overrides(self, comparison_id: uuid.UUID) -> None:
        await self._session.execute(
            update(ComparisonOverride)
            .where(
                ComparisonOverride.comparison_id == comparison_id,
                ComparisonOverride.active.is_(True),
            )
            .values(active=False)
        )

    async def _audit(
        self, user: User, action: str, entity_id: uuid.UUID, reason: str | None
    ) -> None:
        """Ручная правка результата проверки без следа в журнале не остаётся."""
        self._session.add(
            AuditEvent(
                actor_id=user.id,
                action=action,
                entity_type="comparison",
                entity_id=entity_id,
                reason=reason,
            )
        )

    async def _refresh_verdict(self, check_item_id: uuid.UUID) -> None:
        """Вердикт пересобирается из актуальных строк после каждой правки.

        Иначе таблица и вердикт расходятся: менеджер снял отказ по параметру, а
        позиция всё ещё «не соответствует».
        """
        item = await self._session.get(CheckItem, check_item_id)
        if item is None:
            return
        await self._session.flush()
        rows = await self._rows(check_item_id)
        verdict = aggregate(verdict_entries(rows))
        await save_computed_verdict(self._session, item, verdict.verdict, verdict.basis)
