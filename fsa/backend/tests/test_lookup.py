from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from app.fsa.normalize import normalize_designation, normalize_number
from app.fsa.product_keys import designations_of, lookup_keys
from app.models import DecisionAction, Document, DocumentCheck, DocumentStatus, RegistryProductKey
from app.services.lookup import LookupService
from app.services.review import ReviewService
from tests.conftest import CERT_NUMBER, DEAD_NUMBER, TODAY, certificate

RECOGNIZED_AT = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


async def build_index(session) -> dict[str, int]:
    return await LookupService(session).rebuild_product_keys()


async def add_document(session, number: str, **overrides) -> Document:
    values = {
        "sha256": "a" * 64,
        "filename": "sert.pdf",
        "supplier": "Supplier A",
        "status": DocumentStatus.recognized.value,
        "doc_class": "certificate",
        "registration_number_key": normalize_number(number),
        "recognized_at": RECOGNIZED_AT,
        "extracted": {},
    }
    values.update(overrides)
    document = Document(**values)
    session.add(document)
    await session.flush()
    session.add(
        DocumentCheck(
            document_id=document.id,
            verdict="valid",
            matched_by="registration_number",
            registration_number_key=values["registration_number_key"],
            checked_on=TODAY,
            valid_until=date(2026, 11, 14),
            needs_manual_review=False,
            review_reasons=[],
        )
    )
    await session.flush()
    return document


def test_designations_expand_series():
    """Один сертификат покрывает серию, и в индекс должна попасть вся серия."""
    keys = designations_of(
        "Огнетушители переносные порошковые ОП-2, 3, 4, 5, выпускаемые по ТУ"
    )

    # Ключи хранятся в нормализованной форме: омоглифы сведены в латиницу.
    assert normalize_designation("ОП-2") in keys
    assert normalize_designation("ОП-5") in keys


def test_lookup_keys_fall_back_to_whole_string():
    """Короткий артикул под шаблон обозначения не подходит, искать по нему надо."""
    assert lookup_keys("Огнетушитель ОП-5(з)-АВСЕ, 5 кг") == [
        normalize_designation("ОП-5(з)-АВСЕ")
    ]
    assert lookup_keys("МГПХ") == [normalize_designation("МГПХ")]


def test_product_keys_skip_overlong_textual_enumerations():
    """Текстовый перечень не должен превышать размер ключа в Alembic."""
    overlong = "Model " + ".".join(str(number) for number in range(80))

    assert designations_of(overlong) == []
    assert lookup_keys(overlong) == []


async def test_by_product_finds_certificate(registry, session):
    await build_index(session)

    found = await LookupService(session).by_product("Допинг 2.02", on=TODAY)

    assert found
    assert found[0].certificate.registration_number == CERT_NUMBER
    assert found[0].matched_by == "product_key"


async def test_by_product_puts_alive_certificate_first(registry, session):
    session.add(
        certificate(
            "ЕАЭС RU С-RU.ПБ97.В.00777/25",
            valid_until=date(2030, 1, 1),
            product_full_name="Модули установок пожаротушения тонкораспыленной водой МУПТВ-13",
        )
    )
    await session.flush()
    await build_index(session)

    found = await LookupService(session).by_product("МУПТВ-13", on=TODAY)

    assert len(found) >= 2
    assert found[0].certificate.valid_until == date(2030, 1, 1)


async def test_by_number_returns_certificate(registry, session):
    found = await LookupService(session).by_number(CERT_NUMBER, on=TODAY)

    assert found is not None
    assert found.matched_by == "registration_number"
    assert found.certificate.registration_number == CERT_NUMBER


async def test_link_by_number_without_document_needs_review(registry, session):
    result = await LookupService(session).link(
        position_ref="line-7", number=CERT_NUMBER, on=TODAY
    )

    assert result["verdict"] == "valid"
    assert result["document"] is None
    assert result["needs_manual_review"] is True
    assert "файл документа не загружен — приложить нечего" in result["review_reasons"]


async def test_link_requires_human_confirmation(registry, session):
    await add_document(session, CERT_NUMBER)

    result = await LookupService(session).link(
        position_ref="line-7", number=CERT_NUMBER, on=TODAY
    )

    assert result["document"] is None
    assert "документ загружен, но не подтверждён человеком" in result["review_reasons"]


async def test_link_is_clean_after_confirmation(registry, session):
    document = await add_document(session, CERT_NUMBER)
    await ReviewService(session).decide(
        document, action=DecisionAction.confirm.value, reviewer="Иванов"
    )

    result = await LookupService(session).link(
        position_ref="line-7", number=CERT_NUMBER, on=TODAY
    )

    assert result["needs_manual_review"] is False
    assert result["document"]["confirmed_by"] == "Иванов"
    assert result["document"]["attachable"] is True


async def test_link_by_product_with_several_candidates_asks_manager(registry, session):
    session.add(
        certificate(
            "ЕАЭС RU С-RU.ПБ97.В.00778/25",
            valid_until=date(2030, 1, 1),
            product_full_name="Модули установок пожаротушения тонкораспыленной водой МУПТВ-13",
        )
    )
    await session.flush()
    await build_index(session)

    result = await LookupService(session).link(
        position_ref="line-9", product="МУПТВ-13", on=TODAY
    )

    assert len(result["candidates"]) >= 2
    assert result["needs_manual_review"] is True
    assert any("несколько сертификатов" in reason for reason in result["review_reasons"])


async def test_link_reports_dead_certificate(registry, session):
    result = await LookupService(session).link(
        position_ref="line-3", number=DEAD_NUMBER, on=TODAY
    )

    assert result["verdict"] in {"expired", "terminated"}
    assert result["needs_manual_review"] is True


async def test_link_without_match_is_not_found(registry, session):
    result = await LookupService(session).link(
        position_ref="line-1", number="ЕАЭС RU С-RU.ПБ97.В.09999/21", on=TODAY
    )

    assert result["matched_by"] == "none"
    assert result["verdict"] == "not_found"


async def test_rebuild_is_idempotent(registry, session):
    first = await build_index(session)
    second = await build_index(session)

    assert first == second
    stats = await LookupService(session).product_key_stats()
    assert stats["keys"] == first["keys"]


@pytest.mark.asyncio
async def test_rebuild_survives_repeated_designations(session):
    """Одно обозначение в двух полях карточки не должно ронять пересборку.

    Пара (карточка, ключ) уникальна в индексе, а «Модель» и «Полное
    наименование» часто повторяют друг друга — на большом срезе реестра это
    встречается постоянно.
    """
    card = certificate(
        "ЕАЭС RU С-RU.ПБ01.В.00001/24",
        product_model="ЭМШ 1500",
        product_full_name="Извещатель ЭМШ 1500",
    )
    session.add(card)
    await session.flush()

    result = await LookupService(session).rebuild_product_keys()
    await session.commit()

    assert result["certificates"] >= 1
    stored = list(
        await session.scalars(
            select(RegistryProductKey).where(RegistryProductKey.certificate_id == card.id)
        )
    )
    assert len(stored) == len({row.product_key for row in stored})
