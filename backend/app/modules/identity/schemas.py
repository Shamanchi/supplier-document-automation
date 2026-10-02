"""Схемы модуля auth (docs/api/auth/README.md, фаза 3).

Перенесено из supplier-document-automation (docs/migration/02-backend-identity.md).
Адаптация для фазы 2 (docs/migration/06-frontend-order-page.md): пока фронтенд
фазы 2 не переведён на camelCase-контракт, схемы отдают оба написания ключей
(`accessToken`/`expiresIn` и `display_name`) — P2-клиент читает snake_case +
camelCase в login, P3-клиент — camelCase.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.schemas import BaseSchema


class LoginRequest(BaseSchema):
    """Login: принимает email либо username (P2-фронт отправляет username)."""

    email: EmailStr | None = None
    username: str | None = Field(default=None, min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=512)


class SessionResponse(BaseSchema):
    """Ответ login и refresh.

    Refresh-токен в теле НЕ возвращается: он уходит в httpOnly cookie, чтобы
    его не мог прочитать JS при XSS. Клиент хранит только access-токен в памяти
    и обновляет его через `POST /auth/refresh` (cookie отправляется браузером).

    Ключи сериализуются camelCase (`accessToken`, `expiresIn`) — это контракт
    фазы 3, и его же ждёт клиент фазы 2 (`AuthSession`), расхождений нет.
    """

    access_token: str
    token_type: str = "bearer"
    expires_in: int


class CurrentUserResponse(BaseModel):
    """Источник истины о текущем пользователе для route guards и меню.

    Переходный dual-format (шаг миграции 02): фронтенд фазы 2 читает
    `display_name`, фронтенд фазы 3 — `displayName`. После перевода P2-фронтенда
    на camelCase-контракт (шаг 06) snake-дубликат удаляется и схема возвращается
    к канону BaseSchema.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: UUID
    email: str
    display_name: str
    displayName: str
    initials: str
    username: str = ""
    roles: list[str]
    role: str
    permissions: dict[str, list[str]]
