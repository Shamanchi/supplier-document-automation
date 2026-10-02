"""Row classification: item / category / empty.

A category row carries text but neither a parseable price nor a unit —
e.g. a section caption merged across the row, or a caption sitting in a
single cell of an otherwise empty row. Merged-fill copies are ignored so
a caption merged over the price column does not look like a price.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from app.parsing.grid import Cell
from app.parsing.mapping import TableMapping
from app.parsing.normalize import parse_price

ROW_EMPTY = "empty"
ROW_ITEM = "item"
ROW_CATEGORY = "category"
ROW_NOTE = "note"

_CONTACT_CAPTION_RE = re.compile(
    r"(?:@|https?://|www\.|\b(?:email|e-mail|website|site|тел|телефон|сайт|позвон\w*|напис\w*\s+нам)\b)",
    re.IGNORECASE,
)
_PRICE_CAPTION_RE = re.compile(r"^(?:цена|цены|стоимость)\b", re.IGNORECASE)
_OUTLINE_PREFIX_RE = re.compile(r"^\s*(\d+(?:\.\d+)*\.?)\s+")
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]+")
_SENTENCE_TRANSITION_RE = re.compile(
    r"(?P<previous>[A-Za-zА-Яа-яЁё0-9]+)"
    r"(?P<punctuation>[.!?]+)\s+(?=[A-ZА-ЯЁ])"
)
_TERMINAL_PUNCTUATION_RE = re.compile(r"[.!?]+\s*$")
_CLAUSE_TRANSITION_RE = re.compile(
    r",\s+(?:что|котор\w*|если|поскольку|так\s+как|при\s+этом)\b",
    re.IGNORECASE,
)
_LOWERCASE_FRAGMENT_RE = re.compile(r'^\s*["«„]*(?:\(\s*)?[а-яё]')
_PARENTHETICAL_FRAGMENT_RE = re.compile(r"^\s*[([]")
_LEADING_PREPOSITION_RE = re.compile(
    r"^\s*[\"«„]*(?:с|со|без|из|для|по|на|при|от|до|в|во)\s+",
    re.IGNORECASE,
)
_SHORT_PASSIVE_CLAUSE_RE = re.compile(
    r"^\s*[\"«„]*[A-ZА-ЯЁ][\w-]+\s+"
    r"[a-zа-яё][\w-]*(?:ованы|ёваны|ированы|ены|аны|яны)\b"
)
_DOCUMENT_REFERENCE_FRAGMENT_RE = re.compile(
    r"^\s*[A-ZА-ЯЁ]{2,6}\s+\d[\d.\-/]*.*(?:№\s*|[A-ZА-ЯЁ]{2,}\s+[A-ZА-ЯЁ]-)",
)


def _looks_like_technical_fragment(value: object) -> bool:
    text = " ".join(str(value).split())
    return bool(
        _LOWERCASE_FRAGMENT_RE.search(text)
        or _PARENTHETICAL_FRAGMENT_RE.search(text)
        or _LEADING_PREPOSITION_RE.search(text)
        or _SHORT_PASSIVE_CLAUSE_RE.search(text)
        or _DOCUMENT_REFERENCE_FRAGMENT_RE.search(text)
    )


def _looks_like_prominent_product_heading(value: object) -> bool:
    """Recognise a nominal heading before attached descriptive sentences.

    This is intentionally structural rather than a product-word dictionary.
    It admits a capitalised noun phrase such as a product class, while short
    passive clauses, prepositional fragments and all-caps prose titles remain
    notes.  The caller must already have strong visual heading evidence.
    """

    text = " ".join(str(value).split())
    if not text or _looks_like_technical_fragment(text):
        return False
    lead = re.split(r"[.!?]+\s+(?=[A-ZА-ЯЁ])", text, maxsplit=1)[0].strip()
    lead_words = _WORD_RE.findall(lead)
    if len(lead_words) < 2:
        return False
    first_letter = next((char for char in lead if char.isalpha()), "")
    if not first_letter or not first_letter.isupper():
        return False
    if lead.isupper() and lead != text:
        return False
    return _CLAUSE_TRANSITION_RE.search(lead) is None


def looks_like_explanatory_text(value: object) -> bool:
    """Return whether a standalone caption is clearly prose, not a heading.

    The check is deliberately conservative. A punctuation mark alone is not
    enough: short labels and uppercase abbreviations remain eligible category
    headings, while sentence transitions and longer sentence-like captions do
    not enter category context.
    """

    text = " ".join(str(value).split())
    if not text:
        return False
    candidate = _OUTLINE_PREFIX_RE.sub("", text, count=1)
    words = _WORD_RE.findall(candidate)
    # Supplier workbooks often format every line of a multi-line product card
    # exactly like a section caption.  Grammar is the stable distinction:
    # fragments that start mid-sentence, a parenthetical supply condition, a
    # prepositional characteristic, a short passive clause, or a bare numbered
    # document reference describe the preceding product class.  They are not
    # additional levels in the category hierarchy.
    if _looks_like_technical_fragment(candidate):
        return True
    for transition in _SENTENCE_TRANSITION_RE.finditer(candidate):
        previous = transition.group("previous")
        punctuation = transition.group("punctuation")
        if punctuation == "." and previous.isupper() and len(previous) <= 3:
            continue
        return True
    if len(words) >= 5 and _TERMINAL_PUNCTUATION_RE.search(candidate):
        return True
    if len(words) >= 7 and _CLAUSE_TRANSITION_RE.search(candidate):
        return True
    return len(words) >= 7 and ";" in candidate


def classify_caption_semantics(
    value: object, *, visually_prominent: bool = False
) -> str:
    """Classify one standalone caption through the shared P1 semantics."""

    if _looks_like_technical_fragment(value):
        return ROW_NOTE
    if looks_like_explanatory_text(value):
        if visually_prominent and _looks_like_prominent_product_heading(value):
            return ROW_CATEGORY
        return ROW_NOTE
    return ROW_CATEGORY


@dataclass
class RowClass:
    kind: str
    category_text: str | None = None
    category_indent: int | None = None
    category_outline_level: int | None = None


def category_outline_level(value: object) -> int | None:
    """Return a reliable numbered-heading depth without treating ``12 В`` as one."""

    match = _OUTLINE_PREFIX_RE.match(str(value))
    if match is None:
        return None
    marker = match.group(1)
    if "." not in marker:
        return None
    return marker.rstrip(".").count(".") + 1


def _cell(row: list[Cell], index: int) -> Cell | None:
    if 0 <= index < len(row):
        return row[index]
    return None


def classify_row(row: list[Cell], mapping: TableMapping) -> RowClass:
    def original_value(index: int) -> object | None:
        cell = _cell(row, index)
        if cell is None or cell.is_merged_fill:
            return None
        return cell.value

    name_val = None
    unit_val = None
    has_money = False
    has_price_value = False
    for col in mapping.columns:
        cell = _cell(row, col.index)
        v = original_value(col.index)
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        if col.role == "name" and name_val is None:
            name_val = v
        elif col.role == "unit" and unit_val is None:
            unit_val = v
        elif col.role == "price":
            has_price_value = True
            price_value = cell.display_value if cell and cell.display_value is not None else v
            if parse_price(
                price_value, allow_qualified=mapping.allow_qualified_prices
            ).status in ("ok", "on_request"):
                has_money = True

    # For a horizontally split catalogue, a row may contain an item only in
    # the neighbouring panel.  Looking through the *whole* source row would
    # make that neighbouring product a category caption in this panel, so
    # inspect only columns that belong to the current mapping first.
    any_text = None
    mapped_indices = {column.index for column in mapping.columns}
    mapped_real_values: list[str] = []
    for index in sorted(mapped_indices):
        cell = _cell(row, index)
        if cell is None or cell.is_merged_fill or cell.value is None:
            continue
        s = str(cell.value).strip()
        if s:
            mapped_real_values.append(s)
            if len(mapped_real_values) == 1:
                any_text = s

    if any_text is None:
        # A standalone caption merged across a source row is meaningful
        # context for every panel.  Preserve that case, but only when the
        # entire row contains exactly one real value; two cells are much more
        # likely to be an item (name + price) in another horizontal panel.
        whole_row_values = [
            str(cell.value).strip()
            for cell in row
            if not cell.is_merged_fill
            and cell.value is not None
            and str(cell.value).strip()
        ]
        if len(whole_row_values) == 1:
            any_text = whole_row_values[0]

    if any_text is None:
        return RowClass(ROW_EMPTY)
    # A non-empty price cell is item evidence even when its notation is not
    # currently parseable (for example ``15000/16000``).  Turning such a row
    # into category context contaminates every following item; keeping it as
    # an item lets the ordinary PRICE_PARSE_ERROR path report the real issue.
    if has_money or has_price_value or unit_val is not None:
        return RowClass(ROW_ITEM)
    if len(mapped_real_values) > 1:
        # A high-confidence category caption has one real anchor. Multiple
        # anchors inside the panel are item/attribute evidence, even when a
        # variable merged layout moved the price away from its header column.
        return RowClass(ROW_ITEM)
    if _CONTACT_CAPTION_RE.search(any_text) or _PRICE_CAPTION_RE.search(any_text):
        return RowClass(ROW_EMPTY)
    visually_prominent = any(
        cell is not None and cell.is_bold
        for index in mapped_indices
        if (cell := _cell(row, index)) is not None and not cell.is_merged_fill
    ) or any(
        (cell := _cell(row, index)) is not None and cell.is_merged_fill
        for index in mapped_indices
    )
    if (
        classify_caption_semantics(
            any_text,
            visually_prominent=visually_prominent,
        )
        == ROW_NOTE
    ):
        return RowClass(ROW_NOTE)

    def indentation(value: object) -> int:
        raw = str(value)
        prefix = raw[: len(raw) - len(raw.lstrip())]
        return len(prefix.expandtabs(4))

    if name_val is not None:
        # Name text but no price and no unit -> a category caption in the
        # name column.
        return RowClass(
            ROW_CATEGORY,
            category_text=str(name_val).strip(),
            category_indent=indentation(name_val),
            category_outline_level=category_outline_level(name_val),
        )
    return RowClass(
        ROW_CATEGORY,
        category_text=any_text,
        category_indent=indentation(any_text),
        category_outline_level=category_outline_level(any_text),
    )
