"""Service-account authentication and quota helpers for price imports."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthUser, access_user_from_request
from app.config import Settings, settings
from app.db import get_session
from app.models import PriceImport, PriceImportStatus, ServiceClient


KEY_SCHEME = "plk"
INTERNAL_UI_KEY_PREFIX = "internal-ui"


@dataclass(frozen=True)
class ServicePrincipal:
    client_id: uuid.UUID
    name: str


def _pepper(config: Settings = settings) -> bytes:
    value = config.service_api_key_pepper or config.app_auth_secret
    if not value or len(value) < 32:
        raise HTTPException(status_code=503, detail="SERVICE_API_KEY_PEPPER не настроен")
    return value.encode()


def api_key_digest(raw_key: str, config: Settings = settings) -> str:
    return hmac.new(_pepper(config), raw_key.encode(), hashlib.sha256).hexdigest()


def generate_service_api_key(config: Settings = settings) -> tuple[str, str, str]:
    prefix = secrets.token_hex(6)
    raw_key = f"{KEY_SCHEME}_{prefix}_{secrets.token_urlsafe(32)}"
    return raw_key, prefix, api_key_digest(raw_key, config)


def webhook_secret_for_client(client_id: uuid.UUID, config: Settings = settings) -> str:
    return hmac.new(
        _pepper(config), f"price-import-webhook:{client_id}".encode(), hashlib.sha256
    ).hexdigest()


def _prefix(raw_key: str) -> str | None:
    parts = raw_key.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_SCHEME or not parts[1]:
        return None
    return parts[1]


async def authenticate_service_key(
    request: Request,
    session: AsyncSession,
    config: Settings = settings,
) -> ServiceClient:
    if not config.price_import_service_enabled:
        raise HTTPException(status_code=503, detail="Price Import Service отключён")
    scheme, _, raw_key = request.headers.get("Authorization", "").partition(" ")
    prefix = _prefix(raw_key) if scheme.casefold() == "bearer" else None
    if prefix is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется service key")
    client = (
        await session.execute(select(ServiceClient).where(ServiceClient.key_prefix == prefix))
    ).scalar_one_or_none()
    supplied = api_key_digest(raw_key, config)
    expected = client.key_digest if client is not None else "0" * 64
    valid = hmac.compare_digest(supplied, expected)
    if client is None or not client.is_active or not valid:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Service key недействителен")
    client.last_used_at = datetime.now(timezone.utc)
    request.state.service_principal = ServicePrincipal(client.id, client.name)
    return client


async def get_service_client(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ServiceClient:
    return await authenticate_service_key(request, session)


async def get_import_reader(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ServiceClient | AuthUser:
    """Allow scoped service keys and authenticated UI users to read jobs."""

    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.casefold() == "bearer" and _prefix(token) is not None:
        return await authenticate_service_key(request, session)
    return access_user_from_request(request)


async def _internal_ui_client(session: AsyncSession) -> ServiceClient:
    client = (
        await session.execute(select(ServiceClient).where(ServiceClient.is_internal.is_(True)))
    ).scalar_one_or_none()
    if client is not None:
        return client
    try:
        async with session.begin_nested():
            client = ServiceClient(
                name="Внутренний интерфейс",
                key_prefix=INTERNAL_UI_KEY_PREFIX,
                key_digest=api_key_digest("internal-ui-key-disabled"),
                is_internal=True,
                is_active=False,
            )
            session.add(client)
            await session.flush()
        return client
    except IntegrityError:
        # A concurrent first UI request created the single internal scope.
        return (
            await session.execute(
                select(ServiceClient).where(ServiceClient.is_internal.is_(True))
            )
        ).scalar_one()


async def internal_service_client(session: AsyncSession) -> ServiceClient:
    """Внутренний UI-скоуп для импортов без внешнего сервисного ключа.

    Импорт файла из Bitrix24 инициирует менеджер в интерфейсе, поэтому
    сервисного клиента у него нет. Прайсу же нужен устойчивый скоуп
    идемпотентности — им служит единый внутренний клиент.
    """
    return await _internal_ui_client(session)


async def get_import_writer(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ServiceClient:
    """Resolve an external service scope or the authenticated admin UI scope."""

    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.casefold() == "bearer" and _prefix(token) is not None:
        return await authenticate_service_key(request, session)
    user = access_user_from_request(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Загрузка прайса доступна администратору")
    return await _internal_ui_client(session)


async def get_import_admin(request: Request) -> AuthUser:
    """Require a browser-admin JWT for review mutations.

    Service keys deliberately cannot edit shared supplier parsing rules.
    The price-import prefix bypasses the global middleware, so the dependency
    must authenticate explicitly instead of trusting ``request.state``.
    """

    user = access_user_from_request(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Проверка прайса доступна администратору")
    return user


async def enforce_service_quotas(client: ServiceClient, session: AsyncSession) -> None:
    active = (
        await session.execute(
            select(func.count(PriceImport.id)).where(
                PriceImport.service_client_id == client.id,
                PriceImport.status.not_in(PriceImportStatus.terminal_values()),
            )
        )
    ).scalar_one()
    if active >= client.max_concurrent_imports:
        raise HTTPException(status_code=429, detail="Превышен лимит одновременных импортов")

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    key = f"price-import:rate:{client.id}:{int(datetime.now(timezone.utc).timestamp() // 60)}"
    try:
        try:
            count = await redis.incr(key)
            if count == 1:
                await redis.expire(key, 120)
            if count > client.requests_per_minute:
                raise HTTPException(status_code=429, detail="Превышен лимит запросов")
        except RedisError as exc:
            raise HTTPException(
                status_code=503, detail="Хранилище квот временно недоступно"
            ) from exc
    finally:
        await redis.aclose()


async def scoped_import(
    session: AsyncSession,
    principal: ServiceClient | AuthUser,
    import_id: uuid.UUID,
) -> PriceImport:
    """Найти импорт в пределах видимости вызывающего.

    Сервисный клиент видит только собственные задания, поэтому чужой
    `import_id` неотличим от несуществующего — иначе по коду ответа можно было
    бы перебирать чужие идентификаторы.
    """

    conditions = [PriceImport.id == import_id]
    if isinstance(principal, ServiceClient):
        conditions.append(PriceImport.service_client_id == principal.id)
    price_import = (
        await session.execute(select(PriceImport).where(*conditions))
    ).scalar_one_or_none()
    if price_import is None:
        raise HTTPException(status_code=404, detail="Импорт не найден")
    return price_import
