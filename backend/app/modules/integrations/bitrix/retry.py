"""Политика повторов из таблицы контракта.

| Ошибка | Поведение |
|---|---|
| timeout/network/5xx | exponential backoff + jitter |
| 429 | учитывать `Retry-After` |
| 401/403 | терминально, без бесконечного retry |
| deal/file 404 | терминальная бизнес-ошибка |
| invalid mapping/payload | терминальная contract-ошибка |

Политика вынесена отдельно от исполнителя, потому что это единственная часть
интеграции, которую нужно проверять без БД, воркера и портала.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

from app.config import Settings
from app.modules.integrations.bitrix.errors import BitrixTransportError


@dataclass(frozen=True)
class RetryDecision:
    """Что делать с заданием после неудачной попытки."""

    retry: bool
    delay_seconds: int = 0
    #: Причина отказа от повтора — попадает в `integration_jobs.error`.
    reason: str | None = None


def plan_retry(
    error: BitrixTransportError,
    *,
    attempt: int,
    settings: Settings,
    jitter: Callable[[int], int] | None = None,
) -> RetryDecision:
    """Решение по одной неудачной попытке.

    `attempt` — номер только что завершившейся попытки, начиная с 1.
    """
    if not error.retryable:
        return RetryDecision(retry=False, reason="TERMINAL_ERROR")
    if attempt >= settings.bitrix_max_attempts:
        return RetryDecision(retry=False, reason="ATTEMPTS_EXHAUSTED")

    if error.retry_after_seconds is not None:
        # `Retry-After` портала уважается как есть: собственный backoff поверх
        # него означал бы игнорировать явное указание сервера.
        delay = min(error.retry_after_seconds, settings.bitrix_retry_max_seconds)
        return RetryDecision(retry=True, delay_seconds=delay)

    backoff = settings.bitrix_retry_base_seconds * (2 ** (attempt - 1))
    backoff = min(backoff, settings.bitrix_retry_max_seconds)
    # Jitter обязателен: без него все задания, упавшие на одном сбое портала,
    # вернутся к нему одновременно и повторят обвал.
    spread = jitter or (lambda limit: random.randint(0, limit))  # noqa: S311
    return RetryDecision(retry=True, delay_seconds=backoff + spread(backoff // 2))
