"""Подбор пар «требование ТЗ ↔ факт паспорта».

Два прохода канона §15.1:

1. для каждого requirement-параметра ищется факт по каноническому параметру
   каталога — тому самому, к которому нормализация уже привела оба названия;
2. оставшиеся факты становятся строками `EXTRA_FACT` со статусом
   `NOT_REQUIRED` — их менеджер может связать с требованием вручную.

Fuzzy и embedding не реализованы сознательно: канон разрешает им только
*предлагать* пару, а любой предложенный, но не подтверждённый кандидат всё
равно даёт `REVIEW_REQUIRED`. Пока каталог синонимов ведётся вручную, честнее
показать «не найдено», чем похожее по буквам чужое значение. Место под них
оставлено: `match_method` в базе уже содержит `FUZZY`/`EMBEDDING`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalogs.normalize import normalize as normalize_text
from app.modules.extraction.models import ExtractionRun, RawParameter
from app.modules.order_matching.models import MatchCandidate
from app.modules.normalization.models import NormalizedParameter

#: Совпадение по каноническому параметру: имена сторон совпали дословно.
SCORE_CANONICAL = Decimal("1.0")
#: То же попадание, но названия сторон разные — их свёл синоним каталога.
SCORE_ALIAS = Decimal("0.9")


@dataclass(frozen=True)
class Side:
    """Нормализованный параметр одной стороны вместе с исходным названием."""

    normalized: NormalizedParameter
    raw: RawParameter

    @property
    def canonical_parameter_id(self) -> uuid.UUID | None:
        return self.normalized.canonical_parameter_id


@dataclass
class Candidate:
    """Кандидат в пару к требованию."""

    fact: Side
    method: str
    score: Decimal
    rank: int = 0
    eligible: bool = True
    rejection_reasons: list[str] = field(default_factory=list)


@dataclass
class Pair:
    """Требование и выбранный к нему факт (может отсутствовать)."""

    requirement: Side
    candidates: list[Candidate]

    @property
    def chosen(self) -> Candidate | None:
        """Кандидат берётся, только если он единственный лучший.

        Два одинаково хороших кандидата — не повод выбрать первый попавшийся:
        канон §15.1 требует `REVIEW_REQUIRED`.
        """
        eligible = [item for item in self.candidates if item.eligible]
        if not eligible or self.ambiguous:
            return None
        return eligible[0]

    @property
    def ambiguous(self) -> bool:
        eligible = [item for item in self.candidates if item.eligible]
        return len(eligible) > 1 and eligible[0].score == eligible[1].score


@dataclass
class MatchResult:
    pairs: list[Pair]
    extra_facts: list[Side]


class ParameterMatcher:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def match(self, check_item_id: uuid.UUID) -> MatchResult:
        """Строит пары по последнему извлечению каждой роли."""
        requirements = await self._sides(check_item_id, "REQUIREMENT")
        facts = await self._sides(check_item_id, "FACT")

        facts_by_parameter: dict[uuid.UUID, list[Side]] = {}
        for fact in facts:
            if fact.canonical_parameter_id is not None:
                facts_by_parameter.setdefault(fact.canonical_parameter_id, []).append(
                    fact
                )

        pairs: list[Pair] = []
        used: set[uuid.UUID] = set()
        for requirement in requirements:
            candidates = self._candidates(requirement, facts_by_parameter)
            pair = Pair(requirement=requirement, candidates=candidates)
            chosen = pair.chosen
            if chosen is not None:
                used.add(chosen.fact.normalized.id)
            pairs.append(pair)

        extra_facts = [
            fact
            for fact in facts
            if fact.normalized.id not in used and fact.normalized.normalized_ok
        ]
        return MatchResult(pairs=pairs, extra_facts=extra_facts)

    async def persist(self, check_item_id: uuid.UUID, result: MatchResult) -> int:
        """Сохраняет кандидатов, включая отвергнутых."""
        saved = 0
        for pair in result.pairs:
            for candidate in pair.candidates:
                self._session.add(
                    MatchCandidate(
                        check_item_id=check_item_id,
                        requirement_parameter_id=pair.requirement.normalized.id,
                        fact_parameter_id=candidate.fact.normalized.id,
                        method=candidate.method,
                        score=candidate.score,
                        rank=candidate.rank,
                        eligible=candidate.eligible,
                        rejection_reasons=candidate.rejection_reasons,
                    )
                )
                saved += 1
        await self._session.flush()
        return saved

    # --- внутреннее -----------------------------------------------------

    def _candidates(
        self, requirement: Side, facts_by_parameter: dict[uuid.UUID, list[Side]]
    ) -> list[Candidate]:
        parameter_id = requirement.canonical_parameter_id
        if parameter_id is None:
            return []

        candidates = [
            Candidate(
                fact=fact,
                method=self._method(requirement, fact),
                score=self._score(requirement, fact),
                eligible=fact.normalized.normalized_ok,
                rejection_reasons=(
                    [] if fact.normalized.normalized_ok else ["NOT_NORMALIZED"]
                ),
            )
            for fact in facts_by_parameter.get(parameter_id, [])
        ]
        candidates.sort(key=lambda item: (not item.eligible, -item.score))
        for position, candidate in enumerate(candidates, start=1):
            candidate.rank = position
            if position > 1 and candidate.eligible:
                candidate.rejection_reasons.append("LOWER_RANK")
        return candidates

    def _method(self, requirement: Side, fact: Side) -> str:
        """Как сведены стороны: дословно или через синоним каталога."""
        if normalize_text(requirement.raw.name_text) == normalize_text(
            fact.raw.name_text
        ):
            return "CANONICAL"
        return "ALIAS"

    def _score(self, requirement: Side, fact: Side) -> Decimal:
        return (
            SCORE_CANONICAL
            if self._method(requirement, fact) == "CANONICAL"
            else SCORE_ALIAS
        )

    async def _sides(self, check_item_id: uuid.UUID, role: str) -> list[Side]:
        """Параметры роли из последнего извлечения.

        Позицию можно перепроверять: у неё несколько прогонов извлечения, и
        сравнивать нужно с последним, а не со смесью попыток. Исключённые
        менеджером параметры (`excluded`) в сопоставление не идут.
        """
        latest_run = await self._session.scalar(
            select(RawParameter.extraction_run_id)
            .join(ExtractionRun, ExtractionRun.id == RawParameter.extraction_run_id)
            .where(
                RawParameter.check_item_id == check_item_id,
                RawParameter.role == role,
            )
            .order_by(ExtractionRun.created_at.desc())
            .limit(1)
        )
        if latest_run is None:
            return []

        rows = (
            await self._session.execute(
                select(RawParameter, NormalizedParameter)
                .join(
                    NormalizedParameter,
                    NormalizedParameter.raw_parameter_id == RawParameter.id,
                )
                .where(
                    RawParameter.check_item_id == check_item_id,
                    RawParameter.role == role,
                    RawParameter.extraction_run_id == latest_run,
                    RawParameter.excluded.is_(False),
                )
                .order_by(RawParameter.created_at)
            )
        ).all()
        return [Side(normalized=normalized, raw=raw) for raw, normalized in rows]
