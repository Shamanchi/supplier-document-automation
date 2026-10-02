"""Сервисные ключи: выпуск, проверка, области прав, отзыв."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.service_keys import (
    ServiceKeyError,
    ServiceKeyService,
    expand_scopes,
    generate_key,
    key_digest,
    normalize_scopes,
    prefix_of,
    root_principal,
)

PEPPER = "p" * 40


def config() -> Settings:
    return Settings(
        service_api_key_pepper=PEPPER, database_url="postgresql+asyncpg://x/y"
    )


class TestKeyFormat:
    def test_generated_key_carries_its_prefix(self):
        raw_key, prefix, digest = generate_key(config())
        assert raw_key.startswith("fsa_")
        assert prefix_of(raw_key) == prefix
        assert digest == key_digest(raw_key, config())

    def test_foreign_key_shapes_are_rejected(self):
        # Ключ соседнего контура начинается с `plk_` и сюда попадать не должен.
        assert prefix_of("plk_abc_secret") is None
        assert prefix_of("bare-token") is None

    def test_short_pepper_disables_keys_instead_of_weakening_them(self):
        weak = Settings(service_api_key_pepper="short", database_url="postgresql+asyncpg://x/y")
        with pytest.raises(ServiceKeyError, match="PEPPER"):
            generate_key(weak)


class TestScopes:
    def test_admin_implies_the_rest(self):
        assert expand_scopes("admin") == {"admin", "documents", "read"}

    def test_read_stays_read(self):
        assert expand_scopes("read") == {"read"}

    def test_unknown_scope_is_refused(self):
        with pytest.raises(ServiceKeyError, match="неизвестные области"):
            normalize_scopes("read,delete-everything")

    def test_empty_scope_list_is_refused(self):
        with pytest.raises(ServiceKeyError):
            normalize_scopes("  ")

    def test_root_token_keeps_full_rights(self):
        principal = root_principal()
        assert principal.allows("admin") and principal.allows("read")


@pytest.mark.asyncio
class TestIssuing:
    async def test_issued_key_authenticates_with_its_scopes(self, session):
        service = ServiceKeyService(session, config())
        client, raw_key = await service.issue(name="price-backend", scopes="read")
        await session.commit()

        principal = await service.authenticate(raw_key)
        assert principal is not None
        assert principal.name == "price-backend"
        assert principal.allows("read")
        # Клиенту прайсов нужно только чтение: документы ему недоступны даже
        # при утечке ключа.
        assert not principal.allows("documents")
        assert principal.client_id == client.id

    async def test_key_is_not_stored_in_plain_text(self, session):
        service = ServiceKeyService(session, config())
        client, raw_key = await service.issue(name="ocr-bridge", scopes="documents")
        assert raw_key not in client.key_hash
        assert client.key_hash == key_digest(raw_key, config())

    async def test_wrong_key_with_a_valid_prefix_is_refused(self, session):
        service = ServiceKeyService(session, config())
        _, raw_key = await service.issue(name="price-backend", scopes="read")
        await session.commit()
        prefix = prefix_of(raw_key)
        assert await service.authenticate(f"fsa_{prefix}_wrong-secret") is None

    async def test_revoked_key_stops_working(self, session):
        service = ServiceKeyService(session, config())
        _, raw_key = await service.issue(name="price-backend", scopes="read")
        await session.commit()
        await service.revoke("price-backend")
        await session.commit()
        assert await service.authenticate(raw_key) is None

    async def test_duplicate_consumer_is_refused(self, session):
        service = ServiceKeyService(session, config())
        await service.issue(name="price-backend", scopes="read")
        await session.commit()
        with pytest.raises(ServiceKeyError, match="уже заведён"):
            await service.issue(name="price-backend", scopes="read")

    async def test_revoking_one_consumer_leaves_others_alive(self, session):
        """Ради этого ключи и разделены: отзыв одного не трогает остальных."""
        service = ServiceKeyService(session, config())
        _, price_key = await service.issue(name="price-backend", scopes="read")
        _, ocr_key = await service.issue(name="ocr-bridge", scopes="documents")
        await session.commit()

        await service.revoke("price-backend")
        await session.commit()

        assert await service.authenticate(price_key) is None
        assert await service.authenticate(ocr_key) is not None

    async def test_use_is_recorded(self, session):
        service = ServiceKeyService(session, config())
        client, raw_key = await service.issue(name="price-backend", scopes="read")
        await session.commit()
        assert client.last_used_at is None
        await service.authenticate(raw_key)
        assert client.last_used_at is not None
