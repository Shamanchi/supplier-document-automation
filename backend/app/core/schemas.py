"""Базовые схемы и типы публичного API.

Реализует сквозные соглашения из docs/api/API_CONVENTIONS.md:
  * §1 — наружу camelCase, внутри Python — snake_case;
  * §2 — деньги и ставки строкой (decimal-string), а не float;
  * §3 — уверенность хранится долей 0..1, отдаётся процентами 0..100.

Схемы модулей наследуются от `BaseSchema`; поля уверенности объявляются типом
`Confidence`, денежные — `Money`/`Rate`. Ручные преобразования шкалы (`* 100`)
в мапперах запрещены: иначе двойное представление даёт ошибки «умножили дважды».

Перенесено из supplier-document-automation без изменений (docs/migration/01-backend-core.md).
"""

from decimal import Decimal, InvalidOperation
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer
from pydantic.alias_generators import to_camel

MONEY_PLACES = Decimal("0.01")
CONFIDENCE_PLACES = Decimal("0.0001")


class BaseSchema(BaseModel):
    """Базовый класс всех схем публичного API.

    `populate_by_name` позволяет собирать модель и по snake_case-именам —
    это нужно для конструирования из ORM-объектов и в тестах.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


def _to_decimal(value: object) -> Decimal:
    """Принимает число или строку; float отвергается как источник потери точности."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise ValueError("используйте Decimal или строку: float теряет точность")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"не является числом: {value!r}") from exc


def _money_out(value: Decimal) -> str:
    return str(value.quantize(MONEY_PLACES))


def _percent_in(value: object) -> Decimal:
    """Вход API (0..100) → доменное значение (0..1)."""
    percent = _to_decimal(value)
    if not (Decimal(0) <= percent <= Decimal(100)):
        raise ValueError("уверенность вне диапазона 0..100")
    return (percent / 100).quantize(CONFIDENCE_PLACES)


def _percent_out(value: Decimal) -> str:
    """Доменное значение (0..1) → выход API (0..100)."""
    return str((value * 100).quantize(MONEY_PLACES))


#: Денежная величина: в JSON — строка с двумя знаками ("1200.00").
Money = Annotated[
    Decimal,
    BeforeValidator(_to_decimal),
    PlainSerializer(_money_out, return_type=str),
]

#: Ставка или процент (НДС, скидка): в JSON — строка с двумя знаками ("22.00").
Rate = Annotated[
    Decimal,
    BeforeValidator(_to_decimal),
    PlainSerializer(_money_out, return_type=str),
]

#: Уверенность: в домене и БД доля 0..1, в JSON — проценты строкой ("91.00").
Confidence = Annotated[
    Decimal,
    BeforeValidator(_percent_in),
    PlainSerializer(_percent_out, return_type=str),
]

#: Коэффициент без фиксированной точности: множитель конверсии, сдвиг, допуск.
#: В отличие от `Money`/`Rate` не квантуется до двух знаков — иначе scale вида
#: 0.001 превратился бы в "0.00" и конверсия молча стала бы нулевой.
Factor = Annotated[
    Decimal,
    BeforeValidator(_to_decimal),
    PlainSerializer(str, return_type=str),
]
