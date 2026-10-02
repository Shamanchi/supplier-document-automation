"""Static deployment accounts, signed sessions, and API RBAC middleware."""
from __future__ import annotations

import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from app.config import Settings, settings

Role = Literal["user", "admin"]
TOKEN_ISSUER = "docprice-price-loader"
TOKEN_AUDIENCE = "docprice-price-loader-ui"


@dataclass(frozen=True)
class AuthUser:
    username: str
    role: Role


def _configured_accounts(config: Settings = settings) -> tuple[tuple[AuthUser, str], ...]:
    if not config.app_admin_password or not config.app_user_password:
        raise HTTPException(status_code=503, detail="Аутентификация приложения не настроена")
    if config.app_admin_username == config.app_user_username:
        raise HTTPException(status_code=503, detail="Логины admin и user должны различаться")
    return (
        (AuthUser(config.app_admin_username, "admin"), config.app_admin_password),
        (AuthUser(config.app_user_username, "user"), config.app_user_password),
    )


def _signing_key(config: Settings = settings) -> str:
    if not config.app_auth_secret or len(config.app_auth_secret) < 32:
        raise HTTPException(status_code=503, detail="APP_AUTH_SECRET не настроен")
    return config.app_auth_secret


def authenticate_credentials(username: str, password: str, config: Settings = settings) -> AuthUser | None:
    """Compare every configured account to reduce username enumeration signals."""
    matched: AuthUser | None = None
    for user, expected_password in _configured_accounts(config):
        username_matches = secrets.compare_digest(username.encode(), user.username.encode())
        password_matches = secrets.compare_digest(password.encode(), expected_password.encode())
        if username_matches and password_matches:
            matched = user
    return matched


def create_token(user: AuthUser, token_type: Literal["access", "refresh"], config: Settings = settings) -> str:
    now = datetime.now(UTC)
    lifetime = (
        timedelta(minutes=config.app_access_token_expiry_minutes)
        if token_type == "access"
        else timedelta(days=config.app_refresh_token_expiry_days)
    )
    return jwt.encode(
        {
            "sub": user.username,
            "role": user.role,
            "type": token_type,
            "iss": TOKEN_ISSUER,
            "aud": TOKEN_AUDIENCE,
            "iat": now,
            "exp": now + lifetime,
            "jti": uuid.uuid4().hex,
        },
        _signing_key(config),
        algorithm="HS256",
    )


def decode_token(token: str, expected_type: Literal["access", "refresh"], config: Settings = settings) -> AuthUser:
    try:
        payload = jwt.decode(
            token,
            _signing_key(config),
            algorithms=["HS256"],
            issuer=TOKEN_ISSUER,
            audience=TOKEN_AUDIENCE,
            options={"require": ["sub", "role", "type", "iat", "exp", "jti"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Сессия недействительна") from exc
    if payload.get("type") != expected_type or payload.get("role") not in {"user", "admin"}:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Сессия недействительна")
    user = AuthUser(str(payload["sub"]), payload["role"])
    if not any(
        secrets.compare_digest(user.username.encode(), configured.username.encode())
        and user.role == configured.role
        for configured, _ in _configured_accounts(config)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Сессия недействительна")
    return user


def access_user_from_request(request: Request, config: Settings = settings) -> AuthUser:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.casefold() != "bearer" or not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется вход")
    try:
        from app.modules.identity.service import signing_secret
        from app.modules.identity.security import decode_access_token
        claims = decode_access_token(token, secret=signing_secret(config))
        role = "admin" if set(claims.roles) & {"admin", "sysadmin"} else "user"
        return AuthUser(str(claims.user_id), role)
    except Exception:
        return decode_token(token, "access", config)


def require_admin(request: Request) -> AuthUser:
    """Endpoint-level defence for sensitive reads as well as mutations."""

    user = getattr(request.state, "auth_user", None)
    if not isinstance(user, AuthUser):
        user = access_user_from_request(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Действие доступно только администратору")
    return user


class LoginRateLimiter:
    """Small per-process guard for the single-worker login endpoint."""

    def __init__(self, attempts: int = 5, window_seconds: int = 300) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            recent = [stamp for stamp in self._failures.get(key, []) if now - stamp < self.window_seconds]
            self._failures[key] = recent
            if len(recent) >= self.attempts:
                raise HTTPException(status_code=429, detail="Слишком много попыток входа. Повторите позже")

    def fail(self, key: str) -> None:
        with self._lock:
            self._failures.setdefault(key, []).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


class ApiAuthMiddleware(BaseHTTPMiddleware):
    """Authenticate all application API calls and enforce admin-only writes."""

    public_paths = {
        "/api/v1/health",
        "/api/v1/auth/login",
        "/api/v1/auth/refresh",
        "/api/v1/auth/logout",
    }
    safe_methods = {"GET", "HEAD", "OPTIONS"}
    # Префиксы, где кроме браузерного JWT принимается сервисный ключ. Middleware
    # различить их не может — оба приходят в `Authorization: Bearer`, — поэтому
    # пропускает запрос дальше, а роль проверяет зависимость маршрута
    # (`get_import_reader` / `get_import_admin`). Список перечисляет ровно те
    # префиксы, чьи обработчики такую зависимость объявляют.
    service_prefixes = (
        "/api/v1/price-imports",
        "/api/v1/price-items",
        "/api/v1/price-versions",
        "/api/v1/review",
        "/api/v1/catalog",
    )
    # Контур фазы 3 и заявок: кто может писать, решают зависимости маршрутов
    # (canon identity — `CurrentUser`/`require_roles`). Легаси-роли middleware
    # различают только admin, поэтому без этого списка менеджеру недоступны
    # ни контур заявок (решение №4 задачи №7), ни проверка ТХ, ни профиль.
    # Аутентификация при этом остаётся обязательной — делегируется только
    # проверка права на запись.
    delegated_prefixes = (
        "/api/v1/orders",
        "/api/v1/check-batches",
        "/api/v1/check-items",
        "/api/v1/uploads",
        "/api/v1/certificates",
        "/api/v1/parameters",
        "/api/v1/comparisons",
        "/api/v1/reports",
        "/api/v1/report-artifacts",
        "/api/v1/dictionary",
        "/api/v1/admin/catalogs",
        "/api/v1/admin/synonyms",
        "/api/v1/integrations/bitrix",
        "/api/v1/profile",
        "/api/v1/auth",
    )

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if (
            not request.url.path.startswith("/api/v1/")
            or request.url.path in self.public_paths
            or request.url.path.startswith(self.service_prefixes)
        ):
            return await call_next(request)
        try:
            user = access_user_from_request(request)
            if (
                request.method not in self.safe_methods
                and user.role != "admin"
                and not request.url.path.startswith(self.delegated_prefixes)
            ):
                raise HTTPException(status_code=403, detail="Действие доступно только администратору")
            request.state.auth_user = user
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        return await call_next(request)
