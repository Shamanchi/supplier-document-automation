"""Этап MATCHING: сопоставление, правила, вердикт позиции.

Модуль собирает воедино три вещи и ничего не решает сам:

* `matching` подбирает пары requirement↔fact;
* `rules.engine` даёт статус и объяснение по зафиксированному снимку правил;
* `verdicts.aggregate` сводит статусы в вердикт позиции.

Почему снимок правил фиксируется здесь, а не берётся «текущий»: отчёт обязан
воспроизводиться через год (§15.8). Прогон запоминает `rule_snapshot_id`, и
повторный расчёт по тому же прогону даст тот же результат, даже если правила
успели измениться.

Повторный прогон не удаляет прежние сравнения, а помечает их `stale`: история
проверки — часть аудита, а не мусор.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalogs.models import ParameterTemplate
from app.modules.checks.models import CheckItem, PipelineRun
from app.modules.order_matching.service import ParameterMatcher, Pair, Side
from app.modules.rules.engine import (
    MAJOR,
    MINOR,
    NOT_FOUND_IN_FACT,
    NOT_REQUIRED,
    REVIEW_REQUIRED,
    Policy,
    RuleOutcome,
    compare,
    default_rules,
    policy_from_template,
    rules_checksum,
)
from app.modules.rules.models import Comparison, RuleSnapshot
from app.modules.verdicts.aggregate import VerdictEntry, aggregate
from app.modules.verdicts.batch import refresh_batch_verdict
from app.modules.verdicts.models import ItemVerdict


class MatchingError(Exception):
    """Сравнивать нечем; прогон обязан завершиться отказом с этим кодом."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class MatchingStats:
    """Что получилось на этапе — уходит в метрики этапа и в событие."""

    comparisons: int
    candidates: int
    compliant: int
    non_compliant: int
    review_required: int
    not_found: int
    extra_facts: int
    verdict: str
    rule_snapshot_id: uuid.UUID


class MatchingService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def run(self, run: PipelineRun, item: CheckItem) -> MatchingStats:
        """Выполняет сопоставление и сравнение для позиции."""
        snapshot = await self.ensure_snapshot(run)
        rules = snapshot.rules

        matcher = ParameterMatcher(self._session)
        result = await matcher.match(item.id)
        if not result.pairs:
            raise MatchingError(
                "NO_REQUIREMENT_PARAMETERS",
                "В документе требований не найдено характеристик для сравнения",
            )

        policies = await self._policies(item, run.catalog_version_id)

        await self._mark_previous_stale(item.id)
        candidates = await matcher.persist(item.id, result)

        entries: list[VerdictEntry] = []
        rows: list[Comparison] = []
        for pair in result.pairs:
            comparison, entry = self._compare_pair(run, item, pair, policies, rules)
            rows.append(comparison)
            entries.append(entry)
        for fact in result.extra_facts:
            rows.append(self._extra_fact_row(run, item, fact, rules))
            # На вердикт лишний факт не влияет, но в распределении статусов
            # обязан быть виден: иначе `basis` не сходится с таблицей отчёта.
            entries.append(
                VerdictEntry(status=NOT_REQUIRED, mandatory=False, criticality=MINOR)
            )

        for row in rows:
            self._session.add(row)
        await self._session.flush()

        verdict = aggregate(entries)
        await self._save_verdict(item, verdict.verdict, verdict.basis)

        counted = _counter(row.effective_status for row in rows)
        return MatchingStats(
            comparisons=len(rows),
            candidates=candidates,
            compliant=counted["COMPLIANT"],
            non_compliant=counted["NON_COMPLIANT"],
            review_required=counted[REVIEW_REQUIRED],
            not_found=counted[NOT_FOUND_IN_FACT],
            extra_facts=len(result.extra_facts),
            verdict=verdict.verdict,
            rule_snapshot_id=snapshot.id,
        )

    # --- снимок правил --------------------------------------------------

    async def ensure_snapshot(self, run: PipelineRun) -> RuleSnapshot:
        """Снимок правил прогона: уже зафиксированный или новый.

        Один и тот же набор правил не дублируется: снимок ищется по паре
        (версия, checksum) — так требует уникальный ключ схемы.
        """
        if run.rule_snapshot_id is not None:
            existing = await self._session.get(RuleSnapshot, run.rule_snapshot_id)
            if existing is not None:
                return existing

        rules = default_rules()
        rules["catalogVersionId"] = (
            str(run.catalog_version_id) if run.catalog_version_id else None
        )
        checksum = rules_checksum(rules)
        snapshot = await self._session.scalar(
            select(RuleSnapshot).where(
                RuleSnapshot.rule_version == rules["ruleVersion"],
                RuleSnapshot.checksum == checksum,
            )
        )
        if snapshot is None:
            snapshot = RuleSnapshot(
                id=uuid.uuid4(),
                rule_version=rules["ruleVersion"],
                rules=rules,
                checksum=checksum,
            )
            self._session.add(snapshot)
            await self._session.flush()
        run.rule_snapshot_id = snapshot.id
        return snapshot

    # --- сравнение ------------------------------------------------------

    def _compare_pair(
        self,
        run: PipelineRun,
        item: CheckItem,
        pair: Pair,
        policies: dict[uuid.UUID, Policy],
        rules: dict,
    ) -> tuple[Comparison, VerdictEntry]:
        requirement = pair.requirement
        parameter_id = requirement.canonical_parameter_id
        policy = policies.get(parameter_id, Policy()) if parameter_id else Policy()
        chosen = pair.chosen
        fact = chosen.fact if chosen is not None else None

        outcome = evaluate_pair(requirement, fact, policy=policy, rules=rules)
        if fact is None and pair.ambiguous:
            # Кандидаты есть, но выбрать однозначно нельзя (канон §15.1).
            outcome = RuleOutcome(
                status=REVIEW_REQUIRED,
                operator=outcome.operator,
                explanation={
                    **outcome.explanation,
                    "issue": "AMBIGUOUS_CANDIDATES",
                    "candidates": len(
                        [item for item in pair.candidates if item.eligible]
                    ),
                },
            )

        row_kind = "PAIRED" if fact is not None else "MISSING_FACT"
        comparison = Comparison(
            id=uuid.uuid4(),
            check_item_id=item.id,
            pipeline_run_id=run.id,
            canonical_parameter_id=parameter_id,
            requirement_parameter_id=requirement.normalized.id,
            fact_parameter_id=fact.normalized.id if fact is not None else None,
            row_kind=row_kind,
            operator=outcome.operator,
            computed_status=outcome.status,
            effective_status=outcome.status,
            explanation={
                **outcome.explanation,
                "parameterName": requirement.normalized.canonical_name,
            },
            lineage_chain=build_lineage(requirement, chosen, run, outcome),
            version=1,
        )
        entry = VerdictEntry(
            status=outcome.status,
            mandatory=policy.mandatory,
            criticality=policy.criticality,
        )
        return comparison, entry

    def _extra_fact_row(
        self, run: PipelineRun, item: CheckItem, fact: Side, rules: dict
    ) -> Comparison:
        """Факт без требования: показывается, но на вердикт не влияет."""
        return Comparison(
            id=uuid.uuid4(),
            check_item_id=item.id,
            pipeline_run_id=run.id,
            canonical_parameter_id=fact.canonical_parameter_id,
            requirement_parameter_id=None,
            fact_parameter_id=fact.normalized.id,
            row_kind="EXTRA_FACT",
            operator=None,
            computed_status=NOT_REQUIRED,
            effective_status=NOT_REQUIRED,
            explanation={
                "required": None,
                "fact": _operand_of(fact),
                "issue": "REQUIREMENT_ABSENT",
                "result": None,
                "ruleVersion": rules.get("ruleVersion"),
                "parameterName": fact.normalized.canonical_name,
                "mandatory": False,
                "criticality": "MINOR",
            },
            lineage_chain=build_lineage(fact, None, run, None),
            version=1,
        )

    # --- окружение ------------------------------------------------------

    async def _policies(
        self, item: CheckItem, catalog_version_id: uuid.UUID | None
    ) -> dict[uuid.UUID, Policy]:
        return await parameter_policies(self._session, item, catalog_version_id)

    async def _mark_previous_stale(self, check_item_id: uuid.UUID) -> None:
        await self._session.execute(
            update(Comparison)
            .where(
                Comparison.check_item_id == check_item_id,
                Comparison.stale.is_(False),
            )
            .values(stale=True)
        )

    async def _save_verdict(self, item: CheckItem, verdict: str, basis: dict) -> None:
        await save_computed_verdict(self._session, item, verdict, basis)


def evaluate_pair(
    requirement: Side, fact: Side | None, *, policy: Policy, rules: dict
) -> RuleOutcome:
    """Статус пары «требование ↔ факт» по снимку правил.

    Отдельной функцией, потому что пару считают двое: этап `MATCHING` при
    первом расчёте и ручной разбор при пересчёте строки. Разойтись им нельзя —
    иначе после ручной правки статус менялся бы без изменения данных.
    """
    if requirement.canonical_parameter_id is None:
        # Требование не опознано каталогом: сравнивать не с чем и нечем.
        return RuleOutcome(
            status=REVIEW_REQUIRED,
            operator=None,
            explanation={
                "required": _operand_of(requirement),
                "fact": None if fact is None else _operand_of(fact),
                "issue": "UNKNOWN_REQUIREMENT_PARAMETER",
                "result": False,
                "ruleVersion": rules.get("ruleVersion"),
                "mandatory": policy.mandatory,
                "criticality": policy.criticality,
            },
        )
    return compare(
        requirement.normalized.typed_value,
        fact.normalized.typed_value if fact is not None else None,
        value_kind=requirement.normalized.value_kind,
        policy=policy,
        rules=rules,
    )


async def parameter_policies(
    session: AsyncSession, item: CheckItem, catalog_version_id: uuid.UUID | None
) -> dict[uuid.UUID, Policy]:
    """Политики параметров из шаблона подтверждённого типа оборудования.

    Тип не подтверждён — политик нет: все параметры считаются обязательными
    со значимостью MAJOR и без допуска (безопасный default §15.3.3).
    """
    if item.equipment_type_id is None or catalog_version_id is None:
        return {}
    templates = (
        await session.scalars(
            select(ParameterTemplate).where(
                ParameterTemplate.equipment_type_id == item.equipment_type_id,
                ParameterTemplate.catalog_version_id == catalog_version_id,
            )
        )
    ).all()
    policies: dict[uuid.UUID, Policy] = {}
    for template in templates:
        policy = policy_from_template(template.operator, template.tolerance)
        policies[template.canonical_parameter_id] = Policy(
            operator=policy.operator,
            tolerance_value=policy.tolerance_value,
            tolerance_type=policy.tolerance_type,
            tolerance_source=policy.tolerance_source,
            tolerance_source_id=str(template.id),
            mandatory=template.required,
            criticality=policy.criticality,
        )
    return policies


async def save_computed_verdict(
    session: AsyncSession, item: CheckItem, verdict: str, basis: dict
) -> None:
    """Пишет computed-вердикт; решение менеджера сбрасывается.

    Пересчёт означает новые данные: подтверждение, выданное по прежним
    сравнениям, к ним больше не относится.
    """
    existing = await session.scalar(
        select(ItemVerdict).where(ItemVerdict.check_item_id == item.id)
    )
    if existing is None:
        session.add(
            ItemVerdict(
                id=uuid.uuid4(),
                check_item_id=item.id,
                computed_verdict=verdict,
                manager_verdict="PENDING",
                basis=basis,
            )
        )
    else:
        existing.computed_verdict = verdict
        existing.manager_verdict = "PENDING"
        existing.basis = basis
        existing.comment = None
        existing.approved_by = None
        existing.approved_at = None
        existing.version += 1
    # Вердикт заявки — производная от вердиктов позиций: пересчитывается здесь,
    # иначе список заявок показывал бы устаревший `final_verdict`.
    await session.flush()
    await refresh_batch_verdict(session, item.batch_id)


def verdict_entries(rows: list[Comparison]) -> list[VerdictEntry]:
    """Вклад строк таблицы в вердикт позиции.

    `mandatory` и `criticality` берутся из `explanation`: политика параметра
    зафиксирована там в момент сравнения, и пересчитывать вердикт нужно по ней,
    а не по нынешнему шаблону — иначе правка каталога задним числом изменила бы
    уже показанный менеджеру результат.
    """
    return [
        VerdictEntry(
            status=row.effective_status,
            mandatory=bool((row.explanation or {}).get("mandatory", True)),
            criticality=str((row.explanation or {}).get("criticality", MAJOR)),
        )
        for row in rows
    ]


def _counter(statuses) -> dict[str, int]:
    counts = {
        "COMPLIANT": 0,
        "NON_COMPLIANT": 0,
        REVIEW_REQUIRED: 0,
        NOT_FOUND_IN_FACT: 0,
        NOT_REQUIRED: 0,
    }
    for status in statuses:
        if status in counts:
            counts[status] += 1
    return counts


def _operand_of(side: Side) -> dict:
    """Операнд для объяснения, когда правило не запускалось."""
    typed = side.normalized.typed_value or {}
    return {
        "kind": typed.get("kind"),
        "value": side.raw.value_text,
        "unit": typed.get("canonicalUnit") or side.raw.unit_text,
    }


def build_lineage(
    side: Side, chosen, run: PipelineRun, outcome: RuleOutcome | None
) -> list:
    """Происхождение значения по стадиям (канон §Lineage).

    Цепочка отвечает на вопрос «откуда взялась эта строка отчёта»: какой прогон
    извлёк значение, каким методом, чем нормализовал и по какому снимку правил
    получен статус.
    """
    chain: list[dict] = [
        {
            "step": "extraction",
            "raw_parameter_id": str(side.raw.id),
            "method": side.raw.extraction_method,
            "confidence": (
                str(side.raw.confidence) if side.raw.confidence is not None else None
            ),
        },
        {
            "step": "normalization",
            "normalized_parameter_id": str(side.normalized.id),
            "catalog_version_id": str(side.normalized.catalog_version_id),
            "canonical_parameter_id": (
                str(side.normalized.canonical_parameter_id)
                if side.normalized.canonical_parameter_id
                else None
            ),
        },
    ]
    if chosen is not None:
        chain.append(
            {
                "step": "match",
                "method": chosen.method,
                "score": str(chosen.score),
                "fact_parameter_id": str(chosen.fact.normalized.id),
            }
        )
    if outcome is not None:
        chain.append(
            {
                "step": "rule",
                "rule_snapshot_id": str(run.rule_snapshot_id)
                if run.rule_snapshot_id
                else None,
                "operator": outcome.operator,
                "result": outcome.status,
            }
        )
    return chain
