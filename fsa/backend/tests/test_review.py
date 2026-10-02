from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.models import (
    DecisionAction,
    Document,
    DocumentCheck,
    DocumentStatus,
)
from app.repository import RegistryRepository
from app.services.review import ReviewService, UnknownDecision, effective_state
from tests.conftest import CERT_NUMBER, DEAD_NUMBER, TODAY
from app.fsa.normalize import normalize_number

RECOGNIZED_AT = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


async def make_document(session, **overrides) -> Document:
    values = {
        "sha256": "a" * 64,
        "filename": "sert.pdf",
        "supplier": "Supplier A",
        "status": DocumentStatus.recognized.value,
        "doc_class": "certificate",
        "recognized_at": RECOGNIZED_AT,
        "extracted": {},
    }
    values.update(overrides)
    document = Document(**values)
    session.add(document)
    await session.flush()
    return document


async def make_check(session, document, **overrides) -> DocumentCheck:
    values = {
        "document_id": document.id,
        "verdict": "valid",
        "matched_by": "registration_number",
        "registration_number_key": normalize_number(CERT_NUMBER),
        "checked_on": TODAY,
        "valid_until": date(2026, 11, 14),
        "needs_manual_review": False,
        "review_reasons": [],
    }
    values.update(overrides)
    check = DocumentCheck(**values)
    session.add(check)
    await session.flush()
    return check


async def test_document_without_decision_is_not_attachable(session):
    """Действующий сертификат без подтверждения к КП не прикладывается."""
    document = await make_document(session)
    check = await make_check(session, document)

    state = effective_state(document, check, None)

    assert state.decided is False
    assert state.needs_review is True
    assert state.attachable is False


async def test_confirm_makes_valid_document_attachable(registry, session):
    document = await make_document(session)
    await make_check(session, document)

    _, state = await ReviewService(session).decide(
        document, action=DecisionAction.confirm.value, reviewer="Иванов"
    )

    assert state.attachable is True
    assert state.needs_review is False
    assert state.reviewer == "Иванов"


async def test_reject_keeps_document_in_review(session):
    document = await make_document(session)
    await make_check(session, document)

    _, state = await ReviewService(session).decide(
        document,
        action=DecisionAction.reject.value,
        reviewer="Иванов",
        comment="сертификат не на эту модель",
    )

    assert state.attachable is False
    assert state.needs_review is True


async def test_corrected_number_is_checked_against_registry(registry, session):
    """Номер от человека проверяется так же, как прочитанный движком."""
    document = await make_document(session)
    await make_check(session, document, verdict="not_found", registration_number_key="")

    decision, state = await ReviewService(session).decide(
        document,
        action=DecisionAction.correct.value,
        reviewer="Иванов",
        number=CERT_NUMBER,
        repository=RegistryRepository(session),
        on=TODAY,
    )

    assert decision.corrected_verdict == "valid"
    assert decision.corrected_valid_until == date(2026, 11, 14)
    assert state.verdict == "valid"
    assert state.attachable is True


async def test_corrected_number_that_is_dead_is_not_attachable(registry, session):
    document = await make_document(session)
    await make_check(session, document, verdict="not_found")

    _, state = await ReviewService(session).decide(
        document,
        action=DecisionAction.correct.value,
        reviewer="Иванов",
        number=DEAD_NUMBER,
        repository=RegistryRepository(session),
        on=TODAY,
    )

    assert state.verdict in {"expired", "terminated"}
    assert state.attachable is False


async def test_decision_goes_stale_when_verdict_changes(registry, session):
    """Подтверждали действующий сертификат — сегодня он просрочен."""
    document = await make_document(session)
    check = await make_check(session, document)
    service = ReviewService(session)
    await service.decide(
        document, action=DecisionAction.confirm.value, reviewer="Иванов"
    )

    check.verdict = "expired"
    await session.flush()

    decision = await service.latest_decision(document.id)
    state = effective_state(document, check, decision)

    assert state.stale is True
    assert "вердикт изменился" in state.stale_reason
    assert state.attachable is False
    assert state.needs_review is True


async def test_decision_goes_stale_after_new_recognition(registry, session):
    document = await make_document(session)
    check = await make_check(session, document)
    service = ReviewService(session)
    await service.decide(
        document, action=DecisionAction.confirm.value, reviewer="Иванов"
    )

    document.recognized_at = RECOGNIZED_AT + timedelta(days=1)
    await session.flush()

    decision = await service.latest_decision(document.id)
    state = effective_state(document, check, decision)

    assert state.stale is True
    assert state.stale_reason == "документ распознан заново"


async def test_history_is_append_only_and_latest_wins(registry, session):
    document = await make_document(session)
    await make_check(session, document)
    service = ReviewService(session)

    await service.decide(
        document, action=DecisionAction.confirm.value, reviewer="Иванов"
    )
    await service.decide(
        document,
        action=DecisionAction.reject.value,
        reviewer="Петров",
        comment="документ не тот",
    )

    history = await service.history(document.id)
    assert len(history) == 2
    assert history[0].action == DecisionAction.reject.value
    assert history[0].reviewer == "Петров"


async def test_reviewer_is_required(session):
    document = await make_document(session)
    with pytest.raises(UnknownDecision):
        await ReviewService(session).decide(
            document, action=DecisionAction.confirm.value, reviewer="  "
        )


async def test_correct_without_number_is_rejected(session):
    document = await make_document(session)
    with pytest.raises(UnknownDecision):
        await ReviewService(session).decide(
            document, action=DecisionAction.correct.value, reviewer="Иванов"
        )


async def test_queue_puts_untouched_documents_first(registry, session):
    confirmed = await make_document(session, sha256="b" * 64, filename="confirmed.pdf")
    await make_check(session, confirmed)
    await ReviewService(session).decide(
        confirmed, action=DecisionAction.confirm.value, reviewer="Иванов"
    )
    fresh = await make_document(session, sha256="c" * 64, filename="fresh.pdf")
    await make_check(session, fresh, verdict="not_found")

    total, items = await ReviewService(session).queue(state="pending")

    assert total == 1
    assert items[0]["filename"] == "fresh.pdf"

    total_all, items_all = await ReviewService(session).queue(state="all")
    assert total_all == 2
    assert items_all[0]["decided"] is False


async def test_summary_counts_decisions(registry, session):
    first = await make_document(session, sha256="d" * 64)
    await make_check(session, first)
    await ReviewService(session).decide(
        first, action=DecisionAction.confirm.value, reviewer="Иванов"
    )
    second = await make_document(session, sha256="e" * 64)
    await make_check(session, second, verdict="not_found")

    summary = await ReviewService(session).summary()

    assert summary["documents"] == 2
    assert summary["confirmed"] == 1
    assert summary["pending"] == 1
