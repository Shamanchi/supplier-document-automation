"""Стенд портала без сети.

Нужен для трёх вещей сразу: контрактных тестов, демонстрации Pilot-сценария до
получения доступов и локальной разработки frontend. Поэтому это не «мок в
тестах», а полноценная реализация порта, живущая в приложении и включаемая
конфигурацией (`bitrix_mode=fake`).

Сбои программируются вызовом `fail_next`: политика повторов проверяется на тех
же ошибках, которые вернёт реальный портал, без подмены транспорта.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime

from app.modules.integrations.bitrix.errors import (
    BitrixTransportCode,
    BitrixTransportError,
)
from app.modules.integrations.bitrix.port import (
    BitrixDeal,
    BitrixFile,
    DealFiles,
    DownloadedFile,
    OutgoingFile,
    PublishedReport,
)

#: Минимальный PDF, проходящий проверку сигнатуры модуля uploads.
SAMPLE_PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"

DEFAULT_TENDER_ID = "12345"
DEFAULT_DEAL_ID = "9876"


class FakeBitrixClient:
    """Реализация `BitrixPort` поверх словарей в памяти."""

    def __init__(self, *, portal_url: str = "https://sandbox.bitrix24.ru") -> None:
        self._portal_url = portal_url
        self._deals: dict[str, BitrixDeal] = {}
        self._files: dict[str, list[BitrixFile]] = defaultdict(list)
        self._contents: dict[str, bytes] = {}
        self._notes: dict[str, list[str]] = defaultdict(list)
        self._failures: dict[str, deque[BitrixTransportError]] = defaultdict(deque)
        #: Что уже опубликовано: deal_id → результат. Публикация дописывает
        #: сюда, а не перезаписывает — так виден повтор.
        self.published: dict[str, PublishedReport] = {}
        #: Комментарии таймлайна. Тест на повтор публикации проверяет длину.
        self.comments: list[tuple[str, str]] = []
        #: Журнал вызовов: (метод, аргумент). Нужен тестам про rate limit.
        self.calls: list[tuple[str, str]] = []

    # --- наполнение стенда ---------------------------------------------

    @classmethod
    def with_sample_deal(cls) -> FakeBitrixClient:
        """Стенд с примером из контракта: сделка 12345 и два файла."""
        client = cls()
        client.add_deal(
            BitrixDeal(
                tender_id=DEFAULT_TENDER_ID,
                deal_id=DEFAULT_DEAL_ID,
                title="Поставка насосного оборудования",
                customer="АО Заказчик",
                stage="PREPARATION",
                assignee="Иван Петров",
                updated_at=datetime(2026, 6, 30, 9, 45, tzinfo=UTC),
                external_url=(
                    f"https://sandbox.bitrix24.ru/crm/deal/details/{DEFAULT_DEAL_ID}/"
                ),
            )
        )
        client.add_file(
            DEFAULT_TENDER_ID,
            BitrixFile(
                external_file_id="disk-file-tz",
                name="Техническое задание.pdf",
                size_bytes=len(SAMPLE_PDF),
                media_type="application/pdf",
                source="DEAL_FIELD",
                created_at=datetime(2026, 6, 29, 12, 0, tzinfo=UTC),
            ),
            content=SAMPLE_PDF,
        )
        client.add_file(
            DEFAULT_TENDER_ID,
            BitrixFile(
                external_file_id="disk-file-passport",
                name="Паспорт насоса.pdf",
                size_bytes=len(SAMPLE_PDF),
                media_type="application/pdf",
                source="DISK",
                created_at=datetime(2026, 6, 29, 12, 5, tzinfo=UTC),
            ),
            content=SAMPLE_PDF,
        )
        client.add_file(
            DEFAULT_TENDER_ID,
            BitrixFile(
                external_file_id="disk-file-price",
                name="КП поставщика.pdf",
                size_bytes=len(SAMPLE_PDF),
                media_type="application/pdf",
                source="TIMELINE",
                created_at=datetime(2026, 6, 29, 12, 10, tzinfo=UTC),
            ),
            content=SAMPLE_PDF,
        )
        return client

    def add_deal(self, deal: BitrixDeal) -> None:
        self._deals[deal.tender_id] = deal

    def add_file(self, tender_id: str, file: BitrixFile, *, content: bytes) -> None:
        self._files[tender_id].append(file)
        self._contents[file.external_file_id] = content

    def fail_next(self, method: str, error: BitrixTransportError) -> None:
        """Программирует сбой на ближайший вызов метода."""
        self._failures[method].append(error)

    # --- реализация порта ----------------------------------------------

    async def ping(self) -> None:
        self._maybe_fail("ping", "")

    async def get_deal(self, tender_id: str) -> BitrixDeal:
        self._maybe_fail("get_deal", tender_id)
        deal = self._deals.get(tender_id)
        if deal is None:
            raise BitrixTransportError(
                BitrixTransportCode.DEAL_NOT_FOUND,
                f"Сделка по тендеру {tender_id} не найдена",
                retryable=False,
            )
        return deal

    async def list_files(self, tender_id: str) -> DealFiles:
        self._maybe_fail("list_files", tender_id)
        await self.get_deal(tender_id)
        return DealFiles(
            files=list(self._files[tender_id]), notes=list(self._notes[tender_id])
        )

    def add_note(self, tender_id: str, text: str) -> None:
        """Комментарий ленты сделки — например предупреждение о крупном файле."""
        self._notes[tender_id].append(text)

    async def download_file(self, external_file_id: str) -> DownloadedFile:
        self._maybe_fail("download_file", external_file_id)
        content = self._contents.get(external_file_id)
        if content is None:
            raise BitrixTransportError(
                BitrixTransportCode.FILE_NOT_FOUND,
                f"Файл {external_file_id} не найден",
                retryable=False,
            )
        meta = self._file_meta(external_file_id)
        return DownloadedFile(
            name=meta.name if meta else external_file_id,
            media_type=meta.media_type if meta else None,
            data=content,
        )

    async def publish_report(
        self,
        *,
        deal_id: str,
        files: dict[str, OutgoingFile],
        comment: str | None,
    ) -> PublishedReport:
        self._maybe_fail("publish_report", deal_id)
        previous = self.published.get(deal_id)
        file_ids = dict(previous.file_ids) if previous else {}
        for artifact_id, outgoing in files.items():
            file_ids[artifact_id] = f"b24-file-{deal_id}-{len(file_ids) + 1}"
            self._contents[file_ids[artifact_id]] = outgoing.data

        comment_id = previous.comment_id if previous else None
        if comment is not None:
            comment_id = f"b24-comment-{deal_id}-{len(self.comments) + 1}"
            self.comments.append((deal_id, comment))

        result = PublishedReport(
            deal_id=deal_id, file_ids=file_ids, comment_id=comment_id
        )
        self.published[deal_id] = result
        return result

    # --- внутреннее ------------------------------------------------------

    def _maybe_fail(self, method: str, argument: str) -> None:
        self.calls.append((method, argument))
        queue = self._failures[method]
        if queue:
            raise queue.popleft()

    def _file_meta(self, external_file_id: str) -> BitrixFile | None:
        for files in self._files.values():
            for file in files:
                if file.external_file_id == external_file_id:
                    return file
        return None
