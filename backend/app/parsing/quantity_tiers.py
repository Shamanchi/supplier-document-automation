"""Ярусы цены по количеству: «1-30», «31-60», «>100».

Строка под объединённой ячейкой имени бывает двух разных вещей, и по замеру
корпуса это не спор о трактовке:

* **ярус по количеству** — тот же товар, цена зависит от размера партии
  (`Supplier E`: «1-30», «31-60», «>100» в колонке количества);
* **вариант товара** — другой товар под общим именем (`Supplier C`: «НЗК/Б» против
  «ВЗК/Б», `Supplier B`: «Закр.» против «Откр. Универс.»).

Различает их значение колонки количества: у яруса там диапазон числа, у
варианта — текстовый признак или обычная единица («шт.»). Ярус не создаёт
позицию каталога: иначе в каталоге появляются четыре неразличимых «Допинг
2.02т» с разной ценой, и сопоставление выбирает из них наугад.

Определение живёт здесь, а не в анализаторе, потому что им пользуются оба
контура — и разбор, и эталон. Разойдясь, они дали бы разное число позиций на
одном и том же прайсе.
"""
from __future__ import annotations

import re

_QUANTITY_TIER_RE = re.compile(r"^[<>≥≤]?\s*\d+\s*(?:[-–—+]\s*\d*)?\s*(?:шт|м|ед)?\.?$")


def is_quantity_tier(text: object) -> bool:
    """Значение колонки количества задаёт ярус цены, а не единицу измерения."""
    return bool(_QUANTITY_TIER_RE.match(str(text or "").strip()))


def quantity_bounds(text: object) -> tuple[int | None, int | None]:
    """Порог из подписи яруса: «31-60» → (31, 60), «>100» → (101, None).

    Строгое «>100» начинается со следующей штуки, нестрогое «≥100» и «100+» —
    с самой сотни. Разница в одну штуку, но именно она решает, какой ярус
    применится к заказу ровно на 100 единиц.
    """
    value = str(text or "").strip()
    if not is_quantity_tier(value):
        return None, None
    numbers = [int(n) for n in re.findall(r"\d+", value)]
    if not numbers:
        return None, None
    if value.startswith((">", "≥")) or value.endswith("+"):
        return (numbers[0] + 1 if value.startswith(">") else numbers[0]), None
    if value.startswith(("<", "≤")):
        return None, (numbers[0] - 1 if value.startswith("<") else numbers[0])
    if len(numbers) >= 2:
        return numbers[0], numbers[1]
    return numbers[0], numbers[0]
