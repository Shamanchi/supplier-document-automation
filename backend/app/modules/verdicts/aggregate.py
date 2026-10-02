"""Агрегация вердикта позиции из статусов сравнений.

Правило дословно из DOMAIN_RULES_ADR §5 и канона §15.5. Вынесено отдельной
чистой функцией, потому что это единственное место, где решается, «прошла»
позиция или нет: golden-кейсы §10.3–10.6 проверяют его без базы.

Ключевые тонкости, которые легко потерять при пересказе:

* один `BLOCKER = NON_COMPLIANT` роняет позицию независимо от остального;
* `REVIEW_REQUIRED` и `NOT_FOUND_IN_FACT` среди обязательных дают `PARTIAL`,
  а не отказ: в реальных паспортах половина характеристик подразумевается по
  ГОСТ, и автоматический `NON_COMPLIANT` был бы ложным срабатыванием;
* `MINOR` в агрегацию не входит вообще — только в отчёт.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.modules.rules.engine import (
    BLOCKER,
    COMPLIANT,
    MINOR,
    NON_COMPLIANT,
    NOT_FOUND_IN_FACT,
    NOT_REQUIRED,
    REVIEW_REQUIRED,
)

#: Значения `verdict_value` в базе. `UNDER_MANAGER_REVIEW` из канона отдельным
#: значением enum не заведён: пока менеджер не решил, `manager_verdict`
#: остаётся `PENDING` — это то же состояние, выраженное существующей схемой.
VERDICT_COMPLIANT = "COMPLIANT"
VERDICT_PARTIAL = "PARTIAL"
VERDICT_NON_COMPLIANT = "NON_COMPLIANT"
VERDICT_PENDING = "PENDING"

_ALL_STATUSES = (
    COMPLIANT,
    NON_COMPLIANT,
    REVIEW_REQUIRED,
    NOT_FOUND_IN_FACT,
    NOT_REQUIRED,
)


@dataclass(frozen=True)
class VerdictEntry:
    """Вклад одного сравнения в вердикт."""

    status: str
    mandatory: bool
    criticality: str

    @property
    def counts(self) -> bool:
        return self.mandatory and self.criticality != MINOR


@dataclass(frozen=True)
class AggregatedVerdict:
    verdict: str
    basis: dict


#: Порядок «от худшего к лучшему»: вердикт заявки — худший из вердиктов
#: позиций. Заявка не может быть «соответствует», если хоть одна позиция нет.
_WORST_FIRST = (VERDICT_NON_COMPLIANT, VERDICT_PARTIAL, VERDICT_COMPLIANT)


def aggregate_batch(item_verdicts: Sequence[str | None]) -> AggregatedVerdict:
    """Вердикт заявки из вердиктов её позиций.

    На вход идут **действующие** вердикты позиций: решение менеджера, если оно
    принято, иначе расчёт. Позиция без вердикта вовсе (сравнение ещё не
    выполнялось) делает вердикт заявки `PENDING` — сводить нечего.
    """
    basis: dict[str, int] = {value: 0 for value in _WORST_FIRST}
    basis[VERDICT_PENDING] = 0
    for value in item_verdicts:
        key = value if value in basis else VERDICT_PENDING
        basis[key] += 1
    basis["items"] = len(item_verdicts)

    if not item_verdicts or basis[VERDICT_PENDING]:
        return AggregatedVerdict(VERDICT_PENDING, basis)
    for value in _WORST_FIRST:
        if basis[value]:
            return AggregatedVerdict(value, basis)
    return AggregatedVerdict(VERDICT_PENDING, basis)


def aggregate(entries: Sequence[VerdictEntry]) -> AggregatedVerdict:
    """Считает вердикт позиции и распределение статусов для отчёта."""
    basis = {status: 0 for status in _ALL_STATUSES}
    for entry in entries:
        if entry.status in basis:
            basis[entry.status] += 1

    counted = [entry for entry in entries if entry.counts]
    basis["counted"] = len(counted)
    basis["informational"] = len(entries) - len(counted)

    if any(
        entry.criticality == BLOCKER and entry.status == NON_COMPLIANT
        for entry in counted
    ):
        return AggregatedVerdict(VERDICT_NON_COMPLIANT, basis)
    if any(entry.status in (REVIEW_REQUIRED, NOT_FOUND_IN_FACT) for entry in counted):
        return AggregatedVerdict(VERDICT_PARTIAL, basis)
    if any(entry.status == NON_COMPLIANT for entry in counted):
        return AggregatedVerdict(VERDICT_NON_COMPLIANT, basis)
    if counted:
        return AggregatedVerdict(VERDICT_COMPLIANT, basis)
    # Сравнивать было нечего: вердикта нет, решает менеджер.
    return AggregatedVerdict(VERDICT_PARTIAL, basis)
