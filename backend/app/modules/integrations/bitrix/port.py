"""Контракт портала: нормализованные модели и Protocol.

Модели намеренно не повторяют структуру ответов Bitrix24. Контракт модуля
требует, чтобы raw payload провайдера не протекал во frontend, а без
собственных моделей это требование пришлось бы соблюдать в каждом маппере
отдельно. Здесь же граница одна: всё, что за портом, разговаривает только
этими типами.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

#: Откуда взят файл сделки. Определяет, каким методом портала его читать, и
#: показывается менеджеру: у файла из письма и у файла из папки сделки разный
#: уровень доверия.
FILE_SOURCES = ("DEAL_FIELD", "DISK", "TIMELINE")


@dataclass(frozen=True)
class BitrixDeal:
    """Карточка сделки в терминах нашей предметной области."""

    tender_id: str
    deal_id: str
    title: str | None = None
    customer: str | None = None
    stage: str | None = None
    assignee: str | None = None
    updated_at: datetime | None = None
    external_url: str | None = None


@dataclass(frozen=True)
class BitrixFile:
    """Файл, доступный в сделке."""

    external_file_id: str
    name: str
    size_bytes: int | None = None
    media_type: str | None = None
    source: str = "DISK"
    created_at: datetime | None = None


@dataclass(frozen=True)
class DealFiles:
    """Файлы сделки вместе с текстом комментариев ленты.

    Комментарии возвращаются не для красоты: Фаза 1 не грузит на портал файлы
    крупнее своего лимита, а перечисляет их имена в тексте. Без этого текста
    менеджер видел бы короткий список и не знал, что часть документов до
    портала не доехала.
    """

    files: list[BitrixFile] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DownloadedFile:
    """Содержимое файла портала.

    Имя приходит вместе с байтами: между `list_files` и скачиванием файл могли
    переименовать, и документ должен сохраниться под фактическим именем.
    """

    name: str
    media_type: str | None
    data: bytes


@dataclass(frozen=True)
class OutgoingFile:
    """Файл, отправляемый в сделку (отчёт)."""

    file_name: str
    media_type: str
    data: bytes


@dataclass(frozen=True)
class PublishedReport:
    """Результат публикации: идентификаторы созданного в Bitrix24."""

    deal_id: str
    #: `report_artifact_id` → id файла на портале. Ключ наш, а не внешний:
    #: повтор публикации должен уметь понять, какой артефакт уже загружен.
    file_ids: dict[str, str] = field(default_factory=dict)
    comment_id: str | None = None


class BitrixPort(Protocol):
    """Минимум операций портала, который нужен модулю.

    Все методы поднимают `BitrixTransportError` при сбое обращения — исполнитель
    задания различает повторяемые и терминальные ошибки только по нему.
    """

    async def ping(self) -> None:
        """Проверка связи для health-эндпоинта."""
        ...

    async def get_deal(self, tender_id: str) -> BitrixDeal: ...

    async def list_files(self, tender_id: str) -> DealFiles: ...

    async def download_file(self, external_file_id: str) -> DownloadedFile: ...

    async def publish_report(
        self,
        *,
        deal_id: str,
        files: dict[str, OutgoingFile],
        comment: str | None,
    ) -> PublishedReport:
        """Кладёт файлы в сделку и, если задан текст, пишет комментарий.

        `files` ключуется идентификатором нашего артефакта: повторная
        публикация передаёт только те артефакты, которых на портале ещё нет.
        """
        ...
