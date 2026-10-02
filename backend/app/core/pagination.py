"""Пагинация: cursor для контура Фазы 3, page/page_size для витрины Фазы 2.

Два стиля — следствие решения INT-01 (CONTOUR_BOUNDARIES.md §1). Смешивать их
внутри одного модуля запрещено: модуль выбирает стиль по своему контуру.

Курсор непрозрачен для клиента (base64 от значения сортировки), чтобы его нельзя
было конструировать вручную и завязываться на внутренний порядок.

Перенесено из supplier-document-automation без изменений (docs/migration/01-backend-core.md).
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Generic, TypeVar

from fastapi import Query
from pydantic import Field

from app.core.schemas import BaseSchema

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class CursorPage(BaseSchema, Generic[T]):
    """Ответ cursor-пагинации (контур Фазы 3)."""

    items: list[T]
    next_cursor: str | None = None


class Page(BaseSchema, Generic[T]):
    """Ответ offset-пагинации (контур витрины Фазы 2)."""

    items: list[T]
    page: int
    page_size: int
    total: int

    @property
    def pages_total(self) -> int:
        if self.page_size <= 0:
            return 0
        return -(-self.total // self.page_size)  # деление вверх


class CursorParams(BaseSchema):
    """Query-параметры cursor-пагинации."""

    cursor: str | None = None
    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)


class PageParams(BaseSchema):
    """Query-параметры offset-пагинации.

    Имена остаются snake_case: `page_size` зафиксирован контрактом Фазы 2 §1
    (см. API_CONVENTIONS.md §1 — camelCase не распространяется на query).
    """

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def cursor_params(
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> CursorParams:
    """Зависимость FastAPI для эндпоинтов контура Фазы 3."""
    return CursorParams(cursor=cursor, limit=limit)


def page_params(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> PageParams:
    """Зависимость FastAPI для эндпоинтов витрины Фазы 2."""
    return PageParams(page=page, page_size=page_size)


def encode_cursor(value: str) -> str:
    """Делает курсор непрозрачным для клиента."""
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> str:
    """Разбирает курсор; на некорректном значении поднимает ValueError."""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("некорректный курсор") from exc
