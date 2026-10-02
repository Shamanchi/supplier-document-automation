"""Swagger UI и схема должны быть достижимы и пригодны для реальных вызовов."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.auth import ApiAuthMiddleware
from app.main import app


def _routes() -> set[str]:
    return {getattr(route, "path", None) for route in app.routes}


def test_docs_are_served_under_the_proxied_api_prefix() -> None:
    """nginx на стенде проксирует только `/api/`; всё остальное отдаёт SPA."""

    assert app.docs_url == "/api/docs"
    assert app.redoc_url == "/api/redoc"
    assert app.openapi_url == "/api/openapi.json"
    assert {"/api/docs", "/api/redoc", "/api/openapi.json"} <= _routes()


def test_docs_endpoints_do_not_require_authentication() -> None:
    # Без контекстного менеджера, чтобы не поднимать lifespan (MinIO/БД).
    client = TestClient(app)
    assert client.get("/api/openapi.json").status_code == 200
    assert client.get("/api/docs").status_code == 200


def test_swagger_keeps_the_entered_token_between_reloads() -> None:
    assert app.swagger_ui_parameters is not None
    assert app.swagger_ui_parameters.get("persistAuthorization") is True


def test_single_bearer_scheme_matches_how_credentials_are_actually_sent() -> None:
    """И JWT, и сервисный ключ приходят в `Authorization: Bearer`.

    Схема `apiKey`/`X-Api-Key` заставляла сгенерированный клиент слать заголовок,
    который сервер не читает, — любой вызов возвращал 401.
    """

    schemes = app.openapi()["components"]["securitySchemes"]
    assert set(schemes) == {"bearerAuth"}
    assert schemes["bearerAuth"]["type"] == "http"
    assert schemes["bearerAuth"]["scheme"] == "bearer"


def test_every_non_public_operation_declares_security() -> None:
    paths = app.openapi()["paths"]
    for path, operations in paths.items():
        for method, operation in operations.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            if path in ApiAuthMiddleware.public_paths:
                assert "security" not in operation, f"{method} {path}"
            else:
                assert operation.get("security") == [{"bearerAuth": []}], f"{method} {path}"


def test_service_prefixes_are_not_an_open_door() -> None:
    """Префиксы из `service_prefixes` проходят мимо middleware.

    Она пропускает их потому, что сервисный ключ и браузерный JWT неотличимы в
    одном заголовке, а разбирается с ними зависимость маршрута. Значит, каждая
    ручка такого префикса обязана требовать аутентификацию сама: иначе весь
    префикс оказывается открытым без токена.
    """

    client = TestClient(app)
    version_id = "00000000-0000-0000-0000-000000000001"
    unauthenticated = [
        ("get", f"/api/v1/price-versions/{version_id}"),
        ("get", f"/api/v1/price-versions/{version_id}/items"),
        ("get", f"/api/v1/price-versions/{version_id}/issues"),
        ("get", f"/api/v1/price-versions/{version_id}/export"),
        ("get", f"/api/v1/price-versions/{version_id}/mapping-preview"),
        ("get", "/api/v1/review/queue"),
        ("get", "/api/v1/catalog/categories"),
        ("get", "/api/v1/price-items"),
    ]
    for method, path in unauthenticated:
        response = getattr(client, method)(path)
        assert response.status_code == 401, f"{method.upper()} {path} -> {response.status_code}"


def test_description_tells_an_operator_how_to_authorise() -> None:
    description = app.openapi()["info"]["description"]
    assert "/api/v1/auth/login" in description
    assert "X-Project-ID" in description
