"""Сид скидок по договорённостям заказчика.

Цифры взяты из условий поставщиков: Supplier A даёт процент по категориям
(2–24%), Supplier B — объёмную скидку 4% от 300 тыс. ₽. Скрипт идемпотентен:
повторный запуск обновляет процент существующего правила, а не плодит дубли.

Запуск: `python -m scripts.seed_discounts [--dry-run]`
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal

from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    DiscountRule,
    DiscountScope,
    GlobalCategory,
    ORIGINAL_PROJECT_ID,
    Supplier,
)

# Категории названы так, как они лежат в общем классификаторе. Если категории
# в базе нет, правило пропускается с предупреждением — молча вешать скидку не
# на ту категорию хуже, чем не повесить вовсе.
YARPOZHINVEST_CATEGORIES: list[tuple[str, str]] = [
    ("Огнетушители", "24"),
    ("Рукава пожарные", "15"),
    ("Шкафы пожарные", "10"),
    ("Оповещатели", "5"),
]
YARPOZHINVEST_BASE = "2"
RUSARSENAL_VOLUME = ("4", "300000")


async def _supplier(session, needle: str) -> Supplier | None:
    return await session.scalar(select(Supplier).where(Supplier.name.ilike(f"%{needle}%")))


async def _upsert(session, *, dry_run: bool, **fields) -> str:
    query = select(DiscountRule).where(
        DiscountRule.supplier_id == fields["supplier_id"],
        DiscountRule.scope == fields["scope"],
        DiscountRule.category_id == fields.get("category_id"),
        DiscountRule.normalized_article == fields.get("normalized_article"),
    )
    existing = await session.scalar(query)
    if existing is not None:
        if existing.percent == fields["percent"]:
            return "без изменений"
        if not dry_run:
            existing.percent = fields["percent"]
            existing.min_amount = fields.get("min_amount")
        return "обновлено"
    if not dry_run:
        session.add(DiscountRule(project_id=ORIGINAL_PROJECT_ID, **fields))
    return "создано"


async def main(dry_run: bool) -> None:
    async with SessionLocal() as session:
        yar = await _supplier(session, "Supplier A")
        rus = await _supplier(session, "Supplier B")

        if yar is not None:
            status = await _upsert(
                session,
                dry_run=dry_run,
                supplier_id=yar.id,
                scope=DiscountScope.supplier.value,
                percent=Decimal(YARPOZHINVEST_BASE),
                note="базовая скидка поставщика",
            )
            print(f"Supplier A, базовая {YARPOZHINVEST_BASE}% — {status}")
            for name, percent in YARPOZHINVEST_CATEGORIES:
                category = await session.scalar(
                    select(GlobalCategory).where(GlobalCategory.name.ilike(name))
                )
                if category is None:
                    print(f"  категория «{name}» не найдена — правило пропущено")
                    continue
                status = await _upsert(
                    session,
                    dry_run=dry_run,
                    supplier_id=yar.id,
                    scope=DiscountScope.category.value,
                    category_id=category.id,
                    percent=Decimal(percent),
                    note=f"скидка по категории «{name}»",
                )
                print(f"  {name}: {percent}% — {status}")
        else:
            print("поставщик Supplier A не найден")

        if rus is not None:
            percent, threshold = RUSARSENAL_VOLUME
            status = await _upsert(
                session,
                dry_run=dry_run,
                supplier_id=rus.id,
                scope=DiscountScope.volume.value,
                percent=Decimal(percent),
                min_amount=Decimal(threshold),
                note=f"объёмная скидка от {threshold} ₽",
            )
            print(f"Supplier B, объёмная {percent}% от {threshold} ₽ — {status}")
        else:
            print("поставщик Supplier B не найден")

        if dry_run:
            print("— сухой прогон, изменения не сохранены")
        else:
            await session.commit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="показать, ничего не записывая")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
