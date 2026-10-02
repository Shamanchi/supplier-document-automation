"""Сквозной идентификатор запроса.

`correlationId` попадает в тело каждой ошибки и в заголовок ответа: по нему
сопоставляются запись в логе и то, что увидел пользователь. Клиент может задать
его сам — тогда цепочка прослеживается от фронтенда.

Перенесено из supplier-document-automation без изменений (docs/migration/01-backend-core.md).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.errors import CORRELATION_ID_HEADER


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        correlation = request.headers.get(CORRELATION_ID_HEADER) or str(uuid4())
        request.state.correlation_id = correlation
        response = await call_next(request)
        response.headers[CORRELATION_ID_HEADER] = correlation
        return response
