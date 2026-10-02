"""Сервисные ключи: выпуск, проверка, области прав.

Ключ выдаётся потребителю — соседнему сервису, скрипту, интерфейсу — а не
контуру целиком. Это даёт две вещи, которых не было у общего токена: отзыв
одного потребителя без перевыпуска остальных и ограничение прав. Клиенту
прайсов нужно читать реестр и получать привязку; удалять документы и запускать
импорт реестра ему незачем, и теперь он этого не может.

В базе лежит HMAC-хэш ключа с pepper из окружения, а не сам ключ: утечка дампа
базы не даёт доступа к API. Открыто хранится только префикс — по нему ключ
находится за одно сравнение, без перебора хэшей всех клиентов.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings
from app.models import ServiceClient

KEY_SCHEME = "fsa"

# Областей ровно три, и они соответствуют трём разным последствиям ошибки:
# прочитать лишнее, испортить документ, испортить весь реестр.
SCOPE_READ = "read"
SCOPE_DOCUMENTS = "documents"
SCOPE_ADMIN = "admin"
ALL_SCOPES = (SCOPE_READ, SCOPE_DOCUMENTS, SCOPE_ADMIN)

# Административный ключ включает остальные: раздельные списки для одного и
# того же человека — источник ошибок при выдаче, а не защита.
IMPLIED: dict[str, tuple[str, ...]] = {
    SCOPE_ADMIN: (SCOPE_ADMIN, SCOPE_DOCUMENTS, SCOPE_READ),
    SCOPE_DOCUMENTS: (SCOPE_DOCUMENTS, SCOPE_READ),
    SCOPE_READ: (SCOPE_READ,),
}


class ServiceKeyError(ValueError):
    """Некорректный запрос на выпуск ключа."""


@dataclass(frozen=True, slots=True)
class Principal:
    """Кто пришёл: сервисный клиент или владелец общего токена."""

    name: str
    scopes: frozenset[str]
    client_id: uuid.UUID | None = None

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


# Общий токен из окружения остаётся полноправным: им пользуются интерфейс и
# разовые скрипты, и отбирать у них доступ этой правкой не нужно.
def root_principal() -> Principal:
    return Principal(name="root-token", scopes=frozenset(ALL_SCOPES))


def _pepper(config: Settings = settings) -> bytes:
    value = (config.service_api_key_pepper or "").strip()
    if len(value) < 32:
        raise ServiceKeyError(
            "SERVICE_API_KEY_PEPPER не задан или короче 32 символов — "
            "сервисные ключи выключены"
        )
    return value.encode()


def key_digest(raw_key: str, config: Settings = settings) -> str:
    return hmac.new(_pepper(config), raw_key.encode(), hashlib.sha256).hexdigest()


def generate_key(config: Settings = settings) -> tuple[str, str, str]:
    """Новый ключ: (сам ключ, префикс, хэш). Сам ключ больше нигде не хранится."""
    prefix = secrets.token_hex(6)
    raw_key = f"{KEY_SCHEME}_{prefix}_{secrets.token_urlsafe(32)}"
    return raw_key, prefix, key_digest(raw_key, config)


def prefix_of(raw_key: str) -> str | None:
    parts = raw_key.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_SCHEME or not parts[1]:
        return None
    return parts[1]


def normalize_scopes(scopes: str | list[str]) -> str:
    """Список областей → нормальная форма для хранения."""
    if isinstance(scopes, str):
        items = [item.strip() for item in scopes.split(",")]
    else:
        items = [str(item).strip() for item in scopes]
    chosen = [item for item in items if item]
    if not chosen:
        raise ServiceKeyError("нужна хотя бы одна область прав")
    unknown = sorted(set(chosen) - set(ALL_SCOPES))
    if unknown:
        raise ServiceKeyError(f"неизвестные области: {', '.join(unknown)}")
    return ",".join(sorted(set(chosen)))


def expand_scopes(stored: str) -> frozenset[str]:
    """Хранимые области + всё, что они подразумевают."""
    result: set[str] = set()
    for item in stored.split(","):
        result.update(IMPLIED.get(item.strip(), ()))
    return frozenset(result)


class ServiceKeyService:
    def __init__(self, session: AsyncSession, config: Settings = settings) -> None:
        self.session = session
        self.config = config

    async def issue(
        self, *, name: str, scopes: str | list[str], note: str = ""
    ) -> tuple[ServiceClient, str]:
        """Выпустить ключ. Возвращает клиента и сам ключ — он виден один раз."""
        name = name.strip()
        if not name:
            raise ServiceKeyError("нужно имя потребителя")
        existing = await self.session.scalar(
            select(ServiceClient).where(ServiceClient.name == name)
        )
        if existing is not None:
            raise ServiceKeyError(f"потребитель «{name}» уже заведён")
        raw_key, prefix, digest = generate_key(self.config)
        client = ServiceClient(
            name=name,
            key_prefix=prefix,
            key_hash=digest,
            scopes=normalize_scopes(scopes),
            note=note,
        )
        self.session.add(client)
        await self.session.flush()
        return client, raw_key

    async def revoke(self, name: str) -> ServiceClient | None:
        client = await self.session.scalar(
            select(ServiceClient).where(ServiceClient.name == name.strip())
        )
        if client is None:
            return None
        client.is_active = False
        await self.session.flush()
        return client

    async def authenticate(self, raw_key: str) -> Principal | None:
        """Найти клиента по ключу. Неизвестный ключ — это `None`, не исключение."""
        raw_key = (raw_key or "").strip()
        prefix = prefix_of(raw_key)
        if not prefix:
            return None
        client = await self.session.scalar(
            select(ServiceClient).where(ServiceClient.key_prefix == prefix)
        )
        if client is None or not client.is_active:
            return None
        try:
            expected = client.key_hash
            presented = key_digest(raw_key, self.config)
        except ServiceKeyError:
            return None
        # Сравнение постоянного времени: обычное «==» на строках выдаёт длину
        # общего префикса временем ответа.
        if not hmac.compare_digest(expected, presented):
            return None
        client.last_used_at = datetime.now(timezone.utc)
        return Principal(
            name=client.name,
            scopes=expand_scopes(client.scopes),
            client_id=client.id,
        )
