"""Small, provider-neutral helpers for bounded resumable model batches."""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TypeVar


T = TypeVar("T")


def page_items(items: Sequence[T], *, page_size: int) -> Iterator[list[T]]:
    if page_size <= 0:
        raise ValueError("page_size должен быть положительным")
    for start in range(0, len(items), page_size):
        yield list(items[start : start + page_size])


def continuation_page(
    items: Sequence[T], *, page_size: int, page_offset: int
) -> tuple[list[T], int | None]:
    """Return one stable page and the offset for its continuation task."""

    if page_size <= 0:
        raise ValueError("page_size должен быть положительным")
    if page_offset < 0 or page_offset % page_size:
        raise ValueError("page_offset должен быть неотрицательным кратным page_size")
    page = list(items[page_offset : page_offset + page_size])
    next_offset = page_offset + page_size
    return page, next_offset if next_offset < len(items) else None


def page_summary(*, total_sections: int, page_size: int) -> dict[str, int]:
    if page_size <= 0:
        raise ValueError("page_size должен быть положительным")
    return {
        "total_sections": total_sections,
        "page_size": page_size,
        "pages_count": (total_sections + page_size - 1) // page_size,
    }
