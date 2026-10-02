"""Value normalization: names, articles, units, money."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

_WS_RE = re.compile(r"\s+")
_ARTICLE_LINE_RE = re.compile(r"^[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё._/-]*$")

# PostgreSQL btree entries have a hard byte-size limit (roughly 2.7 KiB on
# the deployed page size). ``normalized_name`` and ``normalized_article`` are
# indexed search keys, while the complete supplier wording remains in the
# unbounded ``source_*`` columns. A 512-codepoint key stays below that limit
# even for four-byte UTF-8 characters and prevents one verbose DOC/DOCX cell
# from aborting an entire transactional import.
MAX_INDEXED_NORMALIZED_TEXT_CHARS = 512


def _indexed_text(value: str) -> str:
    return value[:MAX_INDEXED_NORMALIZED_TEXT_CHARS]


def normalize_name(value: str) -> str:
    s = unicodedata.normalize("NFKC", str(value))
    s = s.lower().replace("ё", "е")
    s = _WS_RE.sub(" ", s).strip()
    return _indexed_text(s)


def normalize_article(value: str) -> str | None:
    s = unicodedata.normalize("NFKC", str(value)).strip()
    s = _WS_RE.sub(" ", s)
    if not s:
        return None
    return _indexed_text(s.upper())


def infer_article_from_name(value: str) -> str | None:
    """Return a standalone model code from the first line of a product name.

    Some price lists put an article on the first visual line of the name cell
    instead of providing a separate article column.  Requiring a second line,
    a digit and no spaces keeps ordinary product descriptions out of this
    fallback.
    """
    lines = [line.strip() for line in str(value).splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    candidate = lines[0]
    if (
        not _ARTICLE_LINE_RE.fullmatch(candidate)
        or not any(char.isalpha() for char in candidate)
        or not any(char.isdigit() for char in candidate)
    ):
        return None
    return candidate


_UNIT_ALIASES: dict[str, str] = {
    "шт": "шт", "шт.": "шт", "штук": "шт", "штука": "шт", "штуки": "шт",
    "м": "м", "м.": "м", "метр": "м", "метров": "м",
    "м.п.": "м.п.", "мп": "м.п.", "м п": "м.п.", "пог.м": "м.п.", "пог. м": "м.п.",
    "п.м": "м.п.", "п.м.": "м.п.", "п/м": "м.п.", "погонный метр": "м.п.",
    "м2": "м2", "м^2": "м2", "м²": "м2", "кв.м": "м2", "кв.м.": "м2", "кв. м": "м2",
    "кг": "кг", "кг.": "кг",
    "л": "л", "л.": "л", "литр": "л",
    "компл": "компл", "компл.": "компл", "комплект": "компл", "к-т": "компл", "кт": "компл",
    "уп": "уп", "уп.": "уп", "упак": "уп", "упак.": "уп", "упаковка": "уп",
    "пара": "пара", "пар": "пара",
}


def normalize_unit(value: object | None) -> str | None:
    if value is None:
        return None
    s = unicodedata.normalize("NFKC", str(value)).lower().replace("ё", "е")
    s = _WS_RE.sub(" ", s).strip().strip(",;")
    if not s:
        return None
    key = s.rstrip(".") if s.rstrip(".") in _UNIT_ALIASES else s
    return _UNIT_ALIASES.get(s, _UNIT_ALIASES.get(key, s))


_CANONICAL_UNITS = frozenset(_UNIT_ALIASES.values())


def is_known_unit(value: object | None) -> bool:
    """Whether a cell value is a recognized measurement unit such as «шт»."""
    if value is None or isinstance(value, bool) or isinstance(value, (int, float)):
        return False
    normalized = normalize_unit(value)
    return normalized in _CANONICAL_UNITS


_ON_REQUEST_MARKERS = (
    "по запросу", "договорная", "договорн", "уточняйте", "уточнять",
    "звоните", "по согласованию", "запрос", "по запр", "под заказ",
    "ожидается поставка", "по заявке", "смета",
)
_ZERO_PRICE_MARKERS = frozenset({"бесплатно", "без оплаты", "безвозмездно"})
# Currency words/symbols stripped before numeric parsing.
_CURRENCY_TOKEN = (
    r"(?<![A-Za-zА-Яа-яЁё])"
    r"(?:руб(?:\.|ля|лей)?|р\.?|₽|rub|eur|€|usd|\$|тенге|тг\.?|kzt)"
    r"(?![A-Za-zА-Яа-яЁё])"
)
_CURRENCY_RE = re.compile(_CURRENCY_TOKEN, re.IGNORECASE)
# Money columns are numeric(14,2): at most 12 digits of the integer part.
PRICE_MAX_INT_DIGITS = 12
# A bare number carries no currency word, no qualifier and no unit — just digits.
_BARE_NUMBER_RE = re.compile(r"^[+-]?[0-9]+(?:[.,][0-9]+)?$")
# Spaces incl. NBSP and narrow NBSP used as thousands separators.
_SPACE_RE = re.compile(r"[\s   ]+")
_NUMBER_TOKEN = r"[0-9]+(?:[\s   ][0-9]{3})*(?:[.,][0-9]+)?"
_RATE_PRICE_RE = re.compile(
    rf"^\s*({_NUMBER_TOKEN})\s*({_CURRENCY_TOKEN})\s+за\s+"
    rf"({_NUMBER_TOKEN})\s*([^\s,;]+).*$",
    re.IGNORECASE,
)
_RANGE_PRICE_RE = re.compile(
    rf"^\s*({_NUMBER_TOKEN})\s*[-–—]\s*({_NUMBER_TOKEN})"
    rf"(?:\s*({_CURRENCY_TOKEN}))?\s*$",
    re.IGNORECASE,
)
_LOWER_BOUND_PRICE_RE = re.compile(
    rf"^\s*от\s+({_NUMBER_TOKEN})"
    rf"(?:\s*({_CURRENCY_TOKEN}))?"
    r"(?:\s+за\b.*)?\s*$",
    re.IGNORECASE,
)
_ALTERNATIVE_PRICE_RE = re.compile(
    r"^\s*[0-9]+(?:[\s   ][0-9]{3})*(?:[.,][0-9]+)?"
    r"(?:\s*[/\\]+\s*[0-9]+(?:[\s   ][0-9]{3})*(?:[.,][0-9]+)?)+"
    r"(?:\s*(?:руб\.?|р\.?|₽|тенге|тг\.?|kzt))?\s*$",
    re.IGNORECASE,
)

# A spreadsheet often uses one of these labels in a numeric characteristic
# column to mean that the value is absent.  They are not malformed numbers.
NUMERIC_ABSENT_MARKERS = {
    "-",
    "—",
    "–",
    "нет",
    "нет данных",
    "н/д",
    "не указан",
    "не указано",
    "по запросу",
}


def is_absent_numeric_marker(raw_value: str) -> bool:
    normalized = " ".join(raw_value.casefold().replace("ё", "е").split())
    return normalized.rstrip(".") in NUMERIC_ABSENT_MARKERS


@dataclass
class ParsedPrice:
    status: str  # "ok" | "empty" | "on_request" | "error"
    amount: Decimal | None = None
    qualifier: str = "exact"
    amount_from: Decimal | None = None
    amount_to: Decimal | None = None
    currency: str | None = None
    currency_source: str = "unresolved"
    basis_quantity: Decimal | None = None
    basis_unit: str | None = None
    raw_value: str | None = None


def _decimal_token(value: str) -> Decimal:
    return Decimal(_SPACE_RE.sub("", value).replace(",", ".")).quantize(Decimal("0.01"))


def currency_code(value: object | None) -> str | None:
    """Return a currency code only when the source contains explicit evidence."""

    if value is None:
        return None
    match = _CURRENCY_RE.search(unicodedata.normalize("NFKC", str(value)).casefold())
    if match is None:
        return None
    token = match.group(0).casefold()
    if token in {"€", "eur"}:
        return "EUR"
    if token in {"$", "usd"}:
        return "USD"
    if token in {"тенге", "тг", "тг.", "kzt"}:
        return "KZT"
    return "RUB"


def currency_from_number_format(value: str | None) -> str | None:
    """Read currency evidence from an Excel number format.

    Excel uses ``$`` as syntax inside locale-qualified formats such as
    ``[$€-1]``. Checking unambiguous symbols first avoids treating that
    syntax marker as a US-dollar sign.
    """
    if not value:
        return None
    normalized = unicodedata.normalize("NFKC", value).casefold()
    if (
        "₽" in normalized
        or re.search(r'(?:руб|"р\.?")', normalized)
        or re.search(r"\[\$\s*р\.?(?:-\d+)?\]", normalized)
    ):
        return "RUB"
    if "€" in normalized or "eur" in normalized:
        return "EUR"
    if "₸" in normalized or "тенге" in normalized or "kzt" in normalized:
        return "KZT"
    if "$" in normalized or "usd" in normalized:
        return "USD"
    return None


@dataclass
class ParsedQuantityTier:
    """Inclusive quantity bounds for one price tier (price break).

    ``min_quantity`` and ``max_quantity`` are both inclusive; ``None`` means the
    tier is unbounded on that side.  A position's tiers partition the quantity
    axis, so a requested quantity matches exactly one tier.
    """

    min_quantity: Decimal | None = None
    max_quantity: Decimal | None = None
    unit: str | None = None
    raw_value: str | None = None


_TIER_UNIT_TOKEN = r"[а-яёa-z][а-яёa-z.]*"
_TIER_UP_TO_RE = re.compile(
    rf"^\s*(?:до|не\s+более)\s+({_NUMBER_TOKEN})\s*({_TIER_UNIT_TOKEN})?\s*$",
    re.IGNORECASE,
)
_TIER_OVER_RE = re.compile(
    rf"^\s*(?:свыше|более)\s+({_NUMBER_TOKEN})\s*({_TIER_UNIT_TOKEN})?\s*$",
    re.IGNORECASE,
)
_TIER_FROM_RE = re.compile(
    rf"^\s*от\s+({_NUMBER_TOKEN})\s*({_TIER_UNIT_TOKEN})?\s*$",
    re.IGNORECASE,
)
_TIER_RANGE_RE = re.compile(
    rf"^\s*({_NUMBER_TOKEN})\s*[-–—]\s*({_NUMBER_TOKEN})\s*({_TIER_UNIT_TOKEN})?\s*$",
    re.IGNORECASE,
)


def _tier_decimal(token: str) -> Decimal:
    return Decimal(_SPACE_RE.sub("", token).replace(",", ".")).quantize(Decimal("0.001"))


def parse_quantity_tier(value: object | None) -> ParsedQuantityTier | None:
    """Parse a quantity-tier label into inclusive bounds, or None.

    Understands the common Russian price-break wordings: ``до N`` (up to),
    ``свыше/более N`` (over), ``от N`` (from) and ``N-M`` ranges.  ``свыше N``
    is an exclusive lower bound; for integer pieces (``шт``) it is normalised to
    the inclusive bound ``N+1`` so that ``до 50 шт.`` / ``свыше 50 шт.`` become a
    gap-free, overlap-free partition ``[..50]`` / ``[51..]``.
    """
    if value is None:
        return None
    raw_value = str(value).strip()
    s = unicodedata.normalize("NFKC", raw_value).lower().replace("ё", "е").strip()
    if not s:
        return None

    match = _TIER_UP_TO_RE.match(s)
    if match is not None:
        return ParsedQuantityTier(
            None, _tier_decimal(match.group(1)),
            normalize_unit(match.group(2)), raw_value,
        )
    match = _TIER_OVER_RE.match(s)
    if match is not None:
        lower = _tier_decimal(match.group(1))
        unit = normalize_unit(match.group(2))
        if unit == "шт":
            lower += Decimal("1")
        return ParsedQuantityTier(lower, None, unit, raw_value)
    match = _TIER_FROM_RE.match(s)
    if match is not None:
        return ParsedQuantityTier(
            _tier_decimal(match.group(1)), None,
            normalize_unit(match.group(2)), raw_value,
        )
    match = _TIER_RANGE_RE.match(s)
    if match is not None:
        lower = _tier_decimal(match.group(1))
        upper = _tier_decimal(match.group(2))
        if upper < lower:
            return None
        return ParsedQuantityTier(lower, upper, normalize_unit(match.group(3)), raw_value)
    return None


def parse_price(value: object | None, *, allow_qualified: bool = False) -> ParsedPrice:
    if value is None:
        return ParsedPrice("empty")
    if isinstance(value, bool):
        return ParsedPrice("error")
    if isinstance(value, (int, float)):
        try:
            amount = Decimal(str(value)).quantize(Decimal("0.01"))
            return ParsedPrice("ok", amount, amount_from=amount, raw_value=str(value))
        except InvalidOperation:
            return ParsedPrice("error")
    if isinstance(value, Decimal):
        amount = value.quantize(Decimal("0.01"))
        return ParsedPrice("ok", amount, amount_from=amount, raw_value=str(value))

    raw_value = str(value).strip()
    s = unicodedata.normalize("NFKC", raw_value).strip().lower().replace("ё", "е")
    if (
        not s
        or s in {"-", "—", "–", "n/a", "нет", "итого", "итого:", "всего", "всего:"}
        or not s.strip("*").strip()
    ):
        return ParsedPrice("empty")
    if any(m in s for m in _ON_REQUEST_MARKERS):
        return ParsedPrice("on_request", qualifier="on_request", raw_value=raw_value)
    if s.rstrip(".") in _ZERO_PRICE_MARKERS:
        return ParsedPrice(
            "ok", Decimal("0.00"), amount_from=Decimal("0.00"), raw_value=raw_value
        )
    rate_match = _RATE_PRICE_RE.match(s) if allow_qualified else None
    if rate_match is not None:
        try:
            amount = _decimal_token(rate_match.group(1))
            basis_quantity = _decimal_token(rate_match.group(3))
            return ParsedPrice(
                "ok",
                amount,
                amount_from=amount,
                currency=currency_code(rate_match.group(2)),
                currency_source="cell",
                basis_quantity=basis_quantity,
                basis_unit=rate_match.group(4).strip(". "),
                raw_value=raw_value,
            )
        except InvalidOperation:
            return ParsedPrice("error", raw_value=raw_value)
    range_match = _RANGE_PRICE_RE.match(s) if allow_qualified else None
    if range_match is not None:
        try:
            lower = _decimal_token(range_match.group(1))
            upper = _decimal_token(range_match.group(2))
        except InvalidOperation:
            return ParsedPrice("error", raw_value=raw_value)
        if upper < lower:
            return ParsedPrice("error", raw_value=raw_value)
        marker = range_match.group(3)
        return ParsedPrice(
            "ok",
            lower,
            qualifier="range",
            amount_from=lower,
            amount_to=upper,
            currency=currency_code(marker),
            currency_source="cell" if marker else "unresolved",
            raw_value=raw_value,
        )
    lower_match = _LOWER_BOUND_PRICE_RE.match(s) if allow_qualified else None
    if lower_match is not None:
        try:
            lower = _decimal_token(lower_match.group(1))
        except InvalidOperation:
            return ParsedPrice("error", raw_value=raw_value)
        marker = lower_match.group(2)
        return ParsedPrice(
            "ok",
            lower,
            qualifier="from",
            amount_from=lower,
            currency=currency_code(marker),
            currency_source="cell" if marker else "unresolved",
            raw_value=raw_value,
        )
    if allow_qualified and _ALTERNATIVE_PRICE_RE.match(s):
        return ParsedPrice("on_request", qualifier="unparsed", raw_value=raw_value)

    # Asterisks after an amount are footnote markers in many supplier files,
    # not part of the monetary value.
    s = s.rstrip("*").rstrip()
    detected_currency = currency_code(s)
    s = _CURRENCY_RE.sub("", s)
    s = _SPACE_RE.sub("", s).strip()
    if not s:
        return ParsedPrice("empty")
    # "2340,36" -> "2340.36"; "1,234.50" -> keep dot decimal, drop comma thousands
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rindex(".") > s.rindex(",") else s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        amount = Decimal(s).quantize(Decimal("0.01"))
    except InvalidOperation:
        return ParsedPrice("error")
    if amount < 0:
        return ParsedPrice("error")
    return ParsedPrice(
        "ok",
        amount,
        amount_from=amount,
        currency=detected_currency,
        currency_source="cell" if detected_currency else "unresolved",
        raw_value=raw_value,
    )


def _bare_number_int_part(value: object) -> str | None:
    """Integer part when the value is a bare number, else ``None``.

    ``None`` means the value is *not* a bare number — it carries a currency word,
    a qualifier (``от 100``), a range, a unit or free text — so the plausibility
    bound does not apply to it.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return str(abs(int(value)))
    if not isinstance(value, str):
        return None
    cleaned = _SPACE_RE.sub("", unicodedata.normalize("NFKC", value).strip().rstrip("*"))
    if not _BARE_NUMBER_RE.match(cleaned):
        return None
    return cleaned.lstrip("+-").split(".", 1)[0].split(",", 1)[0]


def is_price_evidence(value: object | None, *, allow_qualified: bool = True) -> bool:
    """Whether a cell counts as *price evidence* for detection, gates and audits.

    Unlike :func:`parse_price` this is not purely lexical. A bare number is taken
    as a price only when it can plausibly be money: it must fit the money column
    (``numeric(14,2)``) and must not be padded with leading zeros. INN codes and
    bank accounts parse as ordinary decimals, which made them candidates of the
    section detector and — worse — blockers of the ``UNSAFE_NON_PRICE_DECISION``
    guard, so an operator could not dismiss a requisites block
    (см. ``docs/dev/order-page-price-bridge.md``).

    Qualified values (``от 100 руб``, ranges, currencies, ``по запросу``) skip
    the plausibility bound on purpose: their parse is unambiguous, and the guard
    must keep protecting them. Parsing of *declared* price columns keeps using
    :func:`parse_price` — there a number is a price by the operator's decision.
    """
    parsed = parse_price(value, allow_qualified=allow_qualified)
    if parsed.status not in {"ok", "on_request"}:
        return False
    int_part = _bare_number_int_part(value)
    if int_part is None:
        return True
    if len(int_part) > PRICE_MAX_INT_DIGITS:
        return False
    return not (len(int_part) > 1 and int_part.startswith("0"))
