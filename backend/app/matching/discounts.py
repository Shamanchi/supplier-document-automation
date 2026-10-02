"""Скидки поставщиков и их применение к позициям расчёта.

Правило выбора одно и оно не арифметическое, а иерархическое: скидки не
складываются. Среди правил, подходящих к позиции, побеждает самое конкретное —
артикул важнее категории, категория важнее поставщика в целом. Внутри одного
уровня берётся максимальный процент: если у позиции два правила по категории,
менеджер вправе ожидать лучшее из них.

Объёмная скидка стоит особняком: она зависит не от позиции, а от суммы заказа у
поставщика, поэтому считается после того, как известны все строки, и
конкурирует с уровневой скидкой по максимуму — «Supplier B 4% от 300 тыс.» не
должен отменять артикульную скидку в 10%.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matching.text import article_key
from app.models import (
    DiscountRule,
    DiscountScope,
    SupplierCategoryMapping,
    SupplierPriceItem,
)

# Чем больше число, тем конкретнее правило. Объёмная скидка в шкале не
# участвует: она сравнивается по проценту, а не по конкретности.
SPECIFICITY: dict[str, int] = {
    DiscountScope.supplier.value: 1,
    DiscountScope.category.value: 2,
    DiscountScope.article.value: 3,
}

ZERO = Decimal("0")
HUNDRED = Decimal("100")


@dataclass(slots=True)
class DiscountMatch:
    """Найденная скидка: процент, источник и правило, по которому она дана."""

    percent: Decimal
    source: str
    rule_id: uuid.UUID | None

    @property
    def multiplier(self) -> Decimal:
        return (HUNDRED - self.percent) / HUNDRED


NO_DISCOUNT = DiscountMatch(percent=ZERO, source="none", rule_id=None)


@dataclass(slots=True)
class PositionKey:
    """Признаки позиции, по которым к ней подбирается скидка."""

    supplier_id: uuid.UUID | None
    category_id: uuid.UUID | None = None
    normalized_article: str | None = None


def apply_percent(price: Decimal | None, percent: Decimal) -> Decimal | None:
    """Цена после скидки, округлённая до копеек."""
    if price is None:
        return None
    value = Decimal(price) * (HUNDRED - Decimal(percent)) / HUNDRED
    return value.quantize(Decimal("0.01"))


class DiscountService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def rules_for(self, supplier_ids: list[uuid.UUID]) -> list[DiscountRule]:
        if not supplier_ids:
            return []
        query = select(DiscountRule).where(
            DiscountRule.supplier_id.in_(supplier_ids),
            DiscountRule.is_active.is_(True),
        )
        return list(await self.session.scalars(query))

    async def position_key(self, item: SupplierPriceItem, supplier_id: uuid.UUID) -> PositionKey:
        """Признаки позиции прайса для подбора скидки.

        Категория берётся ручная, если она проставлена: оператор правит её
        именно тогда, когда категория поставщика неверна, и скидка должна
        считаться по исправленной.
        """
        category_id = item.manual_category_id
        if category_id is None and item.supplier_category_id is not None:
            category_id = await self.session.scalar(
                select(SupplierCategoryMapping.global_category_id).where(
                    SupplierCategoryMapping.supplier_category_id == item.supplier_category_id
                )
            )
        return PositionKey(
            supplier_id=supplier_id,
            category_id=category_id,
            normalized_article=item.normalized_article,
        )

    def match(self, key: PositionKey, rules: list[DiscountRule]) -> DiscountMatch:
        """Уровневая скидка для позиции: самая конкретная, внутри уровня — максимум."""
        if key.supplier_id is None:
            return NO_DISCOUNT
        article = article_key(key.normalized_article or "")
        best: DiscountMatch | None = None
        best_rank = 0
        for rule in rules:
            # `is False`, а не `not`: у ещё не записанного правила флаг равен
            # None, и такой объект — активное правило, а не выключенное.
            if rule.supplier_id != key.supplier_id or rule.is_active is False:
                continue
            rank = SPECIFICITY.get(rule.scope, 0)
            if rank == 0:
                continue
            if rule.scope == DiscountScope.category.value:
                if key.category_id is None or rule.category_id != key.category_id:
                    continue
            if rule.scope == DiscountScope.article.value:
                if not article or article_key(rule.normalized_article or "") != article:
                    continue
            percent = Decimal(rule.percent)
            if rank < best_rank:
                continue
            if rank > best_rank or best is None or percent > best.percent:
                best = DiscountMatch(percent=percent, source=rule.scope, rule_id=rule.id)
                best_rank = rank
        return best or NO_DISCOUNT

    def volume(
        self, supplier_id: uuid.UUID | None, amount: Decimal, rules: list[DiscountRule]
    ) -> DiscountMatch:
        """Объёмная скидка по сумме позиций поставщика в расчёте."""
        if supplier_id is None:
            return NO_DISCOUNT
        best: DiscountMatch | None = None
        for rule in rules:
            if rule.supplier_id != supplier_id or rule.is_active is False:
                continue
            if rule.scope != DiscountScope.volume.value:
                continue
            threshold = Decimal(rule.min_amount) if rule.min_amount is not None else ZERO
            if amount < threshold:
                continue
            percent = Decimal(rule.percent)
            if best is None or percent > best.percent:
                best = DiscountMatch(
                    percent=percent, source=DiscountScope.volume.value, rule_id=rule.id
                )
        return best or NO_DISCOUNT

    @staticmethod
    def better(left: DiscountMatch, right: DiscountMatch) -> DiscountMatch:
        """Из двух скидок — выгоднее клиенту; скидки не складываются."""
        return right if right.percent > left.percent else left
