"""Resumable page planning for large model batches."""
from __future__ import annotations

from app.rule_proposal_batching import continuation_page, page_items, page_summary


def test_sixty_three_sections_are_processed_as_three_bounded_pages() -> None:
    items = list(range(63))

    pages = list(page_items(items, page_size=24))

    assert [len(page) for page in pages] == [24, 24, 15]
    assert [page[0] for page in pages] == [0, 24, 48]
    assert page_summary(total_sections=63, page_size=24) == {
        "total_sections": 63,
        "page_size": 24,
        "pages_count": 3,
    }


def test_page_planner_rejects_non_positive_page_size() -> None:
    try:
        list(page_items([1], page_size=0))
    except ValueError as error:
        assert "page_size" in str(error)
    else:  # pragma: no cover
        raise AssertionError("Нулевой page_size должен быть отклонён")


def test_continuation_page_returns_next_offset_until_last_page() -> None:
    items = list(range(63))

    first, next_offset = continuation_page(items, page_size=24, page_offset=0)
    second, last_offset = continuation_page(
        items, page_size=24, page_offset=next_offset or 0
    )
    last, done = continuation_page(
        items, page_size=24, page_offset=last_offset or 0
    )

    assert [len(first), len(second), len(last)] == [24, 24, 15]
    assert (next_offset, last_offset, done) == (24, 48, None)
