"""Header row detection and role suggestion from the header dictionary."""
from __future__ import annotations

import re
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator, Protocol

from app.parsing.grid import SheetGrid
from app.parsing.normalize import parse_price

HEADER_SCAN_ROWS = 30

EXACT_CONFIDENCE = 0.9
SUBSTRING_CONFIDENCE = 0.7
PROFILING_DOWNGRADE = 0.4
AUTO_CONFIDENCE_THRESHOLD = 0.7

_PUNCT_RE = re.compile(r"[.,;:()\[\]/\\\-–—_*'\"!?%]+")
_WS_RE = re.compile(r"\s+")


def normalize_header(value: object | None) -> str:
    """NFKC, lower, е->е, line breaks and punctuation collapsed to spaces."""
    if value is None:
        return ""
    s = unicodedata.normalize("NFKC", str(value))
    s = s.lower().replace("ё", "е")
    # A visual line wrap often inserts a discretionary hyphen inside one
    # header word (``Наименова-\nние``). Joining only letter-to-letter wraps
    # preserves meaningful separators in values such as ``цена-опт``.
    s = re.sub(r"(?<=[a-zа-я])-\s+(?=[a-zа-я])", "", s, flags=re.IGNORECASE)
    s = s.replace("\n", " ").replace("\r", " ")
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


# «Без НДС» пишут ещё и как «Без учета НДС», «С НДС» — как «С учетом НДС 22%»
# или «включая НДС». Без этих написаний цена с НДС и без НДС оказываются
# неразличимы: обе получают `unknown`, и в каталог уходят две цены, про которые
# нельзя сказать, какая из них с налогом.
_VAT_EXCLUDED = ("без ндс", "без учета ндс", "без учёта ндс", "ндс не", "кроме ндс")
_VAT_INCLUDED = (
    "с ндс",
    "с учетом ндс",
    "с учётом ндс",
    "включая ндс",
    "ндс вкл",
    "вкл ндс",     # «ЦЕНА, вкл. НДС (РУБ)» — точка съедается нормализацией
    "в т ч ндс",
    "в тч ндс",
)


def _vat_from_header(header_norm: str) -> str:
    # «Без» проверяется первым: «без учета НДС» не должно попасть под «с ндс».
    if any(marker in header_norm for marker in _VAT_EXCLUDED):
        return "excluded"
    if any(marker in header_norm for marker in _VAT_INCLUDED):
        return "included"
    return "unknown"


# (normalized alias, role, price kind or None)
_ALIASES: list[tuple[str, str, str | None]] = [
    # name
    ("наименование", "name", None),
    ("название", "name", None),
    ("продукция", "name", None),
    ("номенклатура", "name", None),
    ("товар", "name", None),
    # Some supplier sheets use a product-defining variant such as the number
    # of mailbox sections instead of a literal «Наименование».  It is a
    # valid name column when paired with price columns and surrounding sheet
    # context, not a quantity threshold for a price tier.
    ("кол во секций", "name", None),
    # article
    ("артикул", "article", None),
    ("номенклатура артикул", "article", None),
    ("номенклатура код", "article", None),
    ("код товара", "article", None),
    ("код", "article", None),
    # unit
    ("ед изм", "unit", None),
    ("единица измерения", "unit", None),
    ("единица", "unit", None),
    ("изм", "unit", None),
    ("ед", "unit", None),
    # prices
    ("розничная цена", "price", "retail"),
    ("розница с ндс", "price", "retail"),
    ("розница", "price", "retail"),
    ("ррц", "price", "retail"),
    # РРЦ пишут и словами. Это розничная цена, а не отдельный вид: поставщик
    # рекомендует её конечному покупателю.
    ("рекомендованная розничная цена", "price", "retail"),
    ("рекомендованная цена", "price", "retail"),
    ("рекомендуемая цена", "price", "retail"),
    ("оптовая цена с ндс", "price", "wholesale"),
    ("оптовая цена", "price", "wholesale"),
    ("цена опт", "price", "wholesale"),
    ("опт цена", "price", "wholesale"),
    ("мелкий опт", "price", "wholesale"),
    ("опт", "price", "wholesale"),
    ("дилерская цена", "price", "dealer"),
    ("цена для дилеров", "price", "dealer"),
    ("дилер", "price", "dealer"),
    ("закупочная цена", "price", "purchase"),
    ("цена со скидкой", "price", "other"),
    ("цена с ндс", "price", "other"),
    ("цена без ндс", "price", "other"),
    ("цена руб", "price", "other"),
    ("цена", "price", "other"),
    # other roles
    ("оптовое кол во", "quantity_threshold", None),
    ("кол во от", "quantity_threshold", None),
    ("оптовая скидка", "discount_percent", None),
    ("скидка", "discount_percent", None),
    ("вес кг", "weight", None),
    ("вес", "weight", None),
    ("масса", "weight", None),
    ("объем", "volume", None),
    ("шир выс гл", "dimensions", None),
    ("длина глубина высота мм", "dimensions", None),
    ("длина глубина высота", "dimensions", None),
    ("габаритные размеры", "dimensions", None),
    ("габариты", "dimensions", None),
    ("габарит", "dimensions", None),
    ("наличие", "stock", None),
    ("остаток", "stock", None),
    ("склад", "stock", None),
    ("срок производства", "lead_time", None),
    ("срок поставки", "lead_time", None),
    ("срок изготовления", "lead_time", None),
    ("производитель", "manufacturer", None),
    ("бренд", "manufacturer", None),
    ("завод", "manufacturer", None),
    ("технические характеристики", "description", None),
    ("техпараметры", "description", None),
    ("краткая характеристика", "description", None),
    ("дополнительная информация", "description", None),
    ("характеристики", "description", None),
    ("описание", "description", None),
    ("примечание", "description", None),
    ("категория", "category", None),
    ("толщина металла", "attribute", None),
    ("изображение", "photo", None),
    ("фото", "photo", None),
    ("условный размер", "dimensions", None),
    ("усл размер", "dimensions", None),
    ("размер", "dimensions", None),
    # NFKC decomposes "№" into "no", so both spellings are listed.
    ("№ п п", "row_number", None),
    ("no п п", "row_number", None),
    ("п п", "row_number", None),
    ("номер", "row_number", None),
    ("№", "row_number", None),
    ("no", "row_number", None),
    ("n", "row_number", None),
]

# For substring matching, longer (more specific) aliases win first.
_ALIASES_BY_LEN = sorted(_ALIASES, key=lambda a: -len(a[0]))
# Short aliases are exact-match only, otherwise "ед" would hit inside "цена в еду" etc.
_MIN_SUBSTRING_ALIAS_LEN = 3


@dataclass
class ColumnSuggestion:
    index: int
    header: str
    role: str = "ignore"
    confidence: float = 0.0
    price_descriptor: dict | None = None  # {"kind": ..., "vat_mode": ...}
    # True when the visible header text is a copied value of a merged anchor.
    # Such a price can still govern a real child column in a two-level header;
    # ``profile_price_columns`` decides that from independent body values.
    is_merged_fill: bool = False


@dataclass
class HeaderDetection:
    header_row_index: int | None
    columns: list[ColumnSuggestion] = field(default_factory=list)


class AliasOverlay(Protocol):
    """Внешний словарь подписей поверх встроенного.

    Держится контекстной переменной, а не аргументом: `suggest_role` вызывается
    из двух десятков мест разбора, и протаскивание сессии БД через каждое из них
    стоило бы дороже, чем даёт словарь. Реализация живёт в `app.column_aliases`.
    """

    def resolve(self, caption: object) -> "ColumnSuggestion | None": ...


_alias_overlay: ContextVar[AliasOverlay | None] = ContextVar("alias_overlay", default=None)


@contextmanager
def use_alias_overlay(overlay: AliasOverlay | None) -> Iterator[None]:
    """Словарь действует только внутри одного разбора."""
    token = _alias_overlay.set(overlay)
    try:
        yield
    finally:
        _alias_overlay.reset(token)


def suggest_role(raw_header: object | None) -> ColumnSuggestion:
    header_norm = normalize_header(raw_header)
    sug = ColumnSuggestion(index=-1, header="" if raw_header is None else str(raw_header))
    if not header_norm:
        return sug

    # Разобранная однажды подпись имеет приоритет над догадкой по подстроке:
    # это подтверждённое решение человека, а не эвристика.
    overlay = _alias_overlay.get()
    if overlay is not None:
        resolved = overlay.resolve(raw_header)
        if resolved is not None:
            return resolved

    # A malformed *cell value* such as «не цена» must not become a second
    # table header during the full-sheet scan. It is not a meaningful price
    # heading, and treating it as one can cut a valid table before the row is
    # reported as a price parsing error.
    if header_norm.startswith(("не цена", "не стоимость", "нет цены")):
        return sug

    # Exact semantic headers take precedence over broad price cues below:
    # «Оптовое кол-во» is a quantity threshold, not a wholesale price.
    for alias, role, kind in _ALIASES:
        if header_norm == alias:
            sug.role, sug.confidence = role, EXACT_CONFIDENCE
            if role == "price":
                sug.price_descriptor = {"kind": kind, "vat_mode": _vat_from_header(header_norm)}
            return sug

    # Some spreadsheet producers insert a discretionary hyphen into a word
    # without a line break (``Наименова-ние``). For long exact aliases only,
    # compare the compact spelling so this formatting artifact cannot hide a
    # semantic header. Short aliases remain exact-only.
    compact = header_norm.replace(" ", "")
    for alias, role, kind in _ALIASES:
        if len(alias.replace(" ", "")) >= 6 and compact == alias.replace(" ", ""):
            sug.role, sug.confidence = role, EXACT_CONFIDENCE
            if role == "price":
                sug.price_descriptor = {
                    "kind": kind,
                    "vat_mode": _vat_from_header(header_norm),
                }
            return sug

    # «Цена завода», «Стоимость дилера», «Тариф» and similar headers are
    # price columns.  The generic ``завод`` alias below means manufacturer
    # only when no price cue is present; otherwise its longer spelling used to
    # win over «опт».  Recognising these cues before the catch-all attribute
    # path also prevents a second price from becoming a product property.
    price_cues = (
        "цена",
        "стоим",
        "тариф",
        "прайс",
        "руб",
        "опт",
        "розн",
        "дилер",
        "закуп",
    )
    if any(cue in header_norm for cue in price_cues):
        price_matches = [
            (len(alias), kind)
            for alias, role, kind in _ALIASES
            if role == "price" and alias in header_norm
        ]
        typed_matches = [match for match in price_matches if match[1] not in {None, "other"}]
        if "дилер" in header_norm:
            kind = "dealer"
        elif "закуп" in header_norm:
            kind = "purchase"
        elif "розн" in header_norm:
            kind = "retail"
        elif "опт" in header_norm:
            kind = "wholesale"
        else:
            kind = max(typed_matches or price_matches, default=(0, "other"))[1] or "other"
        sug.role = "price"
        sug.confidence = SUBSTRING_CONFIDENCE
        sug.price_descriptor = {"kind": kind, "vat_mode": _vat_from_header(header_norm)}
        return sug

    # A compound header such as «Габариты, мм / характеристики ДхШхВ» names a
    # physical size; the generic «характеристики» alias is longer and would
    # otherwise reclassify the column as a free-text description.
    if any(cue in header_norm for cue in ("габарит", "дхшхв", "дхш", "шхг")):
        sug.role = "dimensions"
        sug.confidence = SUBSTRING_CONFIDENCE
        return sug

    for alias, role, kind in _ALIASES_BY_LEN:
        if len(alias) < _MIN_SUBSTRING_ALIAS_LEN:
            continue
        if alias in header_norm:
            sug.role, sug.confidence = role, SUBSTRING_CONFIDENCE
            if role == "price":
                sug.price_descriptor = {"kind": kind, "vat_mode": _vat_from_header(header_norm)}
            return sug

    return sug


def _header_candidate(row, row_index: int) -> tuple[int, HeaderDetection] | None:
    suggestions: list[ColumnSuggestion] = []
    hits = 0
    has_name = False
    has_price = False
    for column_index, cell in enumerate(row):
        suggestion = suggest_role(cell.value)
        # A merged header range repeats the anchor text in every covered cell.
        # The filler cells are visual copies, not independent fields.  Keeping
        # them as names/prices manufactures fake horizontal panels and can
        # attach the right catalogue's price to the left product.
        suggestion.is_merged_fill = cell.is_merged_fill
        if cell.is_merged_fill and suggestion.role != "price":
            suggestion.role = "ignore"
            suggestion.confidence = 0.0
            suggestion.price_descriptor = None
        suggestion.index = column_index
        # An ignored merged-fill must not be revived as a generic draft
        # attribute by ``detection_to_mapping(..., retain_unknown_attributes)``.
        suggestion.header = (
            ""
            if cell.value is None or cell.is_merged_fill
            else str(cell.value)
        )
        suggestions.append(suggestion)
        # Merged-fill copies of one header cell must not inflate the hit
        # count (a caption merged across the row is not 5 dictionary hits).
        if suggestion.confidence >= AUTO_CONFIDENCE_THRESHOLD and not cell.is_merged_fill:
            hits += 1
            # Product values often contain a word like «товар». A heading may
            # be compound («Наименование товара»), but should not look like a
            # numbered data value. This small guard keeps a malformed price
            # cell in a body row from manufacturing a false later table.
            if suggestion.role == "name" and not any(
                char.isdigit() for char in normalize_header(cell.value)
            ):
                has_name = True
            if suggestion.role == "price":
                has_price = True
    # A row of data such as «Товар A | 100 | Товар B | 200» can contain two
    # substring name hits.  It must not become a later table header and cut a
    # valid table at its first item.  Header candidates are specifically price
    # table headers, so require their own recognised price label.
    if not has_name or not has_price or hits < 2:
        return None
    return hits, HeaderDetection(header_row_index=row_index, columns=suggestions)


def detect_multiline_header_candidates(grid: SheetGrid) -> list[HeaderDetection]:
    """Detect a compact two-row header band using header and body evidence.

    A supplier may put the commercial price caption on the first header row
    and its currency/VAT qualifier directly below it.  Neither row then has
    both mandatory labels on its own.  The band is accepted only when the
    candidate column also contains at least two positive prices paired with a
    textual product name, so currency words in an ordinary table are not
    enough to manufacture a price sheet.
    """

    def money_band(text: str) -> bool:
        normalized = normalize_header(text)
        tokens = set(normalized.split())
        return bool(tokens & {"руб", "рубль", "рублей", "rub", "rur"}) or "ндс" in tokens

    def positive_price(value: object | None) -> bool:
        parsed = parse_price(value)
        return parsed.status == "ok" and parsed.amount is not None and parsed.amount > 0

    detections: list[HeaderDetection] = []
    scan_end = min(len(grid.rows) - 1, HEADER_SCAN_ROWS - 1)
    for top_index in range(max(0, scan_end)):
        top = grid.rows[top_index]
        bottom = grid.rows[top_index + 1]
        width = max(len(top), len(bottom))
        if width < 2:
            continue

        combined: list[ColumnSuggestion] = []
        name_indexes: list[int] = []
        money_indexes: list[int] = []
        for column_index in range(width):
            parts = []
            for row in (top, bottom):
                if column_index < len(row):
                    value = row[column_index].value
                    if value is not None and str(value).strip():
                        parts.append(str(value).strip())
            header = " ".join(dict.fromkeys(parts))
            suggestion = suggest_role(header)
            suggestion.index = column_index
            suggestion.header = header
            combined.append(suggestion)
            if suggestion.role == "name" and suggestion.confidence >= AUTO_CONFIDENCE_THRESHOLD:
                name_indexes.append(column_index)
            if money_band(header):
                money_indexes.append(column_index)
        if not name_indexes or not money_indexes:
            continue

        confirmed_prices: list[int] = []
        for price_index in money_indexes:
            hits = 0
            for row in grid.rows[top_index + 2 :]:
                if price_index >= len(row) or not positive_price(row[price_index].value):
                    continue
                if any(
                    name_index < len(row)
                    and isinstance(row[name_index].value, str)
                    and any(char.isalpha() for char in row[name_index].value)
                    for name_index in name_indexes
                ):
                    hits += 1
            if hits >= 2:
                confirmed_prices.append(price_index)
        if not confirmed_prices:
            continue

        for price_index in confirmed_prices:
            header = combined[price_index].header
            combined[price_index].role = "price"
            combined[price_index].confidence = EXACT_CONFIDENCE
            combined[price_index].price_descriptor = {
                "kind": "other",
                "vat_mode": _vat_from_header(normalize_header(header)),
            }
        detections.append(
            HeaderDetection(header_row_index=top_index + 1, columns=combined)
        )
    return detections


def _header_signature(row) -> tuple[str, ...]:
    signature = tuple(normalize_header(cell.value) for cell in row)
    while signature and not signature[-1]:
        signature = signature[:-1]
    return signature


def iter_header_candidates(grid: SheetGrid):
    """Yield ``(row_index, signature, detection)`` for every price-table header
    row in source order, without any deduplication.

    ``detect_header_candidates`` collapses consecutive identical signatures on
    top of this; consumers that need to know how many times a header repeats
    (a vertically stacked category -> header -> positions pattern) use the raw
    stream directly.
    """
    for row_index, row in enumerate(grid.rows):
        candidate = _header_candidate(row, row_index)
        if candidate is None:
            continue
        yield row_index, _header_signature(row), candidate[1]


def count_header_sections(grid: SheetGrid, header_row_index: int | None) -> int:
    """Number of price-table header rows sharing the signature of the row at
    ``header_row_index``.  A value >= 2 means the sheet repeats the same
    section layout (category caption -> header -> positions) several times."""
    if header_row_index is None or not (0 <= header_row_index < len(grid.rows)):
        return 0
    wanted = _header_signature(grid.rows[header_row_index])
    if not wanted:
        return 0
    return sum(
        1
        for _row_index, signature, _detection in iter_header_candidates(grid)
        if signature == wanted
    )


def repeating_section_count(grid: SheetGrid, header_row_index: int | None) -> int:
    """Number of identical stacked sections, or 0 when not a clean repeat.

    A clean repeat is one contiguous run of the same header (the category ->
    header -> positions pattern repeated N times): the signature appears exactly
    once among the deduped header candidates.  An interleaved layout (A -> B ->
    A) returns 0 — its two A headers cannot be told apart by structure alone, so
    those sections must stay individually addressable by a fixed row number.
    """
    raw_count = count_header_sections(grid, header_row_index)
    if raw_count < 2 or header_row_index is None:
        return 0
    wanted = _header_signature(grid.rows[header_row_index])
    deduped_occurrences = sum(
        1
        for detection in detect_header_candidates(grid)
        if detection.header_row_index is not None
        and 0 <= detection.header_row_index < len(grid.rows)
        and _header_signature(grid.rows[detection.header_row_index]) == wanted
    )
    return raw_count if deduped_occurrences == 1 else 0


def detect_header_candidates(grid: SheetGrid) -> list[HeaderDetection]:
    """Return every price-table header in a sheet, in source-row order.

    A single worksheet can contain several vertically stacked price tables
    with different columns.  The former one-header-only approach kept using
    the first table's column positions for later tables and could turn a
    price into an unrelated attribute.  This helper intentionally scans the
    full sheet; only rows with both a recognised name and a recognised price
    label are candidates.
    """

    result: list[HeaderDetection] = []
    previous_signature: tuple[str, ...] | None = None
    for _row_index, signature, detection in iter_header_candidates(grid):
        # A repeated visual header within one long table is not a new table:
        # the regular row parser already skips it and must keep the category
        # context accumulated above it.  Only a *consecutive candidate* with
        # the same normalized layout is redundant; A → B → A is retained
        # because the layout really changes back after section B.
        if signature == previous_signature:
            continue
        result.append(detection)
        previous_signature = signature
    return result


def detect_header(grid: SheetGrid) -> HeaderDetection:
    """Pick the strongest early header for legacy single-table consumers."""

    best: tuple[int, HeaderDetection] | None = None
    for row_index, row in enumerate(grid.rows[:HEADER_SCAN_ROWS]):
        candidate = _header_candidate(row, row_index)
        if candidate is not None and (best is None or candidate[0] > best[0]):
            best = candidate
    if best is None:
        return HeaderDetection(header_row_index=None, columns=[])
    return best[1]


def profile_name_column(grid: SheetGrid, detection: HeaderDetection) -> None:
    """Перенести роль имени на колонку с текстом, если в текущей — номера строк.

    Объединённая шапка «Продукция» накрывает и колонку с порядковым номером, и
    колонку с названием. Роль достаётся мастеру объединения, то есть номеру, и
    в каталог уходят позиции с именами «1», «2», «3». Решается по данным:
    если в колонке-кандидате почти сплошь целые числа, а под той же шапкой
    правее стоит текст — имя там.
    """
    if detection.header_row_index is None:
        return
    body = grid.rows[detection.header_row_index + 1 :]

    def text_share(index: int) -> tuple[float, int]:
        text = filled = 0
        for row in body:
            if index >= len(row):
                continue
            cell = row[index]
            if cell.value is None or cell.is_merged_fill:
                continue
            value = str(cell.value).strip()
            if not value:
                continue
            filled += 1
            if any(char.isalpha() for char in value):
                text += 1
        return (text / filled if filled else 0.0), filled

    by_index = {sug.index: sug for sug in detection.columns}
    for sug in detection.columns:
        if sug.role != "name":
            continue
        share, filled = text_share(sug.index)
        # Колонка имени, где меньше четверти значений содержат буквы, именем не
        # является — это нумерация. Порог низкий намеренно: артикулы вроде
        # «ОП-5» буквы содержат, а номера строк — нет.
        if filled < 3 or share >= 0.25:
            continue
        # Кандидат ищется только среди колонок той же объединённой шапки:
        # соседняя колонка под своей подписью — это другая роль, а не имя.
        for offset in (1, 2):
            neighbour = by_index.get(sug.index + offset)
            if neighbour is None or not neighbour.is_merged_fill:
                break
            if neighbour.role not in {"ignore", "name"}:
                continue
            neighbour_share, neighbour_filled = text_share(neighbour.index)
            if neighbour_filled >= 3 and neighbour_share >= 0.8:
                neighbour.role = "name"
                neighbour.confidence = sug.confidence
                neighbour.header = sug.header
                sug.role = "ignore"
                sug.confidence = 0.0
                break


def merge_price_subheader(grid: SheetGrid, detection: HeaderDetection) -> None:
    """Уточнить режим НДС ценовой колонки по второму ярусу шапки.

    Верхний ярус («Цена для дилеров, руб.») даёт вид цены, нижний («Без учета
    НДС» / «С учетом НДС 22%») — режим НДС. Ни один по отдельности полного
    ответа не даёт, а разбор читает шапку одной строкой, поэтому цена с НДС и
    без НДС оказывались неразличимы: обе с `vat_mode = unknown`.

    Роли не меняются — только режим НДС и подпись. Второй ярус признаётся
    таковым, если в нём нет денег: строка с ценами это данные, а не заголовок.
    """
    if detection.header_row_index is None:
        return
    price_columns = [sug for sug in detection.columns if sug.role == "price"]
    if not price_columns:
        return
    for offset in (1, 2):
        index = detection.header_row_index + offset
        if index >= len(grid.rows):
            return
        row = grid.rows[index]
        if any(
            sug.index < len(row) and parse_price(row[sug.index].value).status == "ok"
            for sug in price_columns
        ):
            return
        applied = False
        for sug in price_columns:
            if sug.index >= len(row):
                continue
            text = row[sug.index].value
            if text is None or not str(text).strip():
                continue
            vat = _vat_from_header(normalize_header(text))
            if vat == "unknown":
                continue
            descriptor = sug.price_descriptor or {"kind": "other", "vat_mode": "unknown"}
            if descriptor.get("vat_mode") in (None, "unknown"):
                descriptor["vat_mode"] = vat
                sug.price_descriptor = descriptor
                # Подпись собирается из обоих ярусов: по ней человек в ревью
                # отличит «с НДС» от «без НДС», а не только по полю.
                sug.header = f"{sug.header} / {str(text).strip()}".strip(" /")
                applied = True
        if applied:
            return


def apply_sheet_vat_statement(grid: SheetGrid, detection: HeaderDetection) -> None:
    """Взять режим НДС из заявления над таблицей, если у колонки его нет.

    Поставщик часто пишет про НДС один раз — строкой «Цены с НДС» над шапкой, а
    не в подписи каждой колонки. По замеру базы у 78 % цен режим НДС не
    определён именно поэтому: в подписи его нет, хотя в документе он сказан.

    Приоритет: подпись колонки → второй ярус шапки → заявление листа. Заявление
    применяется, только когда оно однозначно: если над таблицей сказано и «с
    НДС», и «без НДС», угадывать нельзя — такой лист остаётся неопределённым.
    """
    if detection.header_row_index is None:
        return
    price_columns = [
        sug
        for sug in detection.columns
        if sug.role == "price"
        and (sug.price_descriptor or {}).get("vat_mode") in (None, "unknown")
    ]
    if not price_columns:
        return
    statements = set()
    for row in grid.rows[: detection.header_row_index]:
        for cell in row:
            if cell.is_merged_fill or cell.value is None:
                continue
            text = normalize_header(cell.value)
            # Длинная оговорка («не является офертой… цены указаны…») говорит о
            # чём угодно; заявление о НДС — короткая строка про цены.
            if "ндс" not in text or len(text) > 60:
                continue
            vat = _vat_from_header(text)
            if vat != "unknown":
                statements.add(vat)
    if len(statements) != 1:
        return
    vat = statements.pop()
    for sug in price_columns:
        descriptor = sug.price_descriptor or {"kind": "other", "vat_mode": "unknown"}
        descriptor["vat_mode"] = vat
        sug.price_descriptor = descriptor


def profile_price_columns(grid: SheetGrid, detection: HeaderDetection) -> None:
    """Sanity check: a column suggested as price where fewer than 30% of its
    non-empty body values parse as money gets its confidence downgraded."""
    if detection.header_row_index is None:
        return
    body = grid.rows[detection.header_row_index + 1 :]
    for sug in detection.columns:
        if sug.role != "price" or sug.confidence <= PROFILING_DOWNGRADE:
            continue
        non_empty = 0
        money = 0
        independent_body_value = False
        for row in body:
            if sug.index >= len(row):
                continue
            cell = row[sug.index]
            if cell.value is None or cell.is_merged_fill:
                continue
            independent_body_value = True
            parsed = parse_price(cell.value)
            if parsed.status == "empty":
                continue
            non_empty += 1
            if parsed.status in ("ok", "on_request"):
                money += 1
        # A merged price heading can cover either several real tier columns
        # (keep them) or visual duplicates of one value (discard the fills).
        # The latter has no independent body values and would otherwise make
        # one product acquire duplicate prices or fake horizontal panels.
        if sug.is_merged_fill and not independent_body_value:
            sug.role = "ignore"
            sug.confidence = 0.0
            sug.price_descriptor = None
            continue
        if non_empty > 0 and money / non_empty < 0.3:
            sug.confidence = PROFILING_DOWNGRADE
