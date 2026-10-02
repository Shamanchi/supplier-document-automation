"""Пароли, JWT и refresh-токены.

Контракт — docs/api/auth/README.md §Security (фаза 3):
  * пароли только как Argon2id hash;
  * JWT содержит subject, session ID, время выпуска/истечения и минимум claims;
  * refresh-токен непрозрачен, в БД хранится только его хеш;
  * токены и пароли не попадают в логи.

Приёмы заимствованы из реализации Фазы 2: структура JWT с iss/aud/jti и
ограничитель попыток входа.

Перенесено из supplier-document-automation без изменений (docs/migration/02-backend-identity.md).
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.errors import ApiError

TOKEN_ISSUER = "supplier-document-automation"
TOKEN_AUDIENCE = "supplier-document-automation-ui"
ALGORITHM = "HS256"
TokenType = Literal["access"]

_hasher = PasswordHasher()


class AuthErrorCode:
    """Коды ошибок аутентификации (docs/api/auth/README.md §Ошибки)."""

    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    ACCESS_TOKEN_EXPIRED = "ACCESS_TOKEN_EXPIRED"
    INVALID_ACCESS_TOKEN = "INVALID_ACCESS_TOKEN"
    INVALID_REFRESH_TOKEN = "INVALID_REFRESH_TOKEN"
    REFRESH_TOKEN_REUSED = "REFRESH_TOKEN_REUSED"
    USER_DISABLED = "USER_DISABLED"
    LOGIN_RATE_LIMITED = "LOGIN_RATE_LIMITED"
    UNAUTHENTICATED = "UNAUTHENTICATED"


@dataclass(frozen=True)
class AccessClaims:
    """Полезная нагрузка access-токена."""

    user_id: uuid.UUID
    session_id: uuid.UUID
    roles: tuple[str, ...]


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Проверяет пароль, не раскрывая существование учётной записи.

    Для отсутствующего пользователя вызывается та же операция над фиктивным
    хешем: иначе разница во времени ответа выдала бы наличие email.
    """
    if not password_hash:
        _hasher.hash(password)  # выравниваем время ответа
        return False
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def needs_rehash(password_hash: str) -> bool:
    """Параметры Argon2 усилились — хеш стоит пересчитать при следующем входе."""
    return _hasher.check_needs_rehash(password_hash)


def create_access_token(
    claims: AccessClaims, *, secret: str, expires_minutes: int
) -> tuple[str, int]:
    """Возвращает токен и его время жизни в секундах."""
    now = datetime.now(UTC)
    lifetime = timedelta(minutes=expires_minutes)
    token = jwt.encode(
        {
            "sub": str(claims.user_id),
            "sid": str(claims.session_id),
            "roles": list(claims.roles),
            "type": "access",
            "iss": TOKEN_ISSUER,
            "aud": TOKEN_AUDIENCE,
            "iat": now,
            "exp": now + lifetime,
            "jti": uuid.uuid4().hex,
        },
        secret,
        algorithm=ALGORITHM,
    )
    return token, int(lifetime.total_seconds())


def decode_access_token(token: str, *, secret: str) -> AccessClaims:
    """Разбирает access-токен; истёкший и повреждённый различаются кодом ошибки."""
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            issuer=TOKEN_ISSUER,
            audience=TOKEN_AUDIENCE,
            options={"require": ["sub", "sid", "type", "iat", "exp", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise ApiError(
            AuthErrorCode.ACCESS_TOKEN_EXPIRED,
            "Сессия истекла",
            status_code=401,
        ) from exc
    except jwt.PyJWTError as exc:
        raise ApiError(
            AuthErrorCode.INVALID_ACCESS_TOKEN,
            "Сессия недействительна",
            status_code=401,
        ) from exc

    if payload.get("type") != "access":
        raise ApiError(
            AuthErrorCode.INVALID_ACCESS_TOKEN,
            "Сессия недействительна",
            status_code=401,
        )
    return AccessClaims(
        user_id=uuid.UUID(payload["sub"]),
        session_id=uuid.UUID(payload["sid"]),
        roles=tuple(payload.get("roles", ())),
    )


def generate_refresh_token() -> str:
    """Непрозрачный токен: не несёт claims, проверяется только по БД."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """SHA-256 достаточно: токен и так высокоэнтропийный, подбор невозможен."""
    return hashlib.sha256(token.encode()).hexdigest()


class LoginRateLimiter:
    """Ограничитель попыток входа на процесс.

    Заимствован из Фазы 2. Ограничение осознанное: при нескольких воркерах счёт
    ведётся раздельно, поэтому общий лимит кратен их числу. Для распределённого
    лимита нужен Redis — это отдельная задача, а не молчаливое допущение.
    """

    def __init__(self, attempts: int = 5, window_seconds: int = 300) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            recent = [
                stamp
                for stamp in self._failures.get(key, [])
                if now - stamp < self.window_seconds
            ]
            self._failures[key] = recent
            if len(recent) >= self.attempts:
                raise ApiError(
                    AuthErrorCode.LOGIN_RATE_LIMITED,
                    "Слишком много попыток входа. Повторите позже",
                    status_code=429,
                )

    def fail(self, key: str) -> None:
        with self._lock:
            self._failures.setdefault(key, []).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
