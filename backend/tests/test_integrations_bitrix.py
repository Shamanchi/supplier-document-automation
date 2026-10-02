"""Контрактные тесты интеграции с Bitrix24.

Покрывают раздел QA/contract tests из docs/api/integrations/bitrix/README.md:
happy path сделки и файлов, таймаут, 429 с `Retry-After`, 401/403 и 404,
повторный импорт и повторную публикацию, отсутствие второго комментария при
повторе, применение проверок загрузки к импортированному файлу, доступность
локального отчёта после провала публикации и отсутствие секретов в ответах.

Портал подменяется стендом `FakeBitrixClient` — той же реализацией порта, что
используется в режиме `bitrix_mode=fake`. Отдельного мока нет намеренно:
тестируется ровно то, что поедет в демонстрацию.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from email.message import Message
from urllib.error import HTTPError

import pytest
from sqlalchemy import text

from tests.conftest import requires_db

MANAGER = {"username": "manager@docprice.internal.local", "password": "manager"}
VIEWER = {"username": "viewer@docprice.internal", "password": "viewver"}

BITRIX = "/api/v1/integrations/bitrix"
BATCHES = "/api/v1/check-batches"
TENDER_ID = "12345"
DEAL_ID = "9876"
TZ_FILE = "disk-file-tz"
PASSPORT_FILE = "disk-file-passport"
PRICE_FILE = "disk-file-price"

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# --- часть без БД: политика повторов, подсказки, сводка, транспорт --------


def _settings(**overrides):
    from app.config import Settings

    return Settings(**overrides)


def _transport_error(code: str, *, retryable: bool, retry_after: int | None = None):
    from app.modules.integrations.bitrix.errors import (
        BitrixTransportCode,
        BitrixTransportError,
    )

    return BitrixTransportError(
        BitrixTransportCode(code),
        "проверка",
        retryable=retryable,
        retry_after_seconds=retry_after,
    )


def test_retry_policy_backs_off_exponentially() -> None:
    from app.modules.integrations.bitrix.retry import plan_retry

    settings = _settings(bitrix_retry_base_seconds=5, bitrix_max_attempts=5)
    error = _transport_error("BITRIX_TIMEOUT", retryable=True)

    delays = [
        plan_retry(
            error, attempt=attempt, settings=settings, jitter=lambda _limit: 0
        ).delay_seconds
        for attempt in (1, 2, 3)
    ]
    assert delays == [5, 10, 20]


def test_retry_policy_respects_retry_after() -> None:
    """429 с `Retry-After` не перекрывается собственным backoff."""
    from app.modules.integrations.bitrix.retry import plan_retry

    decision = plan_retry(
        _transport_error("BITRIX_RATE_LIMITED", retryable=True, retry_after=42),
        attempt=1,
        settings=_settings(bitrix_retry_base_seconds=5),
        jitter=lambda _limit: 0,
    )
    assert decision.retry is True
    assert decision.delay_seconds == 42


def test_retry_policy_is_terminal_for_auth_failure() -> None:
    from app.modules.integrations.bitrix.retry import plan_retry

    decision = plan_retry(
        _transport_error("BITRIX_FAILED_AUTH", retryable=False),
        attempt=1,
        settings=_settings(),
    )
    assert decision.retry is False
    assert decision.reason == "TERMINAL_ERROR"


def test_retry_policy_stops_after_last_attempt() -> None:
    from app.modules.integrations.bitrix.retry import plan_retry

    decision = plan_retry(
        _transport_error("BITRIX_TIMEOUT", retryable=True),
        attempt=2,
        settings=_settings(bitrix_max_attempts=2),
    )
    assert decision.retry is False
    assert decision.reason == "ATTEMPTS_EXHAUSTED"


@pytest.mark.parametrize(
    ("file_name", "role"),
    [
        ("Техническое задание.xlsx", "REQUIREMENT"),
        ("Спецификация №4.docx", "REQUIREMENT"),
        ("Паспорт насоса.pdf", "FACT"),
        ("Сертификат соответствия.pdf", "FACT"),
        ("КП поставщика.xlsx", None),
        ("scan_0012.pdf", None),
    ],
)
def test_suggest_role(file_name: str, role: str | None) -> None:
    from app.modules.integrations.bitrix.service import suggest_role

    assert suggest_role(file_name).role == role


@pytest.mark.parametrize(
    ("file_name", "kind"),
    [
        ("Техническое задание.xlsx", "REQUIREMENT"),
        ("Спецификация №4.docx", "REQUIREMENT"),
        ("Паспорт насоса.pdf", "FACT"),
        ("Сертификат соответствия.pdf", "FACT"),
        ("Прайс-лист 2026.xlsx", "PRICE"),
        ("Счёт на оплату №5.pdf", "PRICE"),
        ("Коммерческое предложение поставщика.docx", "PRICE"),
        ("Price list.xlsx", "PRICE"),
        ("scan_0012.pdf", None),
    ],
)
def test_suggest_kind(file_name: str, kind: str | None) -> None:
    from app.modules.integrations.bitrix.service import suggest_kind

    assert suggest_kind(file_name).kind == kind


def _kit(**overrides) -> str:
    from app.modules.integrations.bitrix.status import decide_kit_status

    payload = {
        "deal_linked": True,
        "files_available": False,
        "documents_imported": 0,
        "price_documents": 0,
        "price_ready": 0,
        "price_running": 0,
        "price_attention": 0,
    }
    payload.update(overrides)
    return decide_kit_status(**payload)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"deal_linked": False}, "BITRIX_NOT_LINKED"),
        ({}, "BITRIX_DEAL_FOUND"),
        ({"files_available": True}, "FILES_AVAILABLE"),
        ({"documents_imported": 1}, "READY_FOR_CALCULATION"),
        (
            {"documents_imported": 1, "price_documents": 1},
            "DOCUMENTS_IMPORTED",
        ),
        ({"price_documents": 1, "price_ready": 1}, "PRICE_READY"),
        ({"price_documents": 1, "price_running": 1}, "PRICE_IMPORT_RUNNING"),
        (
            {"price_documents": 1, "price_attention": 1},
            "PRICE_REVIEW_REQUIRED",
        ),
        (
            {"documents_imported": 1, "price_documents": 1, "price_ready": 1},
            "READY_FOR_CALCULATION",
        ),
        (
            {
                "documents_imported": 1,
                "price_documents": 2,
                "price_ready": 1,
                "price_running": 1,
            },
            "PRICE_IMPORT_RUNNING",
        ),
        (
            {
                "documents_imported": 1,
                "price_documents": 2,
                "price_ready": 1,
                "price_attention": 1,
            },
            "PRICE_REVIEW_REQUIRED",
        ),
    ],
)
def test_kit_status_progression(overrides: dict, expected: str) -> None:
    assert _kit(**overrides) == expected


def test_summary_puts_mandatory_mismatches_first() -> None:
    from app.modules.integrations.bitrix.summary import build_comment

    payload = {
        "batch": {"title": "Насосы", "itemsTotal": 1},
        "verdict": {"manager": "NON_COMPLIANT", "comment": None},
        "items": [
            {
                "positionNo": 1,
                "name": "Насос Н1",
                "rows": [
                    {
                        "parameter": "Напор",
                        "status": "NON_COMPLIANT",
                        "mandatory": False,
                        "requirementValue": "40",
                        "requirementUnit": "м",
                        "factValue": "30",
                        "factUnit": "м",
                    },
                    {
                        "parameter": "Материал корпуса",
                        "status": "NOT_FOUND_IN_FACT",
                        "mandatory": True,
                        "requirementValue": "чугун",
                    },
                    {"parameter": "Масса", "status": "COMPLIANT", "mandatory": True},
                ],
            }
        ],
    }
    comment = build_comment(payload, include_key_mismatches=True)

    assert "Не соответствует" in comment
    assert "Расхождений: 2" in comment
    mandatory = comment.index("Материал корпуса")
    optional = comment.index("Напор")
    assert mandatory < optional


def test_summary_can_omit_mismatch_details() -> None:
    from app.modules.integrations.bitrix.summary import build_comment

    payload = {
        "batch": {"title": "Насосы", "itemsTotal": 1},
        "verdict": {"manager": "PARTIAL"},
        "items": [
            {
                "positionNo": 1,
                "name": "Насос Н1",
                "rows": [{"parameter": "Напор", "status": "NON_COMPLIANT"}],
            }
        ],
    }
    comment = build_comment(payload, include_key_mismatches=False)
    assert "Расхождений: 1" in comment
    assert "Напор" not in comment


def _http_error(code: int, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError("https://portal/rest/x", code, "err", headers, None)


@pytest.mark.parametrize(
    ("code", "expected", "retryable"),
    [
        (429, "BITRIX_RATE_LIMITED", True),
        (401, "BITRIX_FAILED_AUTH", False),
        (403, "BITRIX_FAILED_AUTH", False),
        (500, "BITRIX_SERVER_ERROR", True),
        (503, "BITRIX_SERVER_ERROR", True),
        (400, "BITRIX_CONTRACT_ERROR", False),
    ],
)
def test_webhook_transport_classifies_http_errors(
    code: int, expected: str, retryable: bool
) -> None:
    """Классификация статусов портала — таблица retry policy контракта."""
    from app.modules.integrations.bitrix.webhook import UrllibTransport

    error = UrllibTransport._classify(_http_error(code), "crm.deal.get")
    assert str(error.code) == expected
    assert error.retryable is retryable


def test_webhook_transport_reads_retry_after() -> None:
    from app.modules.integrations.bitrix.webhook import UrllibTransport

    error = UrllibTransport._classify(_http_error(429, "17"), "crm.deal.list")
    assert error.retry_after_seconds == 17


def test_webhook_transport_separates_download_budget() -> None:
    """Скачивание файла и REST-вызов не делят один таймаут."""
    from app.modules.integrations.bitrix.webhook import UrllibTransport

    transport = UrllibTransport(
        _settings(
            bitrix_mode="webhook",
            bitrix_base_url="https://example.bitrix24.ru/rest/1/",
            bitrix_webhook_token="token",
            bitrix_timeout_seconds=20,
            bitrix_download_timeout_seconds=180,
        )
    )

    assert transport._timeout == 20
    assert transport._download_timeout == 180


def test_webhook_deal_url_does_not_contain_token() -> None:
    """Ссылка на сделку строится от портала, а не от пути вебхука с токеном."""
    from app.modules.integrations.bitrix.webhook import WebhookBitrixClient

    settings = _settings(
        bitrix_mode="webhook",
        bitrix_base_url="https://example.bitrix24.ru/rest/1/",
        bitrix_webhook_token="super-secret-token",
    )
    client = WebhookBitrixClient(settings, transport=None)  # type: ignore[arg-type]
    url = client._deal_url("9876")

    assert url == "https://portal.bitrix24.ru/crm/deal/details/9876/"
    assert "super-secret-token" not in url


class _StubTransport:
    """Транспорт, отвечающий заранее заданными результатами методов."""

    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, params: dict):
        self.calls.append((method, params))
        return self._responses.get(method)

    async def fetch(self, url: str, limit: int) -> bytes:
        raise AssertionError("не должно вызываться")


def _webhook_client(responses: dict[str, object], **overrides):
    from app.modules.integrations.bitrix.webhook import WebhookBitrixClient

    settings = _settings(
        bitrix_mode="webhook",
        bitrix_base_url="https://example.bitrix24.ru/rest/1/",
        bitrix_webhook_token="token",
        **overrides,
    )
    return WebhookBitrixClient(settings, _StubTransport(responses))


def test_webhook_rejects_deal_that_does_not_carry_the_tender_number() -> None:
    """Фильтр по несуществующему полю портал молча игнорирует.

    `crm.deal.list` с фильтром по отсутствующему полю возвращает первую
    попавшуюся сделку. Без сверки значения опечатка в `BITRIX_TENDER_FIELD`
    привязала бы заявку к чужой сделке — и отчёт уехал бы не туда.
    """
    from app.modules.integrations.bitrix.errors import BitrixTransportError

    client = _webhook_client(
        {"crm.deal.list": [{"ID": "2", "TITLE": "Чужая сделка"}]},
        bitrix_tender_field="UF_CRM_MISSING",
    )

    with pytest.raises(BitrixTransportError) as raised:
        asyncio.run(client.get_deal("3856291"))

    assert str(raised.value.code) == "BITRIX_CONTRACT_ERROR"
    assert raised.value.retryable is False
    assert "BITRIX_TENDER_FIELD" in raised.value.message


def test_webhook_accepts_deal_with_matching_tender_number() -> None:
    client = _webhook_client(
        {
            "crm.deal.list": [
                {
                    "ID": "1312",
                    "TITLE": "Закупка № 3856291",
                    "UF_CRM_1786556378864": "3856291",
                    "STAGE_ID": "NEW",
                }
            ]
        },
        bitrix_tender_field="UF_CRM_1786556378864",
    )

    deal = asyncio.run(client.get_deal("3856291"))

    assert deal.deal_id == "1312"
    assert deal.stage == "NEW"
    assert deal.external_url == "https://portal.bitrix24.ru/crm/deal/details/1312/"


def test_webhook_by_deal_id_needs_no_field_match() -> None:
    """Режим `ID`: сверять нечего, идентификатор и есть ключ поиска."""
    client = _webhook_client(
        {"crm.deal.get": {"ID": "1312", "TITLE": "Закупка"}},
        bitrix_tender_field="ID",
    )

    assert asyncio.run(client.get_deal("1312")).deal_id == "1312"


def test_webhook_refuses_ambiguous_tender_number() -> None:
    """Две сделки с одним номером — отказ, а не случайный выбор."""
    from app.modules.integrations.bitrix.errors import BitrixTransportError

    client = _webhook_client(
        {
            "crm.deal.list": [
                {"ID": "2", "UF_X": "3856291"},
                {"ID": "1312", "UF_X": "3856291"},
            ]
        },
        bitrix_tender_field="UF_X",
    )

    with pytest.raises(BitrixTransportError) as raised:
        asyncio.run(client.get_deal("3856291"))

    assert "найдено сделок: 2" in raised.value.message
    assert raised.value.retryable is False


def test_webhook_reads_timeline_attachments() -> None:
    """Файлы письма Фаза 1 кладёт в комментарий ленты, а не в поле сделки.

    Форма ответа снята с живого портала: `FILES` — словарь, ключ которого
    совпадает с идентификатором файла, а имя и размер лежат внутри.
    """
    transport = _StubTransport(
        {
            "crm.deal.list": [{"ID": "1312", "UF_X": "3856291"}],
            "crm.timeline.comment.list": [
                {
                    "ID": "5148",
                    "COMMENT": "Вложения письма: 2 файл(ов)",
                    "FILES": {
                        "276": {"id": 276, "name": "Опросный лист.pdf", "size": 446287},
                        "278": {"id": 278, "name": "Паспорт.pdf", "size": 1024},
                    },
                },
                # Тот же файл во втором комментарии не должен задвоиться.
                {"ID": "5150", "COMMENT": "повтор", "FILES": {"276": {"id": 276}}},
            ],
        }
    )
    from app.modules.integrations.bitrix.webhook import WebhookBitrixClient

    settings = _settings(
        bitrix_mode="webhook",
        bitrix_base_url="https://example.bitrix24.ru/rest/1/",
        bitrix_webhook_token="token",
        bitrix_tender_field="UF_X",
    )
    files = asyncio.run(
        WebhookBitrixClient(settings, transport).list_files("3856291")
    ).files

    assert [file.name for file in files] == ["Опросный лист.pdf", "Паспорт.pdf"]
    assert {file.source for file in files} == {"TIMELINE"}
    assert files[0].external_file_id == "276"
    assert files[0].size_bytes == 446287
    # Имя пришло в самом комментарии — лишнего запроса за метаданными нет.
    assert [method for method, _ in transport.calls].count("disk.file.get") == 0


def test_webhook_reads_metadata_for_bare_file_ids() -> None:
    """Голый идентификатор в `FILES` дочитывается через `disk.file.get`."""
    client = _webhook_client(
        {
            "crm.deal.list": [{"ID": "1312", "UF_X": "3856291"}],
            "crm.timeline.comment.list": [{"FILES": ["260"]}],
            "disk.file.get": {"ID": "260", "NAME": "Паспорт.pdf", "SIZE": "10"},
        },
        bitrix_tender_field="UF_X",
    )

    files = asyncio.run(client.list_files("3856291")).files

    assert [file.name for file in files] == ["Паспорт.pdf"]
    assert files[0].size_bytes == 10


def test_webhook_returns_timeline_notes() -> None:
    """Текст ленты доезжает до менеджера: там перечислены неприложенные файлы."""
    client = _webhook_client(
        {
            "crm.deal.list": [{"ID": "1312", "UF_X": "3856291"}],
            "crm.timeline.comment.list": [
                {
                    "COMMENT": "Вложения письма: 1 файл(ов). "
                    "Не приложены (превышен размер): Каталог.pdf",
                    "FILES": {"260": {"id": 260, "name": "ТЗ.pdf", "size": 10}},
                }
            ],
        },
        bitrix_tender_field="UF_X",
    )

    page = asyncio.run(client.list_files("3856291"))

    assert len(page.files) == 1
    assert "Каталог.pdf" in page.notes[0]


def test_webhook_without_any_file_source_explains_itself() -> None:
    """Все источники выключены — понятная ошибка, а не пустой список."""
    from app.modules.integrations.bitrix.errors import BitrixTransportError

    client = _webhook_client(
        {"crm.deal.list": [{"ID": "1312", "UF_X": "3856291"}]},
        bitrix_tender_field="UF_X",
        bitrix_read_timeline_files=False,
    )

    with pytest.raises(BitrixTransportError) as raised:
        asyncio.run(client.list_files("3856291"))

    assert "bitrix_read_timeline_files" in raised.value.message


def test_fake_client_serves_sample_deal() -> None:
    from app.modules.integrations.bitrix.fake import FakeBitrixClient

    client = FakeBitrixClient.with_sample_deal()
    deal = asyncio.run(client.get_deal(TENDER_ID))
    page = asyncio.run(client.list_files(TENDER_ID))

    assert deal.deal_id == DEAL_ID
    assert {f.external_file_id for f in page.files} == {
        TZ_FILE,
        PASSPORT_FILE,
        PRICE_FILE,
    }


# --- часть с БД ------------------------------------------------------------


@pytest.fixture
def storage(client):
    """Хранилище документов в памяти — как в тестах загрузки."""
    from app.main import app
    from app.modules.documents.router import get_storage
    from app.modules.documents.storage import InMemoryStorage

    fake = InMemoryStorage()
    app.dependency_overrides[get_storage] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_storage, None)


@pytest.fixture(autouse=True)
def clean_integration_tables():
    """Пустые `external_links` и `integration_jobs` вокруг каждого теста.

    `clean_users` их не задевает: `external_links.entity_id` не является
    внешним ключом, а у задания ссылка на заявку объявлена `ON DELETE SET
    NULL`. Без явной очистки связь файла с документом из предыдущего теста
    делала бы файл «уже импортированным» навсегда.
    """
    from sqlalchemy import create_engine, text

    from tests.conftest import TEST_DATABASE_URL

    if not TEST_DATABASE_URL:
        yield
        return

    engine = create_engine(TEST_DATABASE_URL.replace("+asyncpg", "+psycopg2"), future=True)

    def wipe() -> None:
        with engine.begin() as connection:
            connection.execute(text("TRUNCATE external_links, integration_jobs CASCADE"))

    wipe()
    yield
    wipe()
    engine.dispose()


@pytest.fixture
def bitrix(client):
    """Стенд портала, подключённый вместо реального клиента."""
    from app.main import app
    from app.modules.integrations.bitrix.fake import FakeBitrixClient
    from app.modules.integrations.bitrix.router import get_bitrix_client

    fake = FakeBitrixClient.with_sample_deal()
    app.dependency_overrides[get_bitrix_client] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_bitrix_client, None)


def _auth(client, credentials: dict = MANAGER) -> dict[str, str]:
    token = client.post("/api/v1/auth/login", json=credentials).json()["accessToken"]
    return {"Authorization": f"Bearer {token}"}


def _idempotent(headers: dict[str, str], key: str | None = None) -> dict[str, str]:
    return {**headers, "Idempotency-Key": key or str(uuid.uuid4())}


def _batch(client, headers: dict[str, str], *, tender_id: str | None = TENDER_ID):
    payload = {
        "title": "Проверка насосов",
        "customer": "АО Заказчик",
        "items": [{"name": "Насос Н1"}],
    }
    if tender_id is not None:
        payload["tenderId"] = tender_id
    response = client.post(BATCHES, json=payload, headers=_idempotent(headers))
    assert response.status_code == 201, response.text
    return response.json()


def _import(client, headers, batch, *, files=None, key=None):
    body = {
        "checkBatchId": batch["id"],
        "files": files
        or [
            {
                "externalFileId": TZ_FILE,
                "checkItemId": batch["items"][0]["id"],
                "role": "REQUIREMENT",
            }
        ],
    }
    return client.post(
        f"{BITRIX}/deals/{TENDER_ID}/imports",
        json=body,
        headers=_idempotent(headers, key),
    )


def _in_own_loop(operation):
    """Выполняет корутину на собственном движке БД.

    Общий `app.db.engine` использовать нельзя: `TestClient` держит свой event
    loop, а соединения asyncpg привязаны к тому loop, в котором созданы —
    обращение к ним из `asyncio.run` даёт «attached to a different loop».
    Воркер в проде работает в отдельном процессе, поэтому отдельный движок
    здесь ближе к реальности, чем переиспользование пула приложения.
    """

    async def execute():
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from tests.conftest import TEST_DATABASE_URL

        engine = create_async_engine(TEST_DATABASE_URL)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with session_factory() as session:
                return await operation(session)
        finally:
            await engine.dispose()

    return asyncio.run(execute())


def _run_job(job_id: str, bitrix, storage, *, settings=None):
    """Исполняет задание тем же кодом, что и воркер."""

    async def operation(session):
        from app.config import get_settings
        from app.modules.integrations.bitrix.executor import BitrixJobExecutor

        executor = BitrixJobExecutor(
            session, settings or get_settings(), storage, bitrix
        )
        return await executor.execute(uuid.UUID(job_id))

    return _in_own_loop(operation)


@requires_db
def test_deal_card_is_normalized(client, bitrix) -> None:
    headers = _auth(client)
    response = client.get(f"{BITRIX}/deals/{TENDER_ID}", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenderId"] == TENDER_ID
    assert body["bitrixDealId"] == DEAL_ID
    assert body["customer"] == "АО Заказчик"
    # Raw payload портала наружу не протекает.
    assert "TITLE" not in body and "STAGE_ID" not in body


@requires_db
def test_deal_not_found(client, bitrix) -> None:
    response = client.get(f"{BITRIX}/deals/00000", headers=_auth(client))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "BITRIX_DEAL_NOT_FOUND"


@requires_db
def test_files_carry_suggested_role(client, bitrix) -> None:
    response = client.get(f"{BITRIX}/deals/{TENDER_ID}/files", headers=_auth(client))

    assert response.status_code == 200, response.text
    items = {item["externalFileId"]: item for item in response.json()["items"]}
    assert items[TZ_FILE]["suggestedRole"] == "REQUIREMENT"
    assert items[PASSPORT_FILE]["suggestedRole"] == "FACT"
    assert items[TZ_FILE]["alreadyImportedDocumentId"] is None
    assert items[TZ_FILE]["suggestedKind"] == "REQUIREMENT"
    assert items[TZ_FILE]["kindConfidence"] == "95.00"
    assert items[PRICE_FILE]["suggestedKind"] == "PRICE"
    assert items[PRICE_FILE]["suggestedRole"] is None
    assert items[PRICE_FILE]["alreadyImported"] is False


@requires_db
def test_disabled_integration_answers_503(client) -> None:
    """Без конфигурации маршруты отвечают 503, а не падают."""
    from app.main import app
    from app.modules.integrations.bitrix.router import get_bitrix_client

    app.dependency_overrides[get_bitrix_client] = lambda: None
    try:
        response = client.get(f"{BITRIX}/deals/{TENDER_ID}", headers=_auth(client))
    finally:
        app.dependency_overrides.pop(get_bitrix_client, None)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BITRIX_NOT_CONFIGURED"


@requires_db
def test_health_hides_credentials(client, bitrix) -> None:
    from app.config import Settings, get_settings
    from app.main import app

    secret = "super-secret-webhook-token"
    app.dependency_overrides[get_settings] = lambda: Settings(
        bitrix_mode="webhook",
        bitrix_base_url="https://example.bitrix24.ru/rest/1/",
        bitrix_webhook_token=secret,
    )
    try:
        response = client.get(f"{BITRIX}/health", headers=_auth(client))
    finally:
        app.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "OK"
    assert secret not in response.text
    assert "portal.bitrix24.ru" not in response.text


@requires_db
def test_health_reports_transport_failure(client, bitrix) -> None:
    from app.modules.integrations.bitrix.errors import failed_auth

    bitrix.fail_next("ping", failed_auth())
    response = client.get(f"{BITRIX}/health", headers=_auth(client))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ERROR"
    assert body["errorCode"] == "BITRIX_FAILED_AUTH"


@requires_db
def test_rate_limited_lookup_maps_to_service_unavailable(client, bitrix) -> None:
    from app.modules.integrations.bitrix.errors import rate_limited

    bitrix.fail_next("get_deal", rate_limited(30))
    response = client.get(f"{BITRIX}/deals/{TENDER_ID}", headers=_auth(client))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BITRIX_RATE_LIMITED"


@requires_db
def test_unauthorized_lookup_maps_to_bad_gateway(client, bitrix) -> None:
    from app.modules.integrations.bitrix.errors import failed_auth

    bitrix.fail_next("get_deal", failed_auth())
    response = client.get(f"{BITRIX}/deals/{TENDER_ID}", headers=_auth(client))

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "BITRIX_FAILED_AUTH"


@requires_db
def test_import_requires_idempotency_key(client, bitrix) -> None:
    headers = _auth(client)
    batch = _batch(client, headers)
    response = client.post(
        f"{BITRIX}/deals/{TENDER_ID}/imports",
        json={
            "checkBatchId": batch["id"],
            "files": [
                {
                    "externalFileId": TZ_FILE,
                    "checkItemId": batch["items"][0]["id"],
                    "role": "REQUIREMENT",
                }
            ],
        },
        headers=headers,
    )

    assert response.status_code == 428
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


@requires_db
def test_import_creates_document_with_bitrix_source(client, bitrix, storage) -> None:
    headers = _auth(client)
    batch = _batch(client, headers)

    accepted = _import(client, headers, batch)
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["jobId"]
    assert accepted.json()["status"] == "QUEUED"
    assert accepted.json()["filesTotal"] == 1

    outcome = _run_job(job_id, bitrix, storage)
    assert outcome.status == "COMPLETED", outcome.error

    job = client.get(f"{BITRIX}/imports/{job_id}", headers=headers).json()
    assert job["status"] == "COMPLETED"
    assert job["result"]["documents"][TZ_FILE]

    documents = client.get(
        f"/api/v1/check-items/{batch['items'][0]['id']}/documents", headers=headers
    ).json()
    assert len(documents["requirement"]) == 1
    assert documents["requirement"][0]["fileName"] == "Техническое задание.pdf"

    files = client.get(f"{BITRIX}/deals/{TENDER_ID}/files", headers=headers).json()
    imported = {item["externalFileId"]: item for item in files["items"]}
    assert imported[TZ_FILE]["alreadyImportedDocumentId"] is not None


@requires_db
def test_import_marks_document_source_bitrix(client, bitrix, storage, db) -> None:
    """Провенанс: импортированный документ отличим от ручной загрузки."""
    headers = _auth(client)
    batch = _batch(client, headers)
    _run_job(_import(client, headers, batch).json()["jobId"], bitrix, storage)

    rows = db.execute(
        text(
            "SELECT d.source FROM documents d"
            " JOIN check_items i ON i.id = d.check_item_id"
            " WHERE i.batch_id = :batch_id"
        ),
        {"batch_id": batch["id"]},
    )
    assert list(rows.scalars().all()) == ["BITRIX"]


@requires_db
def test_import_links_batch_to_deal(client, bitrix, storage, db) -> None:
    headers = _auth(client)
    batch = _batch(client, headers)
    _import(client, headers, batch)

    linked = db.execute(
        text(
            "SELECT external_id FROM external_links"
            " WHERE entity_type = 'check_batch' AND entity_id = :batch_id"
            "   AND external_entity_type = 'deal'"
        ),
        {"batch_id": batch["id"]},
    ).scalar()
    assert linked == DEAL_ID


@requires_db
def test_import_repeat_with_same_key_returns_same_job(client, bitrix) -> None:
    headers = _auth(client)
    batch = _batch(client, headers)
    key = str(uuid.uuid4())

    first = _import(client, headers, batch, key=key)
    second = _import(client, headers, batch, key=key)

    assert first.status_code == second.status_code == 202
    assert first.json()["jobId"] == second.json()["jobId"]


@requires_db
def test_import_of_already_imported_file_conflicts(client, bitrix, storage) -> None:
    headers = _auth(client)
    batch = _batch(client, headers)
    _run_job(_import(client, headers, batch).json()["jobId"], bitrix, storage)

    repeated = _import(client, headers, batch)

    assert repeated.status_code == 409
    assert repeated.json()["error"]["code"] == "FILE_ALREADY_IMPORTED"


@requires_db
def test_import_rejects_item_of_another_batch(client, bitrix) -> None:
    headers = _auth(client)
    batch = _batch(client, headers)
    other = _batch(client, headers, tender_id=None)

    response = _import(
        client,
        headers,
        batch,
        files=[
            {
                "externalFileId": TZ_FILE,
                "checkItemId": other["items"][0]["id"],
                "role": "REQUIREMENT",
            }
        ],
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "BITRIX_FILE_MAPPING_REQUIRED"


@requires_db
def test_import_rejects_unknown_file(client, bitrix) -> None:
    headers = _auth(client)
    batch = _batch(client, headers)

    response = _import(
        client,
        headers,
        batch,
        files=[
            {
                "externalFileId": "disk-file-missing",
                "checkItemId": batch["items"][0]["id"],
                "role": "REQUIREMENT",
            }
        ],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "BITRIX_FILE_NOT_FOUND"


@requires_db
def test_import_denied_for_viewer(client, bitrix) -> None:
    manager = _auth(client)
    batch = _batch(client, manager)

    response = _import(client, _auth(client, VIEWER), batch)

    assert response.status_code == 403


@requires_db
def test_import_applies_upload_security_checks(client, bitrix, storage) -> None:
    """Файл из сделки проходит тот же контроль, что и ручная загрузка."""
    from app.modules.integrations.bitrix.port import BitrixFile

    bitrix.add_file(
        TENDER_ID,
        BitrixFile(
            external_file_id="disk-file-fake-pdf",
            name="Подделка.pdf",
            media_type="application/pdf",
        ),
        content=b"not a pdf at all",
    )
    headers = _auth(client)
    batch = _batch(client, headers)
    accepted = _import(
        client,
        headers,
        batch,
        files=[
            {
                "externalFileId": "disk-file-fake-pdf",
                "checkItemId": batch["items"][0]["id"],
                "role": "FACT",
            }
        ],
    )

    outcome = _run_job(accepted.json()["jobId"], bitrix, storage)

    assert outcome.status == "FAILED"
    assert outcome.error["retryable"] is False
    documents = client.get(
        f"/api/v1/check-items/{batch['items'][0]['id']}/documents", headers=headers
    ).json()
    assert documents["fact"] == []


@requires_db
def test_timeout_schedules_retry(client, bitrix, storage) -> None:
    from app.modules.integrations.bitrix.errors import timeout

    headers = _auth(client)
    batch = _batch(client, headers)
    job_id = _import(client, headers, batch).json()["jobId"]
    bitrix.fail_next("download_file", timeout())

    outcome = _run_job(job_id, bitrix, storage)

    assert outcome.status == "RETRY_SCHEDULED"
    job = client.get(f"{BITRIX}/imports/{job_id}", headers=headers).json()
    assert job["attempt"] == 1
    assert job["error"]["code"] == "BITRIX_TIMEOUT"
    assert job["error"]["retryable"] is True
    assert job["nextRetryAt"] is not None


@requires_db
def test_retry_reruns_scheduled_job(client, bitrix, storage) -> None:
    from app.modules.integrations.bitrix.errors import timeout

    headers = _auth(client)
    batch = _batch(client, headers)
    job_id = _import(client, headers, batch).json()["jobId"]
    bitrix.fail_next("download_file", timeout())
    _run_job(job_id, bitrix, storage)

    retried = client.post(f"{BITRIX}/publications/{job_id}/retry", headers=headers)
    assert retried.status_code == 202, retried.text
    assert retried.json()["status"] == "QUEUED"

    outcome = _run_job(job_id, bitrix, storage)
    assert outcome.status == "COMPLETED"
    assert outcome.attempt == 2


@requires_db
def test_auth_failure_is_terminal_and_not_retryable(client, bitrix, storage) -> None:
    from app.modules.integrations.bitrix.errors import failed_auth

    headers = _auth(client)
    batch = _batch(client, headers)
    job_id = _import(client, headers, batch).json()["jobId"]
    bitrix.fail_next("download_file", failed_auth())

    outcome = _run_job(job_id, bitrix, storage)
    assert outcome.status == "FAILED"

    refused = client.post(f"{BITRIX}/publications/{job_id}/retry", headers=headers)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "BITRIX_JOB_NOT_RETRYABLE"


@requires_db
def test_attempts_are_exhausted(client, bitrix, storage) -> None:
    from app.config import get_settings
    from app.modules.integrations.bitrix.errors import timeout

    headers = _auth(client)
    batch = _batch(client, headers)
    job_id = _import(client, headers, batch).json()["jobId"]
    limited = get_settings().model_copy(update={"bitrix_max_attempts": 1})

    bitrix.fail_next("download_file", timeout())
    outcome = _run_job(job_id, bitrix, storage, settings=limited)

    assert outcome.status == "FAILED"
    assert outcome.error["reason"] == "ATTEMPTS_EXHAUSTED"


@requires_db
def test_completed_job_ignores_redelivery(client, bitrix, storage) -> None:
    headers = _auth(client)
    batch = _batch(client, headers)
    job_id = _import(client, headers, batch).json()["jobId"]
    _run_job(job_id, bitrix, storage)

    repeated = _run_job(job_id, bitrix, storage)

    assert repeated.skipped is True
    assert repeated.status == "COMPLETED"


# --- публикация отчёта ------------------------------------------------------


def _approve_batch(db, batch_id: str) -> None:
    db.execute(
        text(
            "UPDATE check_batches"
            "   SET manager_verdict = 'COMPLIANT', final_verdict = 'COMPLIANT',"
            "       verdict_approved_at = now()"
            " WHERE id = :batch_id"
        ),
        {"batch_id": batch_id},
    )
    db.commit()


def _report(db, storage, batch_id: str, *, file_name: str = "Отчёт.xlsx") -> str:
    """Готовый отчёт заявки: snapshot, артефакт и файл в хранилище."""
    snapshot_id = str(uuid.uuid4())
    artifact_id = str(uuid.uuid4())
    storage_key = f"reports/{artifact_id}.xlsx"
    payload = {
        "batch": {"id": batch_id, "title": "Проверка насосов", "itemsTotal": 1},
        "verdict": {"manager": "COMPLIANT", "computed": "COMPLIANT"},
        "items": [
            {
                "positionNo": 1,
                "name": "Насос Н1",
                "rows": [
                    {
                        "parameter": "Напор",
                        "status": "NON_COMPLIANT",
                        "mandatory": True,
                        "requirementValue": "40",
                        "requirementUnit": "м",
                        "factValue": "30",
                        "factUnit": "м",
                    }
                ],
            }
        ],
    }
    db.execute(
        text(
            "INSERT INTO report_snapshots"
            " (id, check_batch_id, status, payload, checksum, language)"
            " VALUES (:id, :batch_id, 'COMPLETED', CAST(:payload AS jsonb), :sum, 'ru')"
        ),
        {
            "id": snapshot_id,
            "batch_id": batch_id,
            "payload": json.dumps(payload, ensure_ascii=False),
            "sum": "checksum",
        },
    )
    db.execute(
        text(
            "INSERT INTO report_artifacts"
            " (id, report_snapshot_id, format, file_name, storage_key, size_bytes)"
            " VALUES (:id, :snapshot_id, 'XLSX', :file_name, :key, 10)"
        ),
        {
            "id": artifact_id,
            "snapshot_id": snapshot_id,
            "file_name": file_name,
            "key": storage_key,
        },
    )
    db.commit()
    from app.config import get_settings

    asyncio.run(
        storage.put(
            get_settings().upload_bucket,
            storage_key,
            b"xlsx-bytes",
            content_type=XLSX_MEDIA_TYPE,
        )
    )
    return artifact_id


def _publish(client, headers, batch_id: str, artifact_ids: list[str], **extra):
    return client.post(
        f"{BITRIX}/check-batches/{batch_id}/publish",
        json={"reportArtifactIds": artifact_ids, **extra},
        headers=_idempotent(headers),
    )


@requires_db
def test_publish_requires_approved_verdict(client, bitrix, storage, db) -> None:
    headers = _auth(client)
    batch = _batch(client, headers)
    _import(client, headers, batch)
    artifact_id = _report(db, storage, batch["id"])

    response = _publish(client, headers, batch["id"], [artifact_id])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REPORT_NOT_READY"


@requires_db
def test_publish_requires_linked_deal(client, bitrix, storage, db) -> None:
    """Заявка без связи со сделкой публиковаться не может — публиковать некуда."""
    headers = _auth(client)
    batch = _batch(client, headers)
    _approve_batch(db, batch["id"])
    artifact_id = _report(db, storage, batch["id"])

    response = _publish(client, headers, batch["id"], [artifact_id])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "BITRIX_BATCH_NOT_LINKED"


@requires_db
def test_publish_rejects_unknown_artifact(client, bitrix, storage, db) -> None:
    headers = _auth(client)
    batch = _batch(client, headers)
    _import(client, headers, batch)
    _approve_batch(db, batch["id"])

    response = _publish(client, headers, batch["id"], [str(uuid.uuid4())])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REPORT_NOT_READY"


@requires_db
def test_publish_uploads_report_and_writes_comment(client, bitrix, storage, db) -> None:
    headers = _auth(client)
    batch = _batch(client, headers)
    _import(client, headers, batch)
    _approve_batch(db, batch["id"])
    artifact_id = _report(db, storage, batch["id"])

    accepted = _publish(client, headers, batch["id"], [artifact_id])
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["tenderId"] == TENDER_ID

    job_id = accepted.json()["jobId"]
    outcome = _run_job(job_id, bitrix, storage)
    assert outcome.status == "COMPLETED", outcome.error

    published = client.get(f"{BITRIX}/publications/{job_id}", headers=headers).json()
    assert published["result"]["files"][artifact_id]
    assert published["result"]["commentId"]
    assert len(bitrix.comments) == 1
    assert "Напор" in bitrix.comments[0][1]


@requires_db
def test_publish_of_same_artifact_conflicts(client, bitrix, storage, db) -> None:
    headers = _auth(client)
    batch = _batch(client, headers)
    _import(client, headers, batch)
    _approve_batch(db, batch["id"])
    artifact_id = _report(db, storage, batch["id"])
    _run_job(
        _publish(client, headers, batch["id"], [artifact_id]).json()["jobId"],
        bitrix,
        storage,
    )

    repeated = _publish(client, headers, batch["id"], [artifact_id])

    assert repeated.status_code == 409
    assert repeated.json()["error"]["code"] == "REPORT_ALREADY_PUBLISHED"


@requires_db
def test_second_publication_does_not_duplicate_comment(
    client, bitrix, storage, db
) -> None:
    """Повторная публикация не создаёт второй комментарий в таймлайне."""
    headers = _auth(client)
    batch = _batch(client, headers)
    _import(client, headers, batch)
    _approve_batch(db, batch["id"])
    first = _report(db, storage, batch["id"], file_name="Отчёт-1.xlsx")
    second = _report(db, storage, batch["id"], file_name="Отчёт-2.xlsx")

    _run_job(
        _publish(client, headers, batch["id"], [first]).json()["jobId"], bitrix, storage
    )
    _run_job(
        _publish(client, headers, batch["id"], [second]).json()["jobId"], bitrix, storage
    )

    assert len(bitrix.comments) == 1


@requires_db
def test_local_report_survives_failed_publication(client, bitrix, storage, db) -> None:
    """Провал публикации не трогает локальный артефакт."""
    from app.modules.integrations.bitrix.errors import failed_auth

    headers = _auth(client)
    batch = _batch(client, headers)
    _import(client, headers, batch)
    _approve_batch(db, batch["id"])
    artifact_id = _report(db, storage, batch["id"])
    job_id = _publish(client, headers, batch["id"], [artifact_id]).json()["jobId"]
    bitrix.fail_next("publish_report", failed_auth())

    outcome = _run_job(job_id, bitrix, storage)
    assert outcome.status == "FAILED"

    download = client.get(
        f"/api/v1/report-artifacts/{artifact_id}/download", headers=headers
    )
    assert download.status_code == 200
    assert download.content == b"xlsx-bytes"


@requires_db
def test_job_not_found(client, bitrix) -> None:
    response = client.get(f"{BITRIX}/imports/{uuid.uuid4()}", headers=_auth(client))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "BITRIX_JOB_NOT_FOUND"


@requires_db
def test_due_jobs_returns_only_matured_retries(client, bitrix, storage, db) -> None:
    """Диспетчер повторов не забирает задания раньше `next_retry_at`."""
    from app.modules.integrations.bitrix.errors import timeout

    headers = _auth(client)
    batch = _batch(client, headers)
    job_id = _import(client, headers, batch).json()["jobId"]
    bitrix.fail_next("download_file", timeout())
    _run_job(job_id, bitrix, storage)

    assert _due_job_ids() == []

    db.execute(
        text(
            "UPDATE integration_jobs SET next_retry_at = :moment WHERE id = :job_id"
        ),
        {"moment": datetime.now(UTC) - timedelta(seconds=1), "job_id": job_id},
    )
    db.commit()

    assert _due_job_ids() == [job_id]


def _due_job_ids() -> list[str]:
    async def collect(session):
        from app.modules.integrations.bitrix.executor import due_jobs

        return [str(job_id) for job_id, _attempt in await due_jobs(session)]

    return _in_own_loop(collect)


# --- ценовые документы сделки (этап 7) -------------------------------------
#
# Комплект заявки собирается из сделки в два контура: документы Ф3
# (`REQUIREMENT`/`FACT`) и ценовые документы Ф2 (`PRICE_LIST`/`SUPPLIER_QUOTE`).
# Ценовой файл проходит тем же parser pipeline Phase 2, что и ручная загрузка,
# но разовый счёт/КП остаётся внутри своей заявки и не становится общим
# активным прайсом.


@requires_db
def test_prepare_deal_is_idempotent(client, bitrix) -> None:
    """Повторная подготовка по тому же tenderId не создаёт вторую заявку."""
    headers = _auth(client)
    body = {"items": [{"positionNo": 1, "name": "Насос Н1"}]}

    first = client.post(
        f"{BITRIX}/deals/{TENDER_ID}/prepare",
        json=body,
        headers=_idempotent(headers),
    )
    second = client.post(
        f"{BITRIX}/deals/{TENDER_ID}/prepare",
        json=body,
        headers=_idempotent(headers),
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert second.json()["checkBatchId"] == first.json()["checkBatchId"]


@requires_db
def test_prepare_missing_deal_does_not_create_tender_ref(client, bitrix, db) -> None:
    """Неизвестный tenderId не создаёт частичную внешнюю связь."""
    headers = _auth(client)
    before = db.execute(text("SELECT count(*) FROM tender_refs")).scalar()
    response = client.post(
        f"{BITRIX}/deals/00000/prepare",
        json={"items": [{"positionNo": 1, "name": "Насос Н1"}]},
        headers=_idempotent(headers),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "BITRIX_DEAL_NOT_FOUND"
    assert db.execute(text("SELECT count(*) FROM tender_refs")).scalar() == before


@requires_db
def test_prepare_existing_batch_returns_files_and_keeps_batch(client, bitrix) -> None:
    """Локальная заявка связывается с найденной сделкой без нового batch."""
    headers = _auth(client)
    batch = _batch(client, headers, tender_id=None)
    response = client.post(
        f"{BITRIX}/deals/{TENDER_ID}/prepare",
        json={"checkBatchId": batch["id"], "items": []},
        headers=_idempotent(headers),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created"] is False
    assert body["checkBatchId"] == batch["id"]
    assert {item["externalFileId"] for item in body["files"]} >= {
        TZ_FILE,
        PASSPORT_FILE,
        PRICE_FILE,
    }


@requires_db
def test_local_batch_rejects_duplicate_tender_link(client, bitrix) -> None:
    """Обычный локальный endpoint не создаёт второй batch для tenderId."""
    headers = _auth(client)
    first = _batch(client, headers)
    second = client.post(
        BATCHES,
        json={
            "title": "Повторная заявка",
            "tenderId": TENDER_ID,
            "items": [{"name": "Насос Н2"}],
        },
        headers=_idempotent(headers),
    )

    assert first["tenderId"] == TENDER_ID
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "TENDER_ALREADY_LINKED"


ADMIN = {"email": "admin@docprice.internal", "password": "admin"}


def _supplier(client, *, name: str = "Поставщик прайса") -> dict:
    """Заводит поставщика: запись в каталог доступна только администратору."""
    code = f"prc-{uuid.uuid4().hex[:8]}"
    response = client.post(
        "/api/v1/suppliers",
        json={"code": code, "name": name},
        headers=_auth(client, ADMIN),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _import_price(
    client,
    headers,
    batch,
    *,
    supplier_id: str | None,
    document_type: str | None = "PRICE_LIST",
    price_file: str = PRICE_FILE,
    key: str | None = None,
):
    file: dict = {"externalFileId": price_file, "kind": "PRICE"}
    if document_type is not None:
        file["priceDocumentType"] = document_type
    if supplier_id is not None:
        file["supplierId"] = supplier_id
    return _import(client, headers, batch, files=[file], key=key)


def _run_price_job(job_id, bitrix, storage, monkeypatch):
    """Исполняет задание, не отправляя разбор в реальную очередь Celery."""
    from app.tasks import run_price_import

    published: list[str] = []
    monkeypatch.setattr(
        run_price_import, "delay", lambda import_id: published.append(import_id)
    )
    return _run_job(job_id, bitrix, storage), published


@requires_db
def test_import_price_list_uses_phase2_pipeline(
    client, bitrix, storage, db, monkeypatch
) -> None:
    """Прайс из сделки регистрируется как обычный ценовой документ Phase 2."""
    headers = _auth(client)
    batch = _batch(client, headers)
    supplier = _supplier(client)
    accepted = _import_price(client, headers, batch, supplier_id=supplier["id"])
    assert accepted.status_code == 202, accepted.text

    outcome, published = _run_price_job(
        accepted.json()["jobId"], bitrix, storage, monkeypatch
    )

    assert outcome.status == "COMPLETED", outcome.error
    # Файл уходит в тот же разбор, что и локальная загрузка.
    assert len(published) == 1
    row = db.execute(
        text(
            "SELECT pf.source, pv.document_type, pv.check_batch_id, pv.status"
            "  FROM price_files pf"
            "  JOIN price_versions pv ON pv.price_file_id = pf.id"
            " WHERE pf.supplier_id = :supplier_id"
        ),
        {"supplier_id": supplier["id"]},
    ).one()
    assert row.source == "BITRIX"
    assert row.document_type == "PRICE_LIST"
    # Общий прайс глобален; активным его делает только успешный разбор.
    assert row.check_batch_id is None
    assert row.status == "uploaded"

    price = outcome.result["prices"][PRICE_FILE]
    assert price["documentType"] == "PRICE_LIST"
    assert price["checkBatchId"] is None
    assert price["supplierName"] == "Поставщик прайса"


@requires_db
def test_supplier_quote_stays_inside_its_batch(
    client, bitrix, storage, db, monkeypatch
) -> None:
    """Разовый счёт/КП привязан к своей заявке и не виден в чужой."""
    headers = _auth(client)
    batch = _batch(client, headers)
    other = _batch(client, headers, tender_id=None)
    supplier = _supplier(client, name="Поставщик счёта")
    accepted = _import_price(
        client,
        headers,
        batch,
        supplier_id=supplier["id"],
        document_type="SUPPLIER_QUOTE",
    )
    assert accepted.status_code == 202, accepted.text

    outcome, _ = _run_price_job(
        accepted.json()["jobId"], bitrix, storage, monkeypatch
    )
    assert outcome.status == "COMPLETED", outcome.error

    bound = db.execute(
        text(
            "SELECT pv.check_batch_id"
            "  FROM price_versions pv"
            "  JOIN price_files pf ON pf.id = pv.price_file_id"
            " WHERE pf.supplier_id = :supplier_id"
        ),
        {"supplier_id": supplier["id"]},
    ).scalar()
    assert str(bound) == batch["id"]

    own = client.get(f"{BITRIX}/check-batches/{batch['id']}/status", headers=headers)
    foreign = client.get(
        f"{BITRIX}/check-batches/{other['id']}/status", headers=headers
    )
    assert own.json()["priceDocuments"] == 1
    assert own.json()["priceRunning"] == 1
    assert own.json()["status"] == "PRICE_IMPORT_RUNNING"
    # В чужой заявке разовый счёт не участвует.
    assert foreign.json()["priceDocuments"] == 0


@requires_db
def test_kit_status_survives_portal_outage(client, bitrix) -> None:
    """Состояние заявки восстанавливается после перезагрузки без Bitrix24.

    Статус читается только из локальных данных, поэтому недоступный портал
    (здесь — отсутствующий клиент) не мешает показать уже собранный комплект.
    """
    from app.main import app
    from app.modules.integrations.bitrix.router import get_bitrix_client

    headers = _auth(client)
    batch = _batch(client, headers)
    _import(client, headers, batch)

    app.dependency_overrides[get_bitrix_client] = lambda: None
    try:
        response = client.get(
            f"{BITRIX}/check-batches/{batch['id']}/status", headers=headers
        )
    finally:
        app.dependency_overrides.pop(get_bitrix_client, None)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dealLinked"] is True
    assert body["tenderId"] == TENDER_ID


@requires_db
def test_import_price_requires_supplier(client, bitrix) -> None:
    """Ценовой файл без поставщика не импортируется (подсказка менеджеру)."""
    headers = _auth(client)
    batch = _batch(client, headers)

    response = _import_price(client, headers, batch, supplier_id=None)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "BITRIX_SUPPLIER_REQUIRED"


@requires_db
def test_import_price_requires_document_type(client, bitrix) -> None:
    headers = _auth(client)
    batch = _batch(client, headers)
    supplier = _supplier(client)

    response = _import_price(
        client,
        headers,
        batch,
        supplier_id=supplier["id"],
        document_type=None,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "BITRIX_PRICE_DOCUMENT_TYPE_REQUIRED"


@requires_db
def test_import_price_rejects_unknown_supplier(client, bitrix) -> None:
    headers = _auth(client)
    batch = _batch(client, headers)

    response = _import_price(client, headers, batch, supplier_id=str(uuid.uuid4()))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "BITRIX_SUPPLIER_NOT_FOUND"


@requires_db
def test_import_price_rejects_wrong_signature(
    client, bitrix, storage, db, monkeypatch
) -> None:
    """Подделанный прайс не создаёт ни версии, ни импорта Phase 2."""
    from app.modules.integrations.bitrix.port import BitrixFile

    bitrix.add_file(
        TENDER_ID,
        BitrixFile(
            external_file_id="disk-file-fake-price",
            name="Прайс.pdf",
            media_type="application/pdf",
            source="TIMELINE",
        ),
        content=b"not a pdf at all",
    )
    headers = _auth(client)
    batch = _batch(client, headers)
    supplier = _supplier(client)
    accepted = _import_price(
        client,
        headers,
        batch,
        supplier_id=supplier["id"],
        price_file="disk-file-fake-price",
    )

    outcome, _ = _run_price_job(
        accepted.json()["jobId"], bitrix, storage, monkeypatch
    )

    assert outcome.status == "FAILED"
    assert outcome.error["retryable"] is False
    created = db.execute(
        text("SELECT count(*) FROM price_files WHERE supplier_id = :supplier_id"),
        {"supplier_id": supplier["id"]},
    ).scalar()
    assert created == 0


@requires_db
def test_price_download_failure_creates_no_partial_records(
    client, bitrix, storage, db
) -> None:
    """Сбой скачивания Ф2 не оставляет локальный ценовой комплект."""
    from app.modules.integrations.bitrix.errors import timeout

    headers = _auth(client)
    batch = _batch(client, headers)
    supplier = _supplier(client)
    accepted = _import_price(client, headers, batch, supplier_id=supplier["id"])
    bitrix.fail_next("download_file", timeout())

    outcome = _run_job(accepted.json()["jobId"], bitrix, storage)

    assert outcome.status == "RETRY_SCHEDULED"
    for table in ("price_files", "price_versions", "price_imports"):
        assert db.execute(
            text(f"SELECT count(*) FROM {table} WHERE supplier_id = :supplier_id"),
            {"supplier_id": supplier["id"]},
        ).scalar() == 0
    assert storage.objects == {}


@requires_db
def test_repeat_import_of_price_file_conflicts(
    client, bitrix, storage, monkeypatch
) -> None:
    """Тот же файл сделки нельзя провести в Phase 2 дважды."""
    headers = _auth(client)
    batch = _batch(client, headers)
    supplier = _supplier(client)
    first = _import_price(client, headers, batch, supplier_id=supplier["id"])
    _run_price_job(first.json()["jobId"], bitrix, storage, monkeypatch)

    repeated = _import_price(client, headers, batch, supplier_id=supplier["id"])

    assert repeated.status_code == 409
    body = repeated.json()["error"]
    assert body["code"] == "FILE_ALREADY_IMPORTED"
    assert body["details"]["priceFileId"] is not None


@requires_db
def test_bitrix_price_is_moved_from_quarantine_after_registration(
    client, bitrix, storage, monkeypatch
) -> None:
    """Успешный Bitrix-импорт не оставляет рабочий файл в quarantine."""
    headers = _auth(client)
    batch = _batch(client, headers)
    supplier = _supplier(client)
    accepted = _import_price(client, headers, batch, supplier_id=supplier["id"])

    outcome, _ = _run_price_job(
        accepted.json()["jobId"], bitrix, storage, monkeypatch
    )

    assert outcome.status == "COMPLETED", outcome.error
    price = outcome.result["prices"][PRICE_FILE]
    price_file_id = price["priceFileId"]
    assert any(
        bucket == "price-files" and key.startswith("sha256/")
        for bucket, key in storage.objects
    )
    assert not any(bucket == "quarantine" for bucket, _key in storage.objects)
    assert price_file_id


@requires_db
def test_same_bitrix_price_content_reuses_existing_import(
    client, bitrix, storage, monkeypatch
) -> None:
    """Разные externalFileId с одинаковым SHA-256 не создают второй PriceFile."""
    from app.modules.integrations.bitrix.fake import SAMPLE_PDF
    from app.modules.integrations.bitrix.port import BitrixFile

    bitrix.add_file(
        TENDER_ID,
        BitrixFile(
            external_file_id="disk-file-price-copy",
            name="Прайс поставщика копия.pdf",
            media_type="application/pdf",
            source="DISK",
        ),
        content=SAMPLE_PDF,
    )
    headers = _auth(client)
    batch = _batch(client, headers)
    supplier = _supplier(client)
    first = _import_price(client, headers, batch, supplier_id=supplier["id"])
    first_outcome, _ = _run_price_job(
        first.json()["jobId"], bitrix, storage, monkeypatch
    )
    assert first_outcome.status == "COMPLETED", first_outcome.error

    second = _import_price(
        client,
        headers,
        batch,
        supplier_id=supplier["id"],
        price_file="disk-file-price-copy",
    )
    second_outcome, _ = _run_price_job(
        second.json()["jobId"], bitrix, storage, monkeypatch
    )

    assert second_outcome.status == "COMPLETED", second_outcome.error
    assert (
        second_outcome.result["prices"]["disk-file-price-copy"]["priceFileId"]
        == first_outcome.result["prices"][PRICE_FILE]["priceFileId"]
    )


@pytest.mark.parametrize(
    ("document_type", "status", "check_batch_id", "allowed"),
    [
        ("PRICE_LIST", "validated", None, True),
        ("PRICE_LIST", "uploaded", None, False),
        ("PRICE_LIST", "validated", uuid.uuid4(), False),
        ("SUPPLIER_QUOTE", "validated", uuid.uuid4(), False),
        ("SUPPLIER_QUOTE", "active", uuid.uuid4(), False),
    ],
)
def test_price_version_activation_guard(
    document_type: str,
    status: str,
    check_batch_id: uuid.UUID | None,
    allowed: bool,
) -> None:
    """Активировать можно только проверенный общий прайс.

    Разовый счёт/КП не становится действующим прайсом поставщика: иначе он
    вытеснил бы настоящий общий прайс и попал в общий каталог.
    """
    from fastapi import HTTPException

    from app.api.versions import ensure_price_version_activatable
    from app.models import PriceVersion

    version = PriceVersion(
        supplier_id=uuid.uuid4(),
        price_file_id=uuid.uuid4(),
        version_no=1,
        status=status,
        document_type=document_type,
        check_batch_id=check_batch_id,
    )
    if allowed:
        ensure_price_version_activatable(version)
    else:
        with pytest.raises(HTTPException) as raised:
            ensure_price_version_activatable(version)
        assert raised.value.status_code == 409
