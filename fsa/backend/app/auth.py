"""Аутентификация контура: общий токен и сервисные ключи по потребителю.

Два способа сосуществуют намеренно. Общий токен (`FSA_API_TOKEN`) остаётся для
интерфейса и разовых скриптов — им пользуются люди. Сервисные ключи выдаются
соседним сервисам: их можно отозвать поштучно и ограничить областью прав, а
общий токен ни того, ни другого не умеет.

Формат заголовка один и тот же — `Authorization: Bearer <значение>`, — поэтому
клиенту не нужно знать, каким из двух способов он аутентифицируется.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings
from app.db import get_session
from app.service_keys import (
    SCOPE_ADMIN,
    SCOPE_DOCUMENTS,
    SCOPE_READ,
    Principal,
    ServiceKeyService,
    root_principal,
)

UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Требуется заголовок Authorization: Bearer <сервисный ключ или FSA_API_TOKEN>",
    headers={"WWW-Authenticate": "Bearer"},
)


def token_is_valid(presented: str, config: Settings = settings) -> bool:
    expected = (config.api_token or "").strip()
    if not expected:
        # Незаполненный токен закрывает контур, а не открывает его.
        return False
    return secrets.compare_digest(presented.strip(), expected)


def _presented(authorization: str) -> str:
    scheme, _, value = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise UNAUTHORIZED
    return value.strip()


async def current_principal(
    authorization: str = Header(default=""),
    session: AsyncSession = Depends(get_session),
) -> Principal:
    """Кто пришёл: владелец общего токена или сервисный клиент со своими правами."""
    presented = _presented(authorization)
    try:
        import jwt
        payload = jwt.decode(presented, settings.phase3_auth_secret, algorithms=["HS256"], issuer=settings.phase3_auth_issuer, audience=settings.phase3_auth_audience)
        roles = set(payload.get("roles", []))
        if payload.get("type") == "access" and roles:
            name = str(payload.get("sub", "user"))
            if "sysadmin" in roles:
                return Principal(name=name, scopes=frozenset({SCOPE_READ, SCOPE_DOCUMENTS, SCOPE_ADMIN}))
            if "admin" in roles:
                return Principal(name=name, scopes=frozenset({SCOPE_READ, SCOPE_DOCUMENTS}))
            if roles & {"manager", "viewer"}:
                return Principal(name=name, scopes=frozenset({SCOPE_READ}))
            raise UNAUTHORIZED
    except Exception:
        pass
    if token_is_valid(presented):
        return root_principal()
    principal = await ServiceKeyService(session).authenticate(presented)
    if principal is None:
        raise UNAUTHORIZED
    # Отметка о последнем использовании — единственная запись на этом пути;
    # без коммита она уйдёт вместе с сессией запроса.
    await session.commit()
    return principal


async def require_token(_: Principal = Depends(current_principal)) -> None:
    """Совместимость: «пустил или нет», без разбора прав."""


def require_scope(scope: str):
    """Зависимость, требующая конкретной области прав."""

    async def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.allows(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"ключу «{principal.name}» не выдана область «{scope}»",
            )
        return principal

    return dependency


require_read = require_scope(SCOPE_READ)
require_documents = require_scope(SCOPE_DOCUMENTS)
require_admin = require_scope(SCOPE_ADMIN)
