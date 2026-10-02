"""Подтверждение человеком: очередь, решение, актуальность решения.

Машинная сверка отвечает на вопрос «что говорит реестр». Прикладывать документ
к коммерческому предложению — ответственность человека, и она должна быть
зафиксирована: кто, когда, на каком состоянии документа.

Три вещи, которые определяют поведение слоя:

* **решение не переписывает вердикт** — хранятся оба, иначе «реестр говорит
  valid» и «менеджер посмотрел и согласился» станут неразличимы;
* **решение устаревает само** — оно привязано к вердикту, номеру и времени
  распознавания; изменилось любое из трёх, и документ возвращается в очередь;
* **исправленный человеком номер проверяется по реестру**, а не принимается на
  веру: человек ошибается в чтении бланка ровно так же, как движок.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.fsa.normalize import normalize_number
from app.fsa.schema import Verdict
from app.models import (
    DecisionAction,
    Document,
    DocumentCheck,
    DocumentDecision,
    DocumentStatus,
)
from app.repository import RegistryRepository

# Причина, по которой решение перестало быть актуальным.
STALE_VERDICT = "вердикт изменился с {was} на {now}"
STALE_NUMBER = "номер документа изменился"
STALE_RECOGNITION = "документ распознан заново"


class UnknownDecision(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EffectiveState:
    """Что показывать по документу с учётом решения человека."""

    verdict: str
    decided: bool
    action: str
    reviewer: str
    decided_at: str
    stale: bool
    stale_reason: str
    attachable: bool
    needs_review: bool
    comment: str = ""
    corrected_number: str = ""


def _as_utc(value: datetime | None) -> datetime | None:
    """Момент времени без часового пояса — SQLite отдаёт naive, Postgres aware.

    Сравнивать их напрямую нельзя: naive-значение из базы никогда не совпадёт
    с aware-значением в памяти, и подтверждение выглядело бы устаревшим сразу
    после сохранения.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _stale_reason(document: Document, check: DocumentCheck | None, decision: DocumentDecision) -> str:
    if check is not None and decision.verdict_at_decision != check.verdict:
        return STALE_VERDICT.format(was=decision.verdict_at_decision, now=check.verdict)
    if check is not None and decision.number_at_decision != (
        check.registration_number_key or ""
    ):
        return STALE_NUMBER
    decided_on = _as_utc(decision.recognized_at_decision)
    recognized = _as_utc(document.recognized_at)
    if decided_on is not None and recognized is not None and decided_on != recognized:
        return STALE_RECOGNITION
    return ""


def effective_state(
    document: Document,
    check: DocumentCheck | None,
    decision: DocumentDecision | None,
) -> EffectiveState:
    machine_verdict = check.verdict if check else Verdict.UNKNOWN.value
    if decision is None:
        return EffectiveState(
            verdict=machine_verdict,
            decided=False,
            action="",
            reviewer="",
            decided_at="",
            stale=False,
            stale_reason="",
            # Прикладывать к КП без подтверждения нельзя даже действующий
            # сертификат: полярность контура — «при сомнении не привязывать».
            attachable=False,
            needs_review=True,
        )

    stale_reason = _stale_reason(document, check, decision)
    action = decision.action
    verdict = (
        decision.corrected_verdict
        if action == DecisionAction.correct.value and decision.corrected_verdict
        else machine_verdict
    )
    attachable = (
        not stale_reason
        and action in {DecisionAction.confirm.value, DecisionAction.correct.value}
        and verdict == Verdict.VALID.value
    )
    return EffectiveState(
        verdict=verdict,
        decided=True,
        action=action,
        reviewer=decision.reviewer,
        decided_at=decision.created_at.isoformat() if decision.created_at else "",
        stale=bool(stale_reason),
        stale_reason=stale_reason,
        attachable=attachable,
        needs_review=bool(stale_reason) or action == DecisionAction.reject.value,
        comment=decision.comment,
        corrected_number=decision.corrected_number,
    )


class ReviewService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_check(self, document_id) -> DocumentCheck | None:
        return await self._session.scalar(
            select(DocumentCheck)
            .where(DocumentCheck.document_id == document_id)
            .order_by(DocumentCheck.checked_on.desc(), DocumentCheck.created_at.desc())
            .limit(1)
        )

    async def latest_decision(self, document_id) -> DocumentDecision | None:
        return await self._session.scalar(
            select(DocumentDecision)
            .where(DocumentDecision.document_id == document_id)
            .order_by(DocumentDecision.created_at.desc())
            .limit(1)
        )

    async def history(self, document_id) -> list[DocumentDecision]:
        return list(
            await self._session.scalars(
                select(DocumentDecision)
                .where(DocumentDecision.document_id == document_id)
                .order_by(DocumentDecision.created_at.desc())
            )
        )

    async def decide(
        self,
        document: Document,
        *,
        action: str,
        reviewer: str,
        comment: str = "",
        number: str = "",
        repository: RegistryRepository | None = None,
        on: date | None = None,
    ) -> tuple[DocumentDecision, EffectiveState]:
        """Записать решение человека. Прошлые решения остаются в истории."""
        if action not in {item.value for item in DecisionAction}:
            raise UnknownDecision(f"неизвестное действие {action!r}")
        if not reviewer.strip():
            raise UnknownDecision("не указано, кто принимает решение")
        if action == DecisionAction.correct.value and not number.strip():
            raise UnknownDecision("для исправления нужен номер сертификата")

        check = await self.latest_check(document.id)
        decision = DocumentDecision(
            document_id=document.id,
            check_id=check.id if check else None,
            action=action,
            reviewer=reviewer.strip(),
            comment=comment.strip(),
            verdict_at_decision=check.verdict if check else "",
            number_at_decision=(check.registration_number_key if check else "") or "",
            recognized_at_decision=document.recognized_at,
        )

        if action == DecisionAction.correct.value:
            # Номер от человека проверяется по реестру так же, как прочитанный
            # движком: подтверждение относится к документу, а не к номеру.
            key = normalize_number(number)
            decision.corrected_number = number.strip()
            decision.corrected_number_key = key
            decision.corrected_verdict = Verdict.UNKNOWN.value
            if repository is not None:
                certificate = await repository.get(key)
                checked_on = on or date.today()
                if certificate is None:
                    decision.corrected_verdict = (
                        Verdict.UNKNOWN.value
                        if await repository.is_known(key)
                        else Verdict.NOT_FOUND.value
                    )
                else:
                    decision.corrected_valid_until = certificate.valid_until
                    if certificate.is_alive_on(checked_on):
                        decision.corrected_verdict = Verdict.VALID.value
                    elif (
                        certificate.valid_until is not None
                        and certificate.valid_until < checked_on
                    ):
                        decision.corrected_verdict = Verdict.EXPIRED.value
                    else:
                        decision.corrected_verdict = Verdict.TERMINATED.value

        self._session.add(decision)
        await self._session.flush()
        return decision, effective_state(document, check, decision)

    async def queue(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        state: str = "pending",
        supplier: str = "",
    ) -> tuple[int, list[dict[str, object]]]:
        """Очередь на подтверждение.

        `pending` — не смотрели вовсе либо подтверждение устарело;
        `decided` — актуальное решение есть; `all` — без фильтра.
        Порядок: сначала документы без решения, внутри — свежие сверху.
        """
        query = select(Document).where(
            Document.status == DocumentStatus.recognized.value
        )
        if supplier:
            query = query.where(Document.supplier == supplier)

        documents = list(
            await self._session.scalars(query.order_by(Document.created_at.desc()))
        )
        items: list[dict[str, object]] = []
        for document in documents:
            check = await self.latest_check(document.id)
            decision = await self.latest_decision(document.id)
            effective = effective_state(document, check, decision)
            pending = effective.needs_review or not effective.decided
            if state == "pending" and not pending:
                continue
            if state == "decided" and pending:
                continue
            items.append(
                {
                    "document_id": str(document.id),
                    "filename": document.filename,
                    "supplier": document.supplier,
                    "doc_class": document.doc_class,
                    "verdict": effective.verdict,
                    "review_reasons": list(check.review_reasons) if check else [],
                    "decided": effective.decided,
                    "action": effective.action,
                    "reviewer": effective.reviewer,
                    "decided_at": effective.decided_at,
                    "stale": effective.stale,
                    "stale_reason": effective.stale_reason,
                    "attachable": effective.attachable,
                }
            )

        total = len(items)
        # Непросмотренное вперёд: устаревшее подтверждение — тоже работа, но
        # менее срочная, чем документ, который не видел никто.
        items.sort(key=lambda item: (item["decided"], item["stale"] is False))
        return total, items[offset : offset + limit]

    async def summary(self) -> dict[str, int]:
        total = await self._session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.status == DocumentStatus.recognized.value)
        )
        pending, confirmed, rejected, corrected, stale = 0, 0, 0, 0, 0
        documents = list(
            await self._session.scalars(
                select(Document).where(
                    Document.status == DocumentStatus.recognized.value
                )
            )
        )
        for document in documents:
            check = await self.latest_check(document.id)
            decision = await self.latest_decision(document.id)
            effective = effective_state(document, check, decision)
            if not effective.decided or effective.stale:
                pending += 1
                stale += 1 if effective.stale else 0
                continue
            if effective.action == DecisionAction.confirm.value:
                confirmed += 1
            elif effective.action == DecisionAction.reject.value:
                rejected += 1
            elif effective.action == DecisionAction.correct.value:
                corrected += 1
        return {
            "documents": total or 0,
            "pending": pending,
            "stale": stale,
            "confirmed": confirmed,
            "rejected": rejected,
            "corrected": corrected,
        }
