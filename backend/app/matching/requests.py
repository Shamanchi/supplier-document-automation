"""Разбор строки заявки: наименование, количество, единица.

Regex, а не модель: строки заявок однотипны («Огнетушитель ОП-4 — 25 шт»), и
на них правило дешевле, предсказуемее и не зависит от сети. Модель нужна там,
где правило не сработало, — и вызывается она отдельно, а не в этом слое.

Разобранная строка всегда остаётся редактируемой: ТЗ требует, чтобы менеджер
поправил количество и наименование до того, как система начнёт искать.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

# Единицы, встречающиеся в заявках заказчика. Ключ — то, что написано,
# значение — код, в котором считает система.
UNITS = {
    "ШТ": "шт", "ШТУК": "шт", "ШТУКИ": "шт", "ШТ.": "шт", "PCS": "шт",
    "КОМПЛ": "компл", "КОМПЛЕКТ": "компл", "К-Т": "компл", "КМП": "компл",
    "УП": "уп", "УПАК": "уп", "УПАКОВКА": "уп",
    "М": "м", "МЕТР": "м", "МЕТРОВ": "м", "ПМ": "м", "П.М": "м",
    "М2": "м2", "КВ.М": "м2", "М3": "м3", "КУБ.М": "м3",
    "КГ": "кг", "Л": "л", "Т": "т", "НАБОР": "набор", "РУЛОН": "рулон",
}

_UNIT_ALTERNATIVES = "|".join(
    sorted((re.escape(unit) for unit in UNITS), key=len, reverse=True)
)

# «— 25 шт», «25шт», «в количестве 25 шт», «x25»
_QUANTITY = re.compile(
    rf"(?:^|[\s\-—–,;:xх×])(?P<qty>\d+(?:[.,]\d+)?)\s*(?P<unit>{_UNIT_ALTERNATIVES})\b",
    re.IGNORECASE,
)
_TRAILING_QUANTITY = re.compile(r"(?:[\s\-—–,;:]\s*)(?P<qty>\d+(?:[.,]\d+)?)\s*$")
_NUMBERING = re.compile(r"^\s*\d{1,3}[.)]\s+")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ParsedLine:
    ordinal: int
    source_line: str
    query_name: str
    quantity: Decimal | None
    unit_code: str | None
    warnings: tuple[str, ...] = ()


def _to_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


def parse_line(line: str, ordinal: int = 0) -> ParsedLine:
    source = (line or "").strip()
    text = _NUMBERING.sub("", source)
    warnings: list[str] = []

    quantity: Decimal | None = None
    unit: str | None = None

    match = None
    for candidate in _QUANTITY.finditer(text):
        match = candidate  # берём последнее вхождение: количество обычно в конце
    if match is not None:
        quantity = _to_decimal(match["qty"])
        unit = UNITS.get(match["unit"].upper().rstrip("."))
        text = (text[: match.start()] + " " + text[match.end() :]).strip()
    else:
        tail = _TRAILING_QUANTITY.search(text)
        if tail is not None:
            quantity = _to_decimal(tail["qty"])
            text = text[: tail.start()].strip()
            warnings.append("единица не указана — принято «шт»")
            unit = "шт"

    name = _SPACE.sub(" ", text.strip(" -—–,;:")).strip()
    if not name:
        warnings.append("в строке нет наименования")
    if quantity is None:
        warnings.append("количество не распознано")

    return ParsedLine(
        ordinal=ordinal,
        source_line=source,
        query_name=name,
        quantity=quantity,
        unit_code=unit,
        warnings=tuple(warnings),
    )


def parse_text(text: str) -> list[ParsedLine]:
    """Заявка построчно; пустые строки и разделители пропускаются."""
    lines: list[ParsedLine] = []
    for raw in (text or "").splitlines():
        if not raw.strip() or set(raw.strip()) <= {"-", "—", "=", "*", "_"}:
            continue
        lines.append(parse_line(raw, ordinal=len(lines) + 1))
    return lines
