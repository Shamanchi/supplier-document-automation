"""Зависимости аутентификации и авторизации для роутеров.

Перенесено из supplier-document-automation (docs/migration/02-backend-identity.md).
Адаптация: секрет подписи — цепочка env фазы 2 (см. service.signing_secret).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.errors import ApiError
from app.db import SessionLocal
from app.modules.identity.models import User
from app.modules.identity.permissions import PermissionsService
from app.modules.identity.security import AuthErrorCode, decode_access_token
from app.modules.identity.service import AuthService, signing_secret

REFRESH_COOKIE_NAME = "docprice_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Сессия БД с транзакцией на запрос: коммит при успехе, откат при ошибке."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_auth_service(session: SessionDep, settings: SettingsDep) -> AuthService:
    return AuthService(session, settings)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def get_permissions_service(session: SessionDep) -> PermissionsService:
    return PermissionsService(session)


PermissionsServiceDep = Annotated[PermissionsService, Depends(get_permissions_service)]


async def get_current_user(
    service: AuthServiceDep,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """Текущий пользователь по access-токену.

    Активность перепроверяется в БД: между выпуском токена и запросом учётную
    запись могли отключить (контракт auth §Security).
    """
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.casefold() != "bearer" or not token:
        raise ApiError(
            AuthErrorCode.UNAUTHENTICATED,
            "Требуется вход",
            status_code=401,
        )
    claims = decode_access_token(token, secret=signing_secret(settings))
    return await service.get_active_user(claims.user_id)


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*allowed: str):
    """Фабрика зависимости, ограничивающей доступ набором ролей."""

    async def dependency(user: CurrentUser) -> User:
        if not set(user.role_names) & set(allowed):
            raise ApiError(
                "FORBIDDEN",
                "Недостаточно прав для операции",
                status_code=403,
            )
        return user

    return dependency
