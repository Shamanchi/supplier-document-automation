from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.auth as auth_module
from app.auth import (
    ApiAuthMiddleware,
    AuthUser,
    LoginRateLimiter,
    authenticate_credentials,
    create_token,
    decode_token,
)
from app.config import Settings
from app.config import settings as global_settings
from app.main import app as main_app


@pytest.fixture
def auth_settings() -> Settings:
    return Settings(
        app_admin_username="operator",
        app_admin_password="admin-password-that-is-long",
        app_user_username="viewer",
        app_user_password="viewer-password-that-is-long",
        app_auth_secret="auth-signing-secret-that-is-long-enough-123456",
    )


def test_credentials_map_to_expected_roles(auth_settings: Settings) -> None:
    assert authenticate_credentials("operator", "admin-password-that-is-long", auth_settings) == AuthUser(
        "operator", "admin"
    )
    assert authenticate_credentials("viewer", "viewer-password-that-is-long", auth_settings) == AuthUser(
        "viewer", "user"
    )
    assert authenticate_credentials("viewer", "wrong", auth_settings) is None


def test_access_and_refresh_tokens_are_not_interchangeable(auth_settings: Settings) -> None:
    user = AuthUser("viewer", "user")
    access = create_token(user, "access", auth_settings)
    refresh = create_token(user, "refresh", auth_settings)

    assert decode_token(access, "access", auth_settings) == user
    assert decode_token(refresh, "refresh", auth_settings) == user
    with pytest.raises(HTTPException) as error:
        decode_token(refresh, "access", auth_settings)
    assert error.value.status_code == 401


def test_login_rate_limiter_blocks_after_configured_failures() -> None:
    limiter = LoginRateLimiter(attempts=2, window_seconds=60)
    limiter.fail("client:user")
    limiter.fail("client:user")
    with pytest.raises(HTTPException) as error:
        limiter.check("client:user")
    assert error.value.status_code == 429
    limiter.reset("client:user")
    limiter.check("client:user")


def test_api_middleware_allows_user_reads_but_forbids_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.add_middleware(ApiAuthMiddleware)

    @app.get("/api/v1/items")
    async def read_items() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/api/v1/items")
    async def write_items() -> dict[str, bool]:
        return {"ok": True}

    def fake_auth(request):
        value = request.headers.get("Authorization")
        if value == "Bearer user":
            return AuthUser("viewer", "user")
        if value == "Bearer admin":
            return AuthUser("operator", "admin")
        raise HTTPException(status_code=401, detail="Требуется вход")

    monkeypatch.setattr(auth_module, "access_user_from_request", fake_auth)
    client = TestClient(app)

    assert client.get("/api/v1/items").status_code == 401
    assert client.get("/api/v1/items", headers={"Authorization": "Bearer user"}).status_code == 200
    denied = client.post("/api/v1/items", headers={"Authorization": "Bearer user"})
    assert denied.status_code == 403
    assert client.post("/api/v1/items", headers={"Authorization": "Bearer admin"}).status_code == 200


def test_login_contract_without_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Шаг миграции 02: login стал DB-backed (модуль identity фазы 3).

    Без БД проверяется только контракт до её касания: пустой identity
    отвергается как 422 в формате envelope фазы 3. Полный цикл
    login→refresh→me проверяется на живой БД в приёмке шага 02
    (docs/migration/02-backend-identity.md §Порядок выполнения).
    """
    client = TestClient(main_app)

    empty = client.post("/api/v1/auth/login", json={"password": "some-password"})
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "INVALID_CREDENTIALS"

    missing_password = client.post("/api/v1/auth/login", json={"email": "a@b.c"})
    assert missing_password.status_code == 422
    assert missing_password.json()["error"]["code"] == "VALIDATION_ERROR"


def test_refresh_without_cookie_is_envelope_401() -> None:
    """Refresh без cookie — 401 в формате envelope (контракт фазы 3)."""
    client = TestClient(main_app)
    response = client.post("/api/v1/auth/refresh")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"
