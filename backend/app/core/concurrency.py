"""Idempotency-Key и optimistic locking.

Оба механизма зафиксированы контрактами:
  * `Idempotency-Key` обязателен на создающих/внешних операциях
    (ARCHITECTURE_CONTRACTS.md, правило ID 5);
  * конкурентное редактирование — через версию сущности: контур Фазы 3
    использует ETag/`If-Match`, витрина Фазы 2 — поле `rowVersion` в теле.

Здесь только транспортная часть: проверка присутствия и формат. Хранение
использованных ключей и сравнение версий выполняет сервис модуля — ему доступна
транзакция, в которой это единственно корректно.

Перенесено из supplier-document-automation без изменений (docs/migration/01-backend-core.md).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Header

from app.core.errors import ApiError, ErrorCode

IDEMPOTENCY_HEADER = "Idempotency-Key"
IF_MATCH_HEADER = "If-Match"


def require_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_HEADER)] = None,
) -> str:
    """Зависимость для операций, повтор которых недопустим.

    Ключ должен быть UUID: свободная строка позволяет клиенту случайно
    переиспользовать одно значение для разных операций.
    """
    if not idempotency_key:
        raise ApiError(
            ErrorCode.IDEMPOTENCY_KEY_REQUIRED,
            f"Требуется заголовок {IDEMPOTENCY_HEADER}",
            status_code=428,
        )
    try:
        UUID(idempotency_key)
    except ValueError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"{IDEMPOTENCY_HEADER} должен быть UUID",
            status_code=422,
        ) from exc
    return idempotency_key


def require_if_match(
    if_match: Annotated[str | None, Header(alias=IF_MATCH_HEADER)] = None,
) -> int:
    """Зависимость для изменяющих операций контура Фазы 3.

    Возвращает версию сущности из ETag. Сравнение с текущей версией — в сервисе,
    внутри той же транзакции, что и запись.
    """
    if not if_match:
        raise ApiError(
            ErrorCode.PRECONDITION_REQUIRED,
            f"Требуется заголовок {IF_MATCH_HEADER}",
            status_code=428,
        )
    return parse_etag(if_match)


def format_etag(version: int) -> str:
    """Версия сущности → значение ETag."""
    return f'"{version}"'


def parse_etag(value: str) -> int:
    """Значение ETag → версия. Поддерживает weak-форму `W/"3"`."""
    candidate = value.strip()
    if candidate.startswith("W/"):
        candidate = candidate[2:]
    candidate = candidate.strip('"')
    if not candidate.isdigit():
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"Некорректный {IF_MATCH_HEADER}: ожидается версия сущности",
            status_code=422,
        )
    return int(candidate)
