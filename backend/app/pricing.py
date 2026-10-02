"""Quantity-based price selection over a position's tier ladder.

A position stores its prices as a list of quantity tiers (price breaks), each
with inclusive ``min_quantity``/``max_quantity`` bounds.  This module answers
"what does N pieces of this position cost?" by picking the tier that covers N.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable, TypeVar

T = TypeVar("T")


def _ladder_key(value: object) -> tuple[object, object]:
    """Лестница ступеней своя у каждого вида цены и режима НДС.

    Позиция может нести и дилерскую цену без ступеней, и розничную со
    ступенями. Сравнивать их между собой нельзя: у розничной ступени нижняя
    граница выше, поэтому она выигрывала бы всегда, и дилерскую цену запрос
    не вернул бы ни при каком количестве.
    """
    return getattr(value, "price_kind", None) or getattr(value, "kind", None), getattr(
        value, "vat_mode", None
    )


def _pick_tier(values: list[T], requested: Decimal) -> T | None:
    matches = [
        value
        for value in values
        if (value.min_quantity is None or requested >= value.min_quantity)
        and (value.max_quantity is None or requested <= value.max_quantity)
    ]
    if not matches:
        unbounded = [
            value
            for value in values
            if value.min_quantity is None and value.max_quantity is None
        ]
        return unbounded[0] if unbounded else None
    return max(
        matches,
        key=lambda value: (
            value.min_quantity is not None,
            value.min_quantity or Decimal("0"),
        ),
    )


def select_prices_for_quantity(
    values: Iterable[T], quantity: object
) -> dict[tuple[object, object], T]:
    """Применимая ступень для каждого вида цены: {(вид, НДС): цена}.

    Это то, что обычно нужно вызывающему: у позиции с дилерской и розничной
    ценой на запрос «200 шт.» есть два ответа, а не один.
    """
    grouped: dict[tuple[object, object], list[T]] = {}
    for value in values:
        grouped.setdefault(_ladder_key(value), []).append(value)
    requested = _as_quantity(quantity)
    out: dict[tuple[object, object], T] = {}
    for key, ladder in grouped.items():
        chosen = _pick_tier(ladder, requested)
        if chosen is not None:
            out[key] = chosen
    return out


def _as_quantity(quantity: object) -> Decimal:
    try:
        return Decimal(str(quantity))
    except (ArithmeticError, ValueError):
        return Decimal("1")


def select_price_for_quantity(
    values: Iterable[T],
    quantity: object,
    *,
    price_kind: str | None = None,
    vat_mode: str | None = None,
) -> T | None:
    """Return the price tier applicable for a requested quantity.

    A tier applies when the quantity falls within its inclusive
    ``[min_quantity, max_quantity]`` bounds (``None`` on a side means
    unbounded).  Among several matching tiers the most specific one — the
    highest lower bound — wins, so a ladder such as ``[..50]`` / ``[51..]``
    resolves unambiguously.  A quantity-unbounded value is the fallback base
    price when no tier covers the request.  Returns ``None`` when there are no
    values at all.

    ``price_kind`` / ``vat_mode`` выбирают лестницу. Без них берётся лестница
    первой цены позиции — ступени разных видов цены не смешиваются.
    """
    values = list(values)
    if price_kind is not None:
        values = [v for v in values if _ladder_key(v)[0] == price_kind]
    if vat_mode is not None:
        values = [v for v in values if _ladder_key(v)[1] == vat_mode]
    if not values:
        return None
    # Вид цены не задан — отвечаем по лестнице первой цены позиции, а не по
    # смеси всех. Кому нужны все виды сразу, есть `select_prices_for_quantity`.
    wanted = _ladder_key(values[0])
    ladder = [value for value in values if _ladder_key(value) == wanted]
    return _pick_tier(ladder, _as_quantity(quantity))
