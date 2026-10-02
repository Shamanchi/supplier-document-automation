"""Сценарии аутентификации: вход, ротация сессии, выход, текущий пользователь.

Перенесено из supplier-document-automation (docs/migration/02-backend-identity.md).
Единственная адаптация: секрет подписи ищется по цепочке env фазы 2
(`phase3_auth_secret` → `auth_secret` → `app_auth_secret`), чтобы единый
identity принимал и старые env-файлы фазы 2, и env фазы 3.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.errors import ApiError
from app.modules.identity.models import RefreshSession, User
from app.modules.identity.security import (
    AccessClaims,
    AuthErrorCode,
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    needs_rehash,
    verify_password,
)


@dataclass(frozen=True)
class IssuedSession:
    """Результат входа или ротации: токены и срок жизни access-токена."""

    access_token: str
    expires_in: int
    refresh_token: str
    refresh_expires_at: datetime
    user: User


def signing_secret(settings: Settings) -> str:
    """Секрет подписи JWT: цепочка env фазы 2, см. докстринг модуля."""
    return (
        settings.phase3_auth_secret
        or settings.auth_secret
        or settings.app_auth_secret
        or ""
    )


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    # --- вход ---------------------------------------------------------

    async def login(self, email: str, password: str) -> IssuedSession:
        user = await self._find_by_email(email)

        # Пароль проверяется даже для несуществующего пользователя: одинаковое
        # время ответа не даёт определить, какие email зарегистрированы.
        password_ok = verify_password(password, user.password_hash if user else None)
        if user is None or not password_ok:
            raise ApiError(
                AuthErrorCode.INVALID_CREDENTIALS,
                "Неверный email или пароль",
                status_code=401,
            )
        if not user.is_active:
            raise ApiError(
                AuthErrorCode.USER_DISABLED,
                "Учётная запись отключена",
                status_code=403,
            )

        if user.password_hash and needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)

        return await self._issue(user, family_id=uuid.uuid4())

    # --- ротация ------------------------------------------------------

    async def refresh(self, refresh_token: str) -> IssuedSession:
        """Ротация с обнаружением повторного использования.

        Предъявление уже ротированного токена означает, что им завладел кто-то
        ещё: отзываем всю цепочку (family), а не только этот токен.
        """
        token_hash = hash_refresh_token(refresh_token)
        stored = await self._session.scalar(
            select(RefreshSession).where(RefreshSession.token_hash == token_hash)
        )
        if stored is None:
            raise ApiError(
                AuthErrorCode.INVALID_REFRESH_TOKEN,
                "Сессия недействительна",
                status_code=401,
            )

        if stored.is_revoked:
            await self._revoke_family(stored.family_id)
            raise ApiError(
                AuthErrorCode.REFRESH_TOKEN_REUSED,
                "Обнаружено повторное использование сессии, все сессии отозваны",
                status_code=401,
            )

        if stored.expires_at <= datetime.now(UTC):
            raise ApiError(
                AuthErrorCode.INVALID_REFRESH_TOKEN,
                "Сессия истекла",
                status_code=401,
            )

        user = await self._session.get(User, stored.user_id)
        if user is None or not user.is_active:
            await self._revoke_family(stored.family_id)
            raise ApiError(
                AuthErrorCode.USER_DISABLED,
                "Учётная запись отключена",
                status_code=403,
            )

        stored.revoked_at = datetime.now(UTC)
        return await self._issue(user, family_id=stored.family_id)

    # --- выход --------------------------------------------------------

    async def logout(self, refresh_token: str | None) -> None:
        """Идемпотентен: неизвестный или отсутствующий токен — не ошибка."""
        if not refresh_token:
            return
        await self._session.execute(
            update(RefreshSession)
            .where(
                RefreshSession.token_hash == hash_refresh_token(refresh_token),
                RefreshSession.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )

    async def logout_all(self, user_id: uuid.UUID) -> None:
        await self._session.execute(
            update(RefreshSession)
            .where(
                RefreshSession.user_id == user_id,
                RefreshSession.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )

    # --- текущий пользователь ------------------------------------------

    async def get_active_user(self, user_id: uuid.UUID) -> User:
        """Активность проверяется по БД, а не по claims: роль могли отозвать."""
        user = await self._session.get(User, user_id)
        if user is None or not user.is_active:
            raise ApiError(
                AuthErrorCode.UNAUTHENTICATED,
                "Требуется вход",
                status_code=401,
            )
        return user

    # --- внутреннее ----------------------------------------------------

    async def _find_by_email(self, email: str) -> User | None:
        return await self._session.scalar(
            select(User).where(User.email == email.strip())
        )

    async def _issue(self, user: User, *, family_id: uuid.UUID) -> IssuedSession:
        session_id = uuid.uuid4()
        access_token, expires_in = create_access_token(
            AccessClaims(
                user_id=user.id,
                session_id=session_id,
                roles=tuple(user.role_names),
            ),
            secret=signing_secret(self._settings),
            expires_minutes=self._settings.access_token_expires_minutes,
        )
        refresh_token = generate_refresh_token()
        expires_at = datetime.now(UTC) + timedelta(
            days=self._settings.refresh_token_expires_days
        )
        self._session.add(
            RefreshSession(
                id=session_id,
                user_id=user.id,
                token_hash=hash_refresh_token(refresh_token),
                family_id=family_id,
                issued_at=datetime.now(UTC),
                expires_at=expires_at,
            )
        )
        await self._session.flush()
        return IssuedSession(
            access_token=access_token,
            expires_in=expires_in,
            refresh_token=refresh_token,
            refresh_expires_at=expires_at,
            user=user,
        )

    async def _revoke_family(self, family_id: uuid.UUID) -> None:
        await self._session.execute(
            update(RefreshSession)
            .where(
                RefreshSession.family_id == family_id,
                RefreshSession.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )
