"""Хранилище ответов по `Idempotency-Key` (§18.7 PHASE3_CANONICAL_ARCHITECTURE.md).

Правила:

| Сценарий | Поведение |
|---|---|
| ключ не найден | выполнить операцию и сохранить ответ |
| ключ найден, `request_hash` совпадает | вернуть сохранённый ответ, не выполняя |
| ключ найден, `request_hash` иной | `422 IDEMPOTENCY_KEY_CONFLICT` |
| ключ занят, ответа ещё нет | `409 IDEMPOTENCY_KEY_IN_PROGRESS` |
| `expires_at` истёк | ключ считается новым |

Ключ занимается ДО выполнения операции (`INSERT ... ON CONFLICT DO NOTHING`),
иначе два одновременных запроса успевают выполниться оба. Занятие живёт в
отдельной транзакции: если бы оно откатывалось вместе с ошибкой операции,
защиты от параллельного дубля не было бы.

Расхождение контрактов: `docs/api/check-batches/README.md` называет конфликт
`409 IDEMPOTENCY_CONFLICT`, канон §18.7 — `422 IDEMPOTENCY_KEY_CONFLICT`.
Реализовано по канону; расхождение отмечено в отчёте как вопрос к согласованию.

Перенесено из supplier-document-automation без изменений (docs/migration/01-backend-core.md).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError, ErrorCode

#: Срок жизни записи. По истечении ключ считается новым (§18.7).
KEY_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class StoredResponse:
    """Сохранённый ответ успешно выполненной операции."""

    status_code: int
    body: dict[str, Any]


def request_hash(method: str, path: str, body: Any) -> str:
    """SHA-256 от метода, пути и тела запроса.

    Тело сериализуется с сортировкой ключей: порядок полей в JSON не должен
    превращать повтор в конфликт.
    """
    payload = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{method} {path} {payload}".encode()).hexdigest()


async def idempotent(
    store: IdempotencyStore,
    key: str,
    digest: str,
    status_code: int,
    operation: Callable[[], Awaitable[dict[str, Any]]],
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Выполняет операцию не более одного раза на ключ.

    Ответ формируется здесь, а не через `response_model`: сохранённое тело нужно
    вернуть повтору байт-в-байт, а FastAPI прогоняет модель заново и не знает
    про сохранённый ответ.
    """
    stored = await store.claim(key, digest)
    if stored is not None:
        return JSONResponse(status_code=stored.status_code, content=stored.body)

    try:
        body = await operation()
    except Exception:
        # Ключ освобождается, иначе повтор после сбоя все 24 часа получал бы
        # «операция уже выполняется», хотя не выполнилось ничего.
        await store.release(key)
        raise

    await store.save(key, status_code, body)
    return JSONResponse(status_code=status_code, content=body, headers=headers)


class IdempotencyStore:
    """Доступ к таблице `idempotency_keys`.

    Работает в переданной сессии, но занятие ключа коммитит отдельно — см.
    docstring модуля.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(self, key: str, digest: str) -> StoredResponse | None:
        """Занимает ключ под операцию либо возвращает готовый ответ.

        `None` означает «ключ наш, операцию нужно выполнить».
        """
        await self._drop_if_expired(key)

        claimed = await self._session.execute(
            text(
                "INSERT INTO idempotency_keys (key, request_hash, expires_at)"
                " VALUES (:key, :digest, :expires_at)"
                " ON CONFLICT (key) DO NOTHING"
                " RETURNING key"
            ),
            {
                "key": key,
                "digest": digest,
                "expires_at": datetime.now(UTC) + KEY_TTL,
            },
        )
        if claimed.first() is not None:
            # Коммит немедленный: занятие должно быть видно параллельным
            # запросам до того, как эта операция завершится.
            await self._session.commit()
            return None

        return self._existing(await self._load(key), digest)

    async def save(self, key: str, status_code: int, body: dict[str, Any]) -> None:
        """Фиксирует ответ выполненной операции за уже занятым ключом."""
        await self._session.execute(
            text(
                "UPDATE idempotency_keys"
                " SET response_status = :status, response_body = CAST(:body AS jsonb)"
                " WHERE key = :key"
            ),
            {
                "key": key,
                "status": status_code,
                "body": json.dumps(body, ensure_ascii=False, default=str),
            },
        )

    async def release(self, key: str) -> None:
        """Снимает занятие, если операция завершилась ошибкой.

        Без этого повтор после сбоя упирался бы в «ключ занят» все 24 часа,
        хотя ничего не выполнилось.

        Откат обязателен до удаления: после ошибки БД транзакция помечена
        сбойной, и любой следующий запрос в ней тоже упал бы. Заодно откат
        снимает частичные записи неудавшейся операции.
        """
        await self._session.rollback()
        await self._session.execute(
            text(
                "DELETE FROM idempotency_keys"
                " WHERE key = :key AND response_status IS NULL"
            ),
            {"key": key},
        )
        await self._session.commit()

    async def purge_expired(self) -> int:
        """Retention: удаляет истёкшие ключи. Возвращает число удалённых строк."""
        result = await self._session.execute(
            text("DELETE FROM idempotency_keys WHERE expires_at < now()")
        )
        return result.rowcount or 0

    # --- внутреннее ----------------------------------------------------

    async def _load(self, key: str) -> tuple[str, int | None, Any] | None:
        row = await self._session.execute(
            text(
                "SELECT request_hash, response_status, response_body"
                " FROM idempotency_keys WHERE key = :key"
            ),
            {"key": key},
        )
        return row.first()

    async def _drop_if_expired(self, key: str) -> None:
        await self._session.execute(
            text(
                "DELETE FROM idempotency_keys WHERE key = :key AND expires_at < now()"
            ),
            {"key": key},
        )

    def _existing(
        self, row: tuple[str, int | None, Any] | None, digest: str
    ) -> StoredResponse:
        if row is None:
            # Ключ исчез между INSERT и SELECT: параллельный retry снял занятие
            # после своей ошибки. Для клиента это «попробуйте снова».
            raise ApiError(
                "IDEMPOTENCY_KEY_IN_PROGRESS",
                "Операция с этим Idempotency-Key ещё выполняется",
                status_code=409,
            )

        stored_hash, status_code, body = row
        if stored_hash != digest:
            raise ApiError(
                ErrorCode.IDEMPOTENCY_KEY_CONFLICT,
                "Idempotency-Key уже использован для другого запроса",
                status_code=422,
            )
        if status_code is None:
            raise ApiError(
                "IDEMPOTENCY_KEY_IN_PROGRESS",
                "Операция с этим Idempotency-Key ещё выполняется",
                status_code=409,
            )
        return StoredResponse(status_code=status_code, body=body)
