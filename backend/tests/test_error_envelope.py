"""Конверт ошибок: доменный `detail`-словарь доходит до клиента целиком.

Отказ гарда `UNSAFE_NON_PRICE_DECISION` несёт координаты заблокированных
ячеек. Пока хендлер отдавал «Ошибка запроса», оператор видел общий текст и не
понимал, что именно он скрывает (см. docs/dev/order-page-price-bridge.md).
"""

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.errors import register_exception_handlers

BLOCKED = {
    "code": "UNSAFE_NON_PRICE_DECISION",
    "message": "Решение скрывает строку с ценовым признаком",
    "sheet": "Счёт",
    "row": 1,
    "column": 3,
    "value": "7707083893",
    "evidence": [{"sheet": "Счёт", "row": 1, "column": 3, "value": "7707083893"}],
}


def _client() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/api/v1/review/blocked")
    async def blocked() -> None:
        raise HTTPException(status_code=422, detail=BLOCKED)

    @app.get("/api/v1/review/conflict")
    async def conflict() -> None:
        raise HTTPException(status_code=409, detail="Review уже изменён другим пользователем")

    @app.get("/api/v1/review/revision")
    async def revision() -> None:
        raise HTTPException(
            status_code=409,
            detail={"code": "REVIEW_REVISION_CONFLICT", "current_revision": 4},
        )

    @app.get("/api/v1/price-versions/blocked")
    async def phase2_blocked() -> None:
        raise HTTPException(status_code=422, detail=BLOCKED)

    return TestClient(app)


def test_domain_detail_reaches_the_envelope() -> None:
    response = _client().get("/api/v1/review/blocked")

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "UNSAFE_NON_PRICE_DECISION"
    assert body["message"] == "Решение скрывает строку с ценовым признаком"
    # Всё, кроме кода и сообщения, — машинные подробности для UI.
    assert body["details"] == {
        "sheet": "Счёт",
        "row": 1,
        "column": 3,
        "value": "7707083893",
        "evidence": [{"sheet": "Счёт", "row": 1, "column": 3, "value": "7707083893"}],
    }


def test_plain_http_errors_get_a_mapped_code() -> None:
    client = _client()

    conflict = client.get("/api/v1/review/conflict")
    assert conflict.status_code == 409
    assert conflict.json()["error"] == {
        "code": "REVISION_CONFLICT",
        "message": "Review уже изменён другим пользователем",
    }

    revision = client.get("/api/v1/review/revision")
    assert revision.json()["error"]["code"] == "REVIEW_REVISION_CONFLICT"
    assert revision.json()["error"]["message"] == "Ошибка запроса"
    assert revision.json()["error"]["details"] == {"current_revision": 4}


def test_phase2_contour_keeps_problem_details() -> None:
    response = _client().get("/api/v1/price-versions/blocked")

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["title"] == "UNSAFE_NON_PRICE_DECISION"
    assert body["detail"] == "Решение скрывает строку с ценовым признаком"
    assert body["errors"]["value"] == "7707083893"
