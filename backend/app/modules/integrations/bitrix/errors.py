"""Коды ошибок модуля и ошибка транспорта.

Разделение принципиальное. `ApiError` с кодом из `BitrixErrorCode` — ответ
клиенту нашего API. `BitrixTransportError` — сбой обращения к порталу: он не
доходит до клиента, а попадает в `integration_jobs.error` и определяет, будет
ли повтор (таблица retry policy контракта).
"""

from __future__ import annotations

from enum import StrEnum


class BitrixErrorCode(StrEnum):
    """Коды из docs/api/integrations/bitrix/README.md §Ошибки API.

    `BITRIX_BATCH_NOT_LINKED` в исходной таблице контракта отсутствует: там
    предусловие «batch связан со сделкой» описано словами, но кода ошибки для
    него нет. Добавлен здесь и в README модуля.
    """

    DEAL_NOT_FOUND = "BITRIX_DEAL_NOT_FOUND"
    FILE_NOT_FOUND = "BITRIX_FILE_NOT_FOUND"
    FILE_ALREADY_IMPORTED = "FILE_ALREADY_IMPORTED"
    REPORT_ALREADY_PUBLISHED = "REPORT_ALREADY_PUBLISHED"
    FILE_MAPPING_REQUIRED = "BITRIX_FILE_MAPPING_REQUIRED"
    SUPPLIER_REQUIRED = "BITRIX_SUPPLIER_REQUIRED"
    SUPPLIER_NOT_FOUND = "BITRIX_SUPPLIER_NOT_FOUND"
    PRICE_DOCUMENT_TYPE_REQUIRED = "BITRIX_PRICE_DOCUMENT_TYPE_REQUIRED"
    BATCH_NOT_LINKED = "BITRIX_BATCH_NOT_LINKED"
    BATCH_LINK_CONFLICT = "BITRIX_BATCH_LINK_CONFLICT"
    BATCH_ITEMS_REQUIRED = "BITRIX_BATCH_ITEMS_REQUIRED"
    REPORT_NOT_READY = "REPORT_NOT_READY"
    NOT_CONFIGURED = "BITRIX_NOT_CONFIGURED"
    JOB_NOT_FOUND = "BITRIX_JOB_NOT_FOUND"
    JOB_NOT_RETRYABLE = "BITRIX_JOB_NOT_RETRYABLE"


class BitrixTransportCode(StrEnum):
    """Классы сбоев портала. Значение попадает в `integration_jobs.error.code`."""

    TIMEOUT = "BITRIX_TIMEOUT"
    NETWORK = "BITRIX_NETWORK_ERROR"
    SERVER_ERROR = "BITRIX_SERVER_ERROR"
    RATE_LIMITED = "BITRIX_RATE_LIMITED"
    FAILED_AUTH = "BITRIX_FAILED_AUTH"
    DEAL_NOT_FOUND = "BITRIX_DEAL_NOT_FOUND"
    FILE_NOT_FOUND = "BITRIX_FILE_NOT_FOUND"
    CONTRACT_ERROR = "BITRIX_CONTRACT_ERROR"


class BitrixTransportError(Exception):
    """Сбой обращения к порталу.

    `retryable` вычисляется здесь, а не в исполнителе: только источник ошибки
    знает, был ли это таймаут сети или отказ прав. Исполнитель лишь применяет
    политику.
    """

    def __init__(
        self,
        code: BitrixTransportCode,
        message: str,
        *,
        retryable: bool,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds

    def as_json(self) -> dict[str, object]:
        """Представление для `integration_jobs.error`.

        Ни URL, ни токен сюда не попадают: строка ошибки хранится в БД и
        отдаётся клиенту в состоянии задания.
        """
        payload: dict[str, object] = {
            "code": str(self.code),
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.retry_after_seconds is not None:
            payload["retryAfterSeconds"] = self.retry_after_seconds
        return payload


def timeout(message: str = "Портал не ответил за отведённое время") -> BitrixTransportError:
    return BitrixTransportError(
        BitrixTransportCode.TIMEOUT, message, retryable=True
    )


def rate_limited(retry_after_seconds: int | None = None) -> BitrixTransportError:
    return BitrixTransportError(
        BitrixTransportCode.RATE_LIMITED,
        "Портал ограничил частоту запросов",
        retryable=True,
        retry_after_seconds=retry_after_seconds,
    )


def failed_auth(message: str = "Портал отклонил учётные данные") -> BitrixTransportError:
    return BitrixTransportError(
        BitrixTransportCode.FAILED_AUTH, message, retryable=False
    )
