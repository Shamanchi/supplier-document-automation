"""Нормализация номеров, дат и обозначений изделий.

Без приведения омоглифов join не сходится: в реестре и в документах один и тот
же номер встречается и с кириллической `С`, и с латинской `C`, а `НА67` и
`HA67` визуально неразличимы. OCR смешивает алфавиты ещё охотнее.
"""

from __future__ import annotations

import re
from datetime import date

# Кириллические буквы, неотличимые от латинских в номерах сертификатов.
HOMOGLYPHS = str.maketrans(
    {
        "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H",
        "О": "O", "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
    }
)

# Регистрационный номер: «ЕАЭС RU С-RU.ПБ97.В.00272/21», «ТС RU С-CN.АИ77.А.07017»,
# старые — без префикса формы.
NUMBER_PATTERN = re.compile(
    r"(?:(?:ЕАЭС|EAЭC|ЕЭАС|ТС|TC)\s+)?"
    r"RU\s*[СC]\s*-\s*[A-ZА-Я]{2}\s*\.\s*[A-ZА-Я0-9]{2,8}\s*\.\s*[A-ZА-Я]\s*\.\s*\d{4,6}"
    r"(?:\s*[/\\]\s*\d{2})?",
    re.IGNORECASE,
)

_DATE_PATTERNS = (
    re.compile(r"(?P<d>\d{2})\.(?P<m>\d{2})\.(?P<y>\d{4})"),
    re.compile(r"(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})"),
)

# Суффиксы исполнения: различают позиции прайса, но покрываются одним
# сертификатом, поэтому в ключ привязки не входят.
VARIANT_SUFFIXES = (
    "ПРЕМИУМ", "PREMIUM", "КРАСНЫЙ", "ХРОМИРОВАННЫЙ",
    "ЗПУ РОССИЯ", "ЗПУ РФ", "ЗПУ КИТАЙ",
)

_TOKEN_PATTERN = re.compile(r"[A-ZА-Я0-9]{2,}")

# «ОП-2, 3, 4, 5, 6, 8, 10» — один сертификат на десяток позиций прайса.
_RANGE_PATTERN = re.compile(
    r"\b(?P<prefix>[A-ZА-Я]{2,6})-(?P<numbers>\d+(?:\s*,\s*\d+){1,})"
)


def fold_homoglyphs(value: str) -> str:
    return value.translate(HOMOGLYPHS)


def normalize_number(value: str) -> str:
    """Ключ сравнения регистрационного номера.

    Пробелы схлопываются, регистр повышается, кириллические омоглифы уходят в
    латиницу. Разделители внутри номера убираются от пробелов, потому что OCR
    охотно вставляет их вокруг точек и дефисов.
    """
    cleaned = re.sub(r"\s+", " ", (value or "").strip().upper())
    cleaned = re.sub(r"\s*([./\\-])\s*", r"\1", cleaned)
    return fold_homoglyphs(cleaned)


def find_numbers(text: str) -> list[str]:
    """Вернуть нормализованные номера сертификатов, встреченные в тексте."""
    seen: dict[str, None] = {}
    for match in NUMBER_PATTERN.finditer(text or ""):
        seen.setdefault(normalize_number(match.group(0)), None)
    return list(seen)


def parse_date(value: str) -> date | None:
    """Разобрать дату в обоих форматах набора: `дд.мм.гггг` и ISO."""
    raw = (value or "").strip()
    if not raw:
        return None
    for pattern in _DATE_PATTERNS:
        match = pattern.match(raw)
        if not match:
            continue
        try:
            return date(int(match["y"]), int(match["m"]), int(match["d"]))
        except ValueError:
            return None
    return None


def normalize_designation(value: str) -> str:
    """Ключ сравнения обозначения изделия.

    `ОП-4 (з) АВСЕ` и `ОП-4(з)-АВСЕ-01` должны сойтись, поэтому пробелы вокруг
    скобок и дефисов убираются, а суффиксы исполнения отбрасываются.
    """
    text = fold_homoglyphs((value or "").upper().replace("Ё", "Е"))
    for suffix in VARIANT_SUFFIXES:
        text = text.replace(fold_homoglyphs(suffix), " ")
    text = re.sub(r"\s*([()\-.,/])\s*", r"\1", text)
    text = re.sub(r"[^A-ZА-Я0-9()\-./]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def expand_ranges(text: str) -> list[str]:
    """Развернуть перечисления вида «ОП-2, 3, 4» в отдельные обозначения."""
    result: list[str] = []
    for match in _RANGE_PATTERN.finditer(fold_homoglyphs((text or "").upper())):
        prefix = match["prefix"]
        for number in re.findall(r"\d+", match["numbers"]):
            result.append(f"{prefix}-{number}")
    return result


def tokenize(value: str) -> set[str]:
    """Токены для оценки схожести наименований продукции."""
    return set(_TOKEN_PATTERN.findall(fold_homoglyphs((value or "").upper())))


def similarity(left: str, right: str) -> float:
    """Коэффициент Жаккара по токенам; 0.0, если сравнивать нечего."""
    a, b = tokenize(left), tokenize(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
