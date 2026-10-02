"""Клиент реального портала через входящий вебхук.

Разделение на транспорт и клиент сделано ради проверяемости: классификация
ошибок портала (429 с `Retry-After`, 401/403, 5xx, таймаут) — единственная
часть, которую можно и нужно тестировать без доступов, и она живёт в
`UrllibTransport`. Прикладные методы поверх него только раскладывают ответы по
моделям порта.

HTTP выполняется через `urllib` в пуле потоков — так же, как обращение к
внешнему LLM в `extraction/providers.py`: отдельная HTTP-библиотека в runtime
зависимостях ради двух запросов не нужна.

Имена полей сделки не угадываются: `bitrix_tender_field`, `bitrix_file_fields`
и `bitrix_deal_folder_field` задаются конфигурацией, и при пустой конфигурации
клиент отвечает понятной ошибкой, а не молча возвращает пустой список.

Проверено на живом портале 2026-09-01 (`profile`, `crm.deal.list`,
`crm.deal.fields`, `crm.deal.userfield.list`, `disk.storage.getlist`):
поиск сделки и карточка работают. Скачивание файла и публикация отчёта на
живом портале **не проверялись** — в том портале нет ни файлов у сделок, ни
папки для отчётов.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.modules.integrations.bitrix.errors import (
    BitrixTransportCode,
    BitrixTransportError,
    failed_auth,
    rate_limited,
    timeout,
)
from app.modules.integrations.bitrix.port import (
    BitrixDeal,
    BitrixFile,
    DealFiles,
    DownloadedFile,
    OutgoingFile,
    PublishedReport,
)

#: Предел размера ответа REST-метода. Файлы качаются отдельным путём, поэтому
#: метровый ответ здесь означает ошибку, а не большой документ.
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

#: Сколько комментариев ленты показывать. Их пишет и человек, и Фаза 1;
#: список файлов не должен утонуть в переписке.
MAX_TIMELINE_NOTES = 5


class WebhookTransport(Protocol):
    """Транспорт REST-вызовов портала."""

    async def call(self, method: str, params: dict[str, Any]) -> Any: ...

    async def fetch(self, url: str, limit: int) -> bytes: ...


class UrllibTransport:
    """Транспорт поверх `urllib` с классификацией ошибок портала."""

    def __init__(self, settings: Settings) -> None:
        if not settings.bitrix_base_url or not settings.bitrix_webhook_token:
            raise ValueError("bitrix_base_url и bitrix_webhook_token обязательны")
        # Токен склеивается с URL здесь и больше нигде: наружу отдаётся только
        # имя метода, а базовый URL в журнал не пишется.
        base = settings.bitrix_base_url.rstrip("/") + "/"
        self._base = urljoin(base, settings.bitrix_webhook_token.strip("/") + "/")
        self._timeout = settings.bitrix_timeout_seconds
        self._download_timeout = settings.bitrix_download_timeout_seconds

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self._base}{method}.json",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        raw = await run_in_threadpool(self._send, request, method, self._timeout)
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BitrixTransportError(
                BitrixTransportCode.CONTRACT_ERROR,
                f"Портал вернул не JSON на {method}",
                retryable=False,
            ) from exc
        if isinstance(envelope, dict) and "error" in envelope:
            raise BitrixTransportError(
                BitrixTransportCode.CONTRACT_ERROR,
                f"{method}: {envelope.get('error')}",
                retryable=False,
            )
        return envelope.get("result") if isinstance(envelope, dict) else envelope

    async def fetch(self, url: str, limit: int) -> bytes:
        request = Request(url, method="GET")
        return await run_in_threadpool(
            self._send, request, "download", self._download_timeout, limit
        )

    # --- внутреннее ------------------------------------------------------

    def _send(
        self,
        request: Request,
        method: str,
        seconds: int,
        limit: int = MAX_RESPONSE_BYTES,
    ) -> bytes:
        # Параметр назван не `timeout`: так зовётся конструктор ошибки таймаута,
        # который вызывается ниже.
        try:
            with urlopen(request, timeout=seconds) as response:
                payload = response.read(limit + 1)
        except HTTPError as exc:
            raise self._classify(exc, method) from exc
        except TimeoutError as exc:
            raise timeout(f"{method}: портал не ответил") from exc
        except URLError as exc:
            raise BitrixTransportError(
                BitrixTransportCode.NETWORK,
                f"{method}: портал недоступен",
                retryable=True,
            ) from exc
        if len(payload) > limit:
            raise BitrixTransportError(
                BitrixTransportCode.CONTRACT_ERROR,
                f"{method}: ответ портала больше допустимого",
                retryable=False,
            )
        return payload

    @staticmethod
    def _classify(exc: HTTPError, method: str) -> BitrixTransportError:
        """HTTP-статус портала → класс сбоя по таблице retry policy."""
        if exc.code == 429:
            return rate_limited(_retry_after(exc))
        if exc.code in (401, 403):
            return failed_auth(f"{method}: портал отклонил учётные данные")
        if exc.code == 404:
            return BitrixTransportError(
                BitrixTransportCode.CONTRACT_ERROR,
                f"{method}: метод недоступен на портале",
                retryable=False,
            )
        if exc.code >= 500:
            return BitrixTransportError(
                BitrixTransportCode.SERVER_ERROR,
                f"{method}: портал ответил {exc.code}",
                retryable=True,
            )
        return BitrixTransportError(
            BitrixTransportCode.CONTRACT_ERROR,
            f"{method}: портал отверг запрос ({exc.code})",
            retryable=False,
        )


def _retry_after(exc: HTTPError) -> int | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return None


class WebhookBitrixClient:
    """Реализация `BitrixPort` поверх REST-методов Bitrix24."""

    def __init__(self, settings: Settings, transport: WebhookTransport) -> None:
        self._settings = settings
        self._transport = transport

    async def ping(self) -> None:
        await self._transport.call("profile", {})

    async def get_deal(self, tender_id: str) -> BitrixDeal:
        raw = await self._find_deal(tender_id)
        customer = await self._company_title(raw.get("COMPANY_ID"))
        assignee = await self._user_name(raw.get("ASSIGNED_BY_ID"))
        deal_id = str(raw["ID"])
        return BitrixDeal(
            tender_id=tender_id,
            deal_id=deal_id,
            title=raw.get("TITLE"),
            customer=customer,
            stage=raw.get("STAGE_ID"),
            assignee=assignee,
            updated_at=_parse_datetime(raw.get("DATE_MODIFY")),
            external_url=self._deal_url(deal_id),
        )

    async def list_files(self, tender_id: str) -> DealFiles:
        raw = await self._find_deal(tender_id)
        files = [
            file
            for field in self._settings.bitrix_file_fields
            for file in _files_of_field(raw.get(field), source="DEAL_FIELD")
        ]
        folder_id = self._folder_id(raw)
        if folder_id:
            files.extend(await self._folder_files(folder_id))
        timeline = self._settings.bitrix_read_timeline_files
        notes: list[str] = []
        if timeline:
            timeline_files, notes = await self._timeline_files(str(raw["ID"]))
            files.extend(timeline_files)
        if not self._settings.bitrix_file_fields and not folder_id and not timeline:
            raise BitrixTransportError(
                BitrixTransportCode.CONTRACT_ERROR,
                "Источник файлов сделки не настроен: задайте bitrix_file_fields,"
                " bitrix_deal_folder_field или bitrix_read_timeline_files",
                retryable=False,
            )
        return DealFiles(files=files, notes=notes)

    async def download_file(self, external_file_id: str) -> DownloadedFile:
        raw = await self._transport.call("disk.file.get", {"id": external_file_id})
        if not raw:
            raise BitrixTransportError(
                BitrixTransportCode.FILE_NOT_FOUND,
                f"Файл {external_file_id} не найден",
                retryable=False,
            )
        url = raw.get("DOWNLOAD_URL")
        if not url:
            raise BitrixTransportError(
                BitrixTransportCode.CONTRACT_ERROR,
                f"У файла {external_file_id} нет ссылки на скачивание",
                retryable=False,
            )
        data = await self._transport.fetch(url, self._settings.upload_max_file_bytes)
        return DownloadedFile(
            name=raw.get("NAME") or external_file_id,
            media_type=raw.get("CONTENT_TYPE"),
            data=data,
        )

    async def publish_report(
        self,
        *,
        deal_id: str,
        files: dict[str, OutgoingFile],
        comment: str | None,
    ) -> PublishedReport:
        folder_id = self._settings.bitrix_report_folder_id
        if files and not folder_id:
            raise BitrixTransportError(
                BitrixTransportCode.CONTRACT_ERROR,
                "Папка Диска для отчётов не настроена: задайте"
                " bitrix_report_folder_id",
                retryable=False,
            )
        file_ids: dict[str, str] = {}
        for artifact_id, outgoing in files.items():
            uploaded = await self._transport.call(
                "disk.folder.uploadfile",
                {
                    "id": folder_id,
                    "data": {"NAME": outgoing.file_name},
                    "fileContent": [
                        outgoing.file_name,
                        base64.b64encode(outgoing.data).decode("ascii"),
                    ],
                    "generateUniqueName": True,
                },
            )
            file_ids[artifact_id] = str(uploaded["ID"])

        comment_id = None
        if comment is not None:
            created = await self._transport.call(
                "crm.timeline.comment.add",
                {"fields": {"ENTITY_ID": deal_id, "ENTITY_TYPE": "deal", "COMMENT": comment}},
            )
            comment_id = str(created)
        return PublishedReport(deal_id=deal_id, file_ids=file_ids, comment_id=comment_id)

    # --- внутреннее ------------------------------------------------------

    async def _find_deal(self, tender_id: str) -> dict[str, Any]:
        """Сделка по номеру тендера.

        Поле связи задаётся конфигурацией: штатного «номера тендера» в
        `crm.deal` нет. Значение `ID` означает, что номер тендера и есть
        идентификатор сделки.
        """
        field = self._settings.bitrix_tender_field
        if field == "ID":
            raw = await self._transport.call("crm.deal.get", {"id": tender_id})
            found = raw or None
        else:
            rows = await self._transport.call(
                "crm.deal.list",
                {"filter": {field: tender_id}, "select": ["*", "UF_*"]},
            )
            if rows and len(rows) > 1:
                # Фаза 1 при таймауте «усыновляет» уже созданную сделку, сверяясь
                # по этому же полю. Если сверка не сработала и сделок оказалось
                # две, молча взять первую значит привязать заявку к случайной из
                # них — и опубликовать отчёт не туда.
                raise BitrixTransportError(
                    BitrixTransportCode.CONTRACT_ERROR,
                    f"По тендеру {tender_id} найдено сделок: {len(rows)}"
                    f" ({', '.join(str(row.get('ID')) for row in rows[:5])})."
                    " Нужна одна: устраните дубль в портале",
                    retryable=False,
                )
            found = rows[0] if rows else None
        if not found:
            raise BitrixTransportError(
                BitrixTransportCode.DEAL_NOT_FOUND,
                f"Сделка по тендеру {tender_id} не найдена",
                retryable=False,
            )
        self._assert_matches(found, field, tender_id)
        return found

    @staticmethod
    def _assert_matches(deal: dict[str, Any], field: str, tender_id: str) -> None:
        """Проверяет, что найденная сделка действительно та.

        Обязательная проверка, а не перестраховка: `crm.deal.list` с фильтром по
        несуществующему полю **молча игнорирует фильтр** и возвращает первую
        попавшуюся сделку. Без сверки опечатка в `BITRIX_TENDER_FIELD` привязала
        бы заявку к чужой сделке и опубликовала бы туда отчёт.
        """
        if field == "ID":
            return
        actual = deal.get(field)
        if str(actual).strip() != str(tender_id).strip():
            raise BitrixTransportError(
                BitrixTransportCode.CONTRACT_ERROR,
                f"Портал вернул сделку {deal.get('ID')}, у которой {field}"
                f" = {actual!r}, а не {tender_id!r}. Скорее всего поле"
                " BITRIX_TENDER_FIELD не существует и фильтр был проигнорирован",
                retryable=False,
            )

    async def _company_title(self, company_id: Any) -> str | None:
        if not company_id or str(company_id) == "0":
            return None
        raw = await self._transport.call("crm.company.get", {"id": company_id})
        return raw.get("TITLE") if raw else None

    async def _user_name(self, user_id: Any) -> str | None:
        if not user_id or str(user_id) == "0":
            return None
        rows = await self._transport.call("user.get", {"ID": user_id})
        if not rows:
            return None
        user = rows[0] if isinstance(rows, list) else rows
        name = " ".join(
            part for part in (user.get("LAST_NAME"), user.get("NAME")) if part
        )
        return name or None

    async def _folder_files(self, folder_id: str) -> list[BitrixFile]:
        rows = await self._transport.call(
            "disk.folder.getchildren", {"id": folder_id}
        )
        return [
            BitrixFile(
                external_file_id=str(row["ID"]),
                name=row.get("NAME") or str(row["ID"]),
                size_bytes=_integer(row.get("SIZE")),
                media_type=row.get("CONTENT_TYPE"),
                source="DISK",
                created_at=_parse_datetime(row.get("CREATE_TIME")),
            )
            for row in rows or []
            if row.get("TYPE") != "folder"
        ]

    async def _timeline_files(
        self, deal_id: str
    ) -> tuple[list[BitrixFile], list[str]]:
        """Вложения комментариев ленты сделки.

        Основной путь появления файлов на этом проекте: почтовый контур Фазы 1
        прикладывает вложения письма к комментарию (`FILES`). Имя и размер
        обычно приходят прямо в комментарии, и лишний запрос за метаданными не
        нужен; `disk.file.get` остаётся запасным путём для версий портала,
        которые отдают голые идентификаторы.
        """
        comments = await self._transport.call(
            "crm.timeline.comment.list",
            {"filter": {"ENTITY_ID": deal_id, "ENTITY_TYPE": "deal"}},
        )
        rows = comments or []
        entries = [
            entry
            for comment in rows
            for entry in _timeline_entries(comment.get("FILES"))
        ]
        # Текст комментария несёт то, чего нет в списке файлов: Фаза 1 не грузит
        # вложения крупнее своего лимита и перечисляет их имена здесь.
        notes = [
            text
            for text in (str(comment.get("COMMENT") or "").strip() for comment in rows)
            if text
        ][:MAX_TIMELINE_NOTES]

        files: list[BitrixFile] = []
        seen: set[str] = set()
        for entry in entries:
            # Один файл может быть приложен к нескольким комментариям.
            if entry["id"] in seen:
                continue
            seen.add(entry["id"])
            name, size = entry["name"], _integer(entry["size"])
            if not name:
                # Голый идентификатор: имя и размер приходится дочитывать.
                info = await self._transport.call(
                    "disk.file.get", {"id": entry["id"]}
                )
                if not info:
                    continue
                name, size = info.get("NAME"), _integer(info.get("SIZE"))
            files.append(
                BitrixFile(
                    external_file_id=entry["id"],
                    name=str(name or entry["id"]),
                    size_bytes=size,
                    source="TIMELINE",
                )
            )
        return files, notes

    def _folder_id(self, deal: dict[str, Any]) -> str | None:
        field = self._settings.bitrix_deal_folder_field
        value = deal.get(field) if field else None
        return str(value) if value else None

    def _deal_url(self, deal_id: str) -> str | None:
        base = self._settings.bitrix_base_url
        if not base:
            return None
        # Ссылка ведёт на портал, а не на вебхук: путь вебхука с токеном
        # наружу отдавать нельзя.
        portal = base.split("/rest/")[0].rstrip("/")
        return f"{portal}/crm/deal/details/{deal_id}/"


def _timeline_entries(value: Any) -> list[dict[str, Any]]:
    """`FILES` комментария → записи о файлах.

    Портал отдаёт словарь `{"<id>": {id, name, size, ...}}` — проверено на
    живом портале. Часть версий отдаёт список объектов или голых
    идентификаторов, поэтому принимаем все три вида: иначе смена версии молча
    оставила бы импорт без файлов.
    """
    if isinstance(value, dict):
        items: list[Any] = list(value.values())
    elif isinstance(value, list):
        items = value
    else:
        return []

    entries: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            found = item.get("id") or item.get("ID")
            if found:
                entries.append(
                    {
                        "id": str(found),
                        "name": item.get("name") or item.get("NAME"),
                        "size": item.get("size") or item.get("SIZE"),
                    }
                )
        elif item:
            entries.append({"id": str(item), "name": None, "size": None})
    return entries


def _files_of_field(value: Any, *, source: str) -> list[BitrixFile]:
    """UF-поле типа «файл» → список файлов. Поле бывает одиночным и массивом."""
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    files: list[BitrixFile] = []
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            files.append(
                BitrixFile(
                    external_file_id=str(item["id"]),
                    name=str(item.get("name") or item["id"]),
                    size_bytes=_integer(item.get("size")),
                    media_type=item.get("contentType"),
                    source=source,
                )
            )
    return files


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
