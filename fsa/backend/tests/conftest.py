from __future__ import annotations

import uuid
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, RegistryCertificate, RegistryNumber
from app.fsa.normalize import normalize_number

CERT_NUMBER = "ЕАЭС RU С-RU.ПБ97.В.00272/21"
DEAD_NUMBER = "ЕАЭС RU С-RU.ПБ97.В.00344/22"
SUCCESSOR_NUMBER = "ЕАЭС RU С-RU.ПБ97.В.00539/24"
UNKNOWN_CARD_NUMBER = "ЕАЭС RU С-RU.АА87.В.00932/22"

TODAY = date(2026, 7, 27)


@pytest_asyncio.fixture()
async def session():
    """Отдельная SQLite-база на тест: схема совпадает с боевой, данных нет."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()


def certificate(number: str, **overrides) -> RegistryCertificate:
    values = {
        "id": uuid.uuid4(),
        "registration_number": number,
        "registration_number_key": normalize_number(number),
        "status": "Действует",
        "status_raw": "Действует",
        "valid_until": date(2026, 11, 14),
        "manufacturer_inn": "7811480470",
        "product_full_name": "Генераторы огнетушащего аэрозоля Допинг 2.02",
    }
    values.update(overrides)
    return RegistryCertificate(**values)


@pytest_asyncio.fixture()
async def registry(session):
    """Небольшой реестр: живой, мёртвый с преемником и «известный номер»."""
    session.add_all(
        [
            certificate(CERT_NUMBER),
            certificate(
                DEAD_NUMBER,
                status="Прекращен",
                status_raw="Прекращен",
                valid_until=date(2024, 3, 13),
                product_full_name="Модули установок пожаротушения тонкораспыленной водой МУПТВ",
            ),
            certificate(
                SUCCESSOR_NUMBER,
                valid_until=date(2029, 3, 10),
                product_full_name="Модули установок пожаротушения тонкораспыленной водой МУПТВ-13",
            ),
            RegistryNumber(
                registration_number_key=normalize_number(UNKNOWN_CARD_NUMBER)
            ),
        ]
    )
    await session.flush()
    return session


@pytest.fixture()
def certificate_text() -> str:
    return f"""
ЕВРАЗИЙСКИЙ ЭКОНОМИЧЕСКИЙ СОЮЗ
СЕРТИФИКАТ СООТВЕТСТВИЯ
№ {CERT_NUMBER}
Серия RU № 0336451
ПРОДУКЦИЯ
Генераторы огнетушащего аэрозоля ГОА-II-0,020-080-003 Допинг 2.02,
выпускаемые по ТУ 4854-008-69229785-2011. Серийный выпуск.
КОД ТН ВЭД ЕАЭС 8424 10 000 0
СООТВЕТСТВУЕТ ТРЕБОВАНИЯМ
ТР ЕАЭС 043/2017
СРОК ДЕЙСТВИЯ С 15.11.2021 ПО 14.11.2026 ВКЛЮЧИТЕЛЬНО
"""
