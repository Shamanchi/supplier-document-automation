"""Маршруты `/auth` (docs/api/auth/README.md, фаза 3).

Мерж канона фазы 3 с роутами фазы 2 (docs/migration/02-backend-identity.md):
  * канон — фаза 3: httpOnly refresh-cookie, rate limiter, `CurrentUserResponse`
    как источник истины о пользователе;
  * совместимость фазы 2: login принимает `username` как синоним `email`
    (P2-фронт отправляет username), `/auth/me` отдаёт dual-format
    (`display_name` и `displayName`).

Формат ошибок — envelope фазы 3: путь `/api/v1/auth` не входит в
`PHASE2_PREFIXES`, а клиент фазы 2 читает и `payload.message` (см. его
`extractPayloadMessage`), так что оба фронта понимают ответ.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Request, Response, status

from app.core.errors import ApiError
from app.modules.identity.dependencies import (
    REFRESH_COOKIE_NAME,
    REFRESH_COOKIE_PATH,
    AuthServiceDep,
    CurrentUser,
    PermissionsServiceDep,
    SettingsDep,
)
from app.modules.identity.models import User
from app.modules.identity.permissions import PermissionsService
from app.modules.identity.schemas import (
    CurrentUserResponse,
    LoginRequest,
    SessionResponse,
)
from app.modules.identity.security import AuthErrorCode, LoginRateLimiter
from app.modules.identity.service import IssuedSession

router = APIRouter(prefix="/auth", tags=["auth"])
_limiter = LoginRateLimiter()


def _set_refresh_cookie(
    response: Response, issued: IssuedSession, settings: SettingsDep
) -> None:
    """Refresh-токен живёт только в httpOnly cookie.

    `path` ограничивает отправку маршрутами `/auth`, `samesite=strict` защищает
    от CSRF: сторонний сайт не сможет инициировать ротацию сессии.
    """
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        issued.refresh_token,
        max_age=settings.refresh_token_expires_days * 86400,
        httponly=True,
        secure=settings.app_auth_secure_cookies,
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
    )


def initials_of(user: User) -> str:
    """Инициалы для аватара: первые буквы двух слов имени."""
    initials = "".join(part[:1].upper() for part in user.display_name.split()[:2])
    return initials or user.email[:2].upper()


def _primary_role(roles: list[str]) -> str:
    """Роль для UI фазы 2: admin/sysadmin — инженерный доступ."""
    if set(roles) & {"admin", "sysadmin"}:
        return "admin"
    return roles[0] if roles else "viewer"


async def current_user_payload(
    user: User, permissions: PermissionsService
) -> CurrentUserResponse:
    """Единственный источник истины о текущем пользователе.

    Экран профиля собирает свою карточку из этих же данных (profile/README.md):
    второй источник расходился бы с `/auth/me` при первой же правке прав.
    """
    roles = user.role_names
    display_name = user.display_name or user.email
    return CurrentUserResponse(
        id=user.id,
        email=user.email,
        display_name=display_name,
        displayName=display_name,
        initials=initials_of(user),
        username=user.email,
        roles=roles,
        role=_primary_role(roles),
        permissions=await permissions.map_for(roles, full_map="sysadmin" in roles),
    )


@router.post("/login", response_model=SessionResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    service: AuthServiceDep,
    settings: SettingsDep,
) -> SessionResponse:
    identity = payload.email or payload.username
    if not identity:
        raise ApiError(
            AuthErrorCode.INVALID_CREDENTIALS,
            "email или username обязателен",
            status_code=422,
        )
    client = request.client.host if request.client else "unknown"
    key = f"{client}:{identity.casefold()}"
    _limiter.check(key)
    try:
        issued = await service.login(identity, payload.password)
    except Exception:
        _limiter.fail(key)
        raise
    _limiter.reset(key)
    _set_refresh_cookie(response, issued, settings)
    return SessionResponse(
        access_token=issued.access_token, expires_in=issued.expires_in
    )


@router.post("/refresh", response_model=SessionResponse)
async def refresh(
    response: Response,
    service: AuthServiceDep,
    settings: SettingsDep,
    docprice_refresh: Annotated[str | None, Cookie(alias=REFRESH_COOKIE_NAME)] = None,
) -> SessionResponse:
    """Обновление access-токена по cookie.

    Тело запроса не читается: токен приходит только в httpOnly cookie.
    """
    if not docprice_refresh:
        raise ApiError(
            AuthErrorCode.INVALID_REFRESH_TOKEN, "Сессия истекла", status_code=401
        )
    issued = await service.refresh(docprice_refresh)
    _set_refresh_cookie(response, issued, settings)
    return SessionResponse(
        access_token=issued.access_token, expires_in=issued.expires_in
    )


# response_model=None обязателен при 204: иначе FastAPI выводит модель ответа из
# аннотации `-> None` и падает на проверке «204 не может иметь тела».
@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def logout(
    response: Response,
    service: AuthServiceDep,
    docprice_refresh: Annotated[str | None, Cookie(alias=REFRESH_COOKIE_NAME)] = None,
) -> None:
    """Идемпотентен: повторный выход и выход без сессии не считаются ошибкой."""
    await service.logout(docprice_refresh)
    response.delete_cookie(REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def logout_all(
    response: Response,
    user: CurrentUser,
    service: AuthServiceDep,
) -> None:
    await service.logout_all(user.id)
    response.delete_cookie(REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH)


@router.get("/me", response_model=CurrentUserResponse)
async def me(
    user: CurrentUser, permissions: PermissionsServiceDep
) -> CurrentUserResponse:
    return await current_user_payload(user, permissions)
