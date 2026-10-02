"""Quantity-based price selection over a position's tier ladder."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.pricing import select_price_for_quantity


@dataclass
class Tier:
    amount: Decimal
    min_quantity: Decimal | None = None
    max_quantity: Decimal | None = None


def _tungus_ladder() -> list[Tier]:
    # "до 50 шт." / "свыше 50 шт." as persisted after tier normalisation.
    return [
        Tier(Decimal("45070"), min_quantity=None, max_quantity=Decimal("50")),
        Tier(Decimal("41080"), min_quantity=Decimal("51"), max_quantity=None),
    ]


def test_quantity_selects_covering_tier():
    ladder = _tungus_ladder()
    assert select_price_for_quantity(ladder, 10).amount == Decimal("45070")
    assert select_price_for_quantity(ladder, 100).amount == Decimal("41080")


def test_tier_bounds_are_inclusive():
    ladder = _tungus_ladder()
    assert select_price_for_quantity(ladder, 50).amount == Decimal("45070")
    assert select_price_for_quantity(ladder, 51).amount == Decimal("41080")


def test_unbounded_value_is_base_price_fallback():
    base = Tier(Decimal("100"))
    bulk = Tier(Decimal("80"), min_quantity=Decimal("10"), max_quantity=None)
    values = [base, bulk]
    assert select_price_for_quantity(values, 1).amount == Decimal("100")
    assert select_price_for_quantity(values, 10).amount == Decimal("80")


def test_most_specific_tier_wins_on_overlap():
    values = [
        Tier(Decimal("100"), min_quantity=None, max_quantity=None),
        Tier(Decimal("90"), min_quantity=Decimal("10"), max_quantity=None),
        Tier(Decimal("80"), min_quantity=Decimal("100"), max_quantity=None),
    ]
    assert select_price_for_quantity(values, 150).amount == Decimal("80")
    assert select_price_for_quantity(values, 50).amount == Decimal("90")


def test_empty_values_return_none():
    assert select_price_for_quantity([], 10) is None
