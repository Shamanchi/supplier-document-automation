"""Ошибки публичного API: два формата по контурам.

Контур Фазы 3 отвечает envelope'ом (docs/api/API_OVERVIEW.md):

    {"error": {"code", "message", "details", "correlationId"}}

Контур витрины Фазы 2 — Problem Details (RFC 9457), как в контракте Фазы 2 §1.
Различие форматов зафиксировано в docs/architecture/CONTOUR_BOUNDARIES.md и
является осознанным: гармонизация отклонена вместе с INT-01.

Stack trace наружу не отдаётся ни в одном формате: `message` предназначено
пользователю, `code` — фронтенду и автоматическим клиентам.

Перенесено из supplier-document-automation без изменений (docs/migration/01-backend-core.md).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

CORRELATION_ID_HEADER = "X-Correlation-Id"
PROBLEM_CONTENT_TYPE = "application/problem+json"

#: Префиксы путей, отвечающих в формате Problem Details (контур Фазы 2).
PHASE2_PREFIXES: tuple[str, ...] = (
    "/api/v1/suppliers",
    "/api/v1/products",
    "/api/v1/calculations",
    "/api/v1/calculation-items",
    "/api/v1/exports",
    "/api/v1/price-files",
    "/api/v1/price-versions",
    "/api/v1/discount-schemes",
)


class ErrorCode(StrEnum):
    """Коды ошибок, общие для модулей. Доменные коды объявляются в модулях."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    FORBIDDEN = "FORBIDDEN"
    UNAUTHORIZED = "UNAUTHORIZED"
    REVISION_CONFLICT = "REVISION_CONFLICT"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    IDEMPOTENCY_KEY_CONFLICT = "IDEMPOTENCY_KEY_CONFLICT"
    PRECONDITION_REQUIRED = "PRECONDITION_REQUIRED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ApiError(Exception):
    """Ошибка бизнес-уровня, транслируемая в ответ нужного контура."""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = message
        self.status_code = status_code
        self.details = details


class NotFoundError(ApiError):
    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(ErrorCode.NOT_FOUND, message, status_code=404, **kw)


class ForbiddenError(ApiError):
    def __init__(self, message: str, **kw: Any) -> None:
        super().__init__(ErrorCode.FORBIDDEN, message, status_code=403, **kw)


class RevisionConflictError(ApiError):
    """Optimistic lock: клиент прислал устаревшую версию сущности."""

    def __init__(
        self, message: str = "Объект изменён другим пользователем", **kw: Any
    ) -> None:
        super().__init__(ErrorCode.REVISION_CONFLICT, message, status_code=409, **kw)


def is_phase2_path(path: str) -> bool:
    """Определяет контур по префиксу пути (см. CONTOUR_BOUNDARIES.md §1)."""
    return path.startswith(PHASE2_PREFIXES)


def correlation_id(request: Request) -> str:
    """Идентификатор запроса: из заголовка клиента либо выданный middleware."""
    return request.headers.get(CORRELATION_ID_HEADER) or getattr(
        request.state, "correlation_id", ""
    )


def envelope_response(
    status_code: int,
    code: str,
    message: str,
    *,
    correlation: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Формат контура Фазы 3."""
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    if correlation:
        error["correlationId"] = correlation
    return JSONResponse(status_code=status_code, content={"error": error})


def problem_response(
    status_code: int,
    code: str,
    message: str,
    *,
    correlation: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Формат контура витрины Фазы 2 (RFC 9457)."""
    problem: dict[str, Any] = {
        "type": f"urn:docprice:error:{code.lower()}",
        "title": code,
        "status": status_code,
        "detail": message,
    }
    if details:
        problem["errors"] = details
    if correlation:
        problem["correlationId"] = correlation
    return JSONResponse(
        status_code=status_code,
        content=problem,
        media_type=PROBLEM_CONTENT_TYPE,
    )


def error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """Выбирает формат по контуру запроса."""
    builder = (
        problem_response if is_phase2_path(request.url.path) else envelope_response
    )
    return builder(
        status_code,
        code,
        message,
        correlation=correlation_id(request),
        details=details,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Подключает обработчики так, чтобы наружу не попадал stack trace."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(
            request, exc.status_code, exc.code, exc.message, exc.details
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Тела ошибок Pydantic содержат исходные значения — наружу отдаём
        # только адрес поля и тип нарушения.
        fields = [
            {"field": ".".join(str(p) for p in err["loc"][1:]), "reason": err["type"]}
            for err in exc.errors()
        ]
        return error_response(
            request,
            422,
            ErrorCode.VALIDATION_ERROR,
            "Некорректные параметры запроса",
            {"fields": fields},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = {
            401: ErrorCode.UNAUTHORIZED,
            403: ErrorCode.FORBIDDEN,
            404: ErrorCode.NOT_FOUND,
            409: ErrorCode.REVISION_CONFLICT,
            422: ErrorCode.VALIDATION_ERROR,
        }.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        message = "Ошибка запроса"
        details: dict[str, Any] | None = None
        if isinstance(exc.detail, str):
            message = exc.detail
        elif isinstance(exc.detail, dict):
            # Доменная ошибка приходит словарём: машинный код плюс координаты
            # (например, UNSAFE_NON_PRICE_DECISION с заблокированными ячейками).
            # Терять их нельзя — иначе фронт показывает «Ошибка запроса» без
            # причины, а оператор не знает, что именно он скрывает.
            payload = dict(exc.detail)
            code = str(payload.pop("code", "") or code)
            message = str(payload.pop("message", "") or message)
            details = payload or None
        return error_response(request, exc.status_code, str(code), message, details)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Сообщение намеренно обезличено: детали остаются в логах по correlationId.
        return error_response(
            request,
            500,
            ErrorCode.INTERNAL_ERROR,
            "Внутренняя ошибка сервера",
        )
