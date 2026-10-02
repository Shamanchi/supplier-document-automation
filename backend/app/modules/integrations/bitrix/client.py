"""Выбор реализации порта по конфигурации.

Единственное место, которое знает про существование стенда. Прикладной слой
работает с `BitrixPort` и не различает, куда уходят вызовы — этим переключение
на реальный портал и сводится к изменению `bitrix_mode`.
"""

from __future__ import annotations

from app.config import Settings
from app.modules.integrations.bitrix.fake import FakeBitrixClient
from app.modules.integrations.bitrix.port import BitrixPort

#: Клиент на процесс. Для стенда это обязательно: его сделки и файлы живут в
#: памяти, и новый экземпляр на запрос терял бы импортированное состояние.
_client: BitrixPort | None = None
_client_mode: str | None = None


def build_bitrix_client(settings: Settings) -> BitrixPort | None:
    """Клиент портала либо `None`, если интеграция выключена.

    `None` вместо исключения: health-эндпоинт обязан отвечать и при выключенной
    интеграции, а решение «отдать 503» принимает прикладной слой.
    """
    global _client, _client_mode

    if not settings.bitrix_configured:
        return None
    if _client is not None and _client_mode == settings.bitrix_mode:
        return _client

    if settings.bitrix_mode == "fake":
        _client = FakeBitrixClient.with_sample_deal()
    else:
        # Импорт внутри ветки: транспорт тянет urllib и starlette-пул, а стенду
        # и тестам они не нужны.
        from app.modules.integrations.bitrix.webhook import (
            UrllibTransport,
            WebhookBitrixClient,
        )

        _client = WebhookBitrixClient(settings, UrllibTransport(settings))
    _client_mode = settings.bitrix_mode
    return _client


def reset_bitrix_client() -> None:
    """Сбрасывает кэш клиента. Нужен тестам, меняющим режим интеграции."""
    global _client, _client_mode
    _client = None
    _client_mode = None
