"""Deterministic recipes for native text PDFs whose visual layout is a table.

``pdfplumber.extract_tables()`` is deliberately conservative: image columns,
two side-by-side catalogues and whitespace-only Office exports often have no
detected table despite a completely usable text layer.  This module uses a
small, reviewable recipe to reconstruct a *synthetic* grid from word
coordinates and then delegates normalization, row classification and quality
checks to the regular parsing pipeline.

It never performs OCR and never calls a model.  A model may propose the same
``pdf_layout_v1`` recipe later, but that proposal still has to pass the normal
dry-run/validation gate before activation.
"""
from __future__ import annotations

import copy
import io
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

from app.parsing.grid import Cell, SheetGrid
from app.parsing.mapping import (
    ATTRIBUTE_VALUE_KINDS,
    ColumnMapping,
    MappingDecision,
    TableMapping,
    valid_attribute_value_kind,
)
from app.parsing.normalize import currency_code, parse_price, parse_quantity_tier
from app.parsing.pipeline import ParseResult, parse_grids


PDF_LAYOUT_FORMAT = "pdf_layout_v1"
_LINE_Y_TOLERANCE = 0.004
_WS_RE = re.compile(r"\s+")


def _normalise_evidence(value: str) -> str:
    return _WS_RE.sub(
        " ", unicodedata.normalize("NFKC", value).lower().replace("ё", "е")
    ).strip()


def _price_descriptor(column: dict[str, Any]) -> dict[str, str]:
    descriptor = column.get("price_descriptor")
    if not isinstance(descriptor, dict):
        return {"kind": "other", "vat_mode": "unknown"}
    return {
        "kind": str(descriptor.get("kind") or "other"),
        "vat_mode": str(descriptor.get("vat_mode") or "unknown"),
    }


def _column_headers(columns: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, int] = {}
    headers: list[str] = []
    labels = {"name": "Наименование", "price": "Цена", "row_number": "№"}
    for column in columns:
        role = str(column.get("role") or "ignore")
        seen[role] = seen.get(role, 0) + 1
        suffix = f" {seen[role]}" if seen[role] > 1 else ""
        headers.append(str(column.get("header") or labels.get(role, role)) + suffix)
    return headers


@dataclass(frozen=True)
class _Word:
    text: str
    x0: float
    x1: float
    y: float


@dataclass
class _Line:
    y: float
    words: list[_Word]


def _pdf_lines(data: bytes) -> Iterable[tuple[int, list[_Line]]]:
    """Yield stable, normalised word lines per PDF page."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            width = float(page.width or 1)
            height = float(page.height or 1)
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False) or []
            ordered = sorted(
                (
                    _Word(
                        text=str(word.get("text") or ""),
                        x0=float(word.get("x0") or 0) / width,
                        x1=float(word.get("x1") or 0) / width,
                        y=float(word.get("top") or 0) / height,
                    )
                    for word in words
                    if str(word.get("text") or "").strip()
                ),
                key=lambda word: (word.y, word.x0, word.text),
            )
            lines: list[_Line] = []
            for word in ordered:
                if not lines or abs(word.y - lines[-1].y) > _LINE_Y_TOLERANCE:
                    lines.append(_Line(y=word.y, words=[word]))
                else:
                    lines[-1].words.append(word)
            for line in lines:
                line.words.sort(key=lambda word: (word.x0, word.x1, word.text))
            yield page_no, lines


def _in_range(value: float, bounds: object) -> bool:
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
        return False
    try:
        lo, hi = float(bounds[0]), float(bounds[1])
    except (TypeError, ValueError):
        return False
    return lo <= value < hi


def _line_value(line: _Line, column: dict[str, Any]) -> str:
    return " ".join(
        word.text
        for word in line.words
        if _in_range(word.x0, column.get("x_range"))
    ).strip()


def _line_value_centered(line: _Line, column: dict[str, Any]) -> str:
    """Join the words whose horizontal centre falls inside a column.

    Header labels are frequently centred over their column and can start just
    left of its boundary (e.g. ``свыше 50 шт.`` above the right price column),
    so the left-edge test used for data cells would mis-assign them.
    """
    bounds = column.get("x_range")
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
        return ""
    try:
        lo, hi = float(bounds[0]), float(bounds[1])
    except (TypeError, ValueError):
        return ""
    return " ".join(
        word.text
        for word in line.words
        if lo <= (word.x0 + word.x1) / 2 < hi
    ).strip()


def _line_has_price(line: _Line, columns: list[dict[str, Any]]) -> bool:
    for column in columns:
        if str(column.get("role")) != "price":
            continue
        if parse_price(_line_value(line, column)).status in {"ok", "on_request"}:
            return True
    return False


def _detect_vat_mode(text: str) -> str | None:
    """Read an explicit VAT statement from a price group header, if any."""
    s = _normalise_evidence(text)
    if not s:
        return None
    # «Без» проверяется первым: «без учета НДС» иначе попадёт под «с ндс».
    if "без ндс" in s or "без учета ндс" in s or "без учёта ндс" in s:
        return "excluded"
    if "ндс не" in s or "кроме ндс" in s:
        return "excluded"
    if (
        "включая ндс" in s
        or "ндс вкл" in s
        or "в т.ч. ндс" in s
        or "в т.ч ндс" in s
        or "с учетом ндс" in s
        or "с учётом ндс" in s
        or "с ндс" in s
    ):
        return "included"
    return None


def _region_price_column_headers(
    lines: list[_Line], region: dict[str, Any]
) -> dict[str, Any]:
    """Read the table header rows that sit above a region's first data row.

    Returns the quantity-tier label of each price column (e.g. ``до 50 шт.``)
    and the shared price group header that spans the price columns (e.g.
    ``Отпускная цена, руб. (включая НДС)``), which carries the currency and VAT
    evidence for every price column. Only lines with currency/VAT evidence form
    the group header, so tier labels do not pollute it.
    """
    columns = [column for column in region.get("columns", []) if isinstance(column, dict)]
    price_indices = [
        index for index, column in enumerate(columns) if column.get("role") == "price"
    ]
    result: dict[str, Any] = {"group_header": None, "tiers": {}}
    if not price_indices:
        return result
    y_range = region.get("y_range", [0, 1])
    in_range = [line for line in lines if _in_range(line.y, y_range)]
    first_data_y = None
    for line in in_range:
        if _line_has_price(line, columns):
            first_data_y = line.y
            break
    if first_data_y is None:
        return result
    header_lines = [line for line in in_range if line.y < first_data_y]
    if not header_lines:
        return result

    lower = min(float(columns[i].get("x_range", [0.0, 1.0])[0]) for i in price_indices)
    upper = max(float(columns[i].get("x_range", [0.0, 1.0])[1]) for i in price_indices)
    union_column: dict[str, Any] = {"x_range": [lower, upper]}
    group_parts: list[str] = []
    for line in header_lines:
        text = _line_value(line, union_column)
        if not text:
            continue
        if currency_code(text) is not None or "ндс" in _normalise_evidence(text):
            group_parts.append(text)
    if group_parts:
        result["group_header"] = " ".join(group_parts)

    for index in price_indices:
        column = columns[index]
        for line in header_lines:
            text = _line_value_centered(line, column)
            if text and parse_quantity_tier(text) is not None:
                result["tiers"][index] = text
                break
    return result


def _merged_price_descriptor(
    column: dict[str, Any], group_header: str | None
) -> dict[str, str]:
    """Combine a recipe's reviewed price descriptor with header evidence.

    The group header above the price columns can prove the currency and the VAT
    treatment (``Отпускная цена, руб. (включая НДС)``); it wins over the
    recipe's defaults when present.
    """
    descriptor = _price_descriptor(column)
    if group_header:
        currency = currency_code(group_header)
        if currency is not None:
            descriptor["currency"] = currency
        vat_mode = _detect_vat_mode(group_header)
        if vat_mode is not None:
            descriptor["vat_mode"] = vat_mode
    return descriptor


def _region_rows(lines: list[_Line], region: dict[str, Any]) -> list[list[str | None]]:
    columns = [column for column in region.get("columns", []) if isinstance(column, dict)]
    if not columns:
        return []
    name_index = next(
        (index for index, column in enumerate(columns) if column.get("role") == "name"),
        None,
    )
    if name_index is None:
        return []
    name_column = columns[name_index]
    strategy = str(name_column.get("join_lines") or "same_row")
    gap = float(region.get("post_anchor_gap") or 0)
    y_range = region.get("y_range", [0, 1])

    rows: list[list[str | None]] = []
    pending_name_lines: list[str] = []
    active_row: list[str | None] | None = None
    active_anchor_y: float | None = None

    for line in lines:
        if not _in_range(line.y, y_range):
            continue
        if not _line_has_price(line, columns):
            name_text = _line_value(line, name_column)
            if not name_text:
                continue
            if (
                active_row is not None
                and strategy == "until_price_anchor_and_after_gap"
                and active_anchor_y is not None
                and line.y - active_anchor_y <= gap
            ):
                previous = active_row[name_index] or ""
                active_row[name_index] = " ".join(
                    value for value in (previous, name_text) if value
                )
            elif strategy != "same_row":
                pending_name_lines.append(name_text)
            continue

        values: list[str | None] = []
        for column in columns:
            raw = _line_value(line, column)
            values.append(raw or None)
        same_line_name = values[name_index] or ""
        if strategy in {"until_price_anchor", "until_price_anchor_and_after_gap"}:
            values[name_index] = " ".join(
                value for value in (*pending_name_lines, same_line_name) if value
            ) or None
        if values[name_index] is None:
            pending_name_lines.clear()
            continue
        rows.append(values)
        active_row = values
        active_anchor_y = line.y
        pending_name_lines.clear()
    return rows


def _row_number_starts(value: str) -> bool:
    """A PDF may glue ``100`` to the following word during extraction."""
    return bool(re.match(r"^\d{1,4}(?:\D|$)", value.strip()))


def _row_number_suffix(value: str) -> str:
    """Return a product prefix glued to its row number by PDF extraction."""
    return re.sub(r"^\d{1,4}\s*", "", value.strip()).strip()


def _region_rows_grouped_by_number(
    lines: list[_Line], region: dict[str, Any]
) -> list[list[str | None]]:
    """Rebuild multi-line logical rows whose leftmost column is a row number.

    This is safer than joining arbitrary preceding lines: section captions in
    the Тунгус PDF have numbers but no price, while a wrapped item retains its
    number across all of its visual lines.
    """
    columns = [column for column in region.get("columns", []) if isinstance(column, dict)]
    row_number_column = next(
        (column for column in columns if column.get("role") == "row_number"), None
    )
    name_index = next(
        (index for index, column in enumerate(columns) if column.get("role") == "name"), None
    )
    if row_number_column is None or name_index is None:
        return []
    y_range = region.get("y_range", [0, 1])
    grouped: list[list[_Line]] = []
    active: list[_Line] | None = None
    for line in lines:
        if not _in_range(line.y, y_range):
            continue
        row_number = _line_value(line, row_number_column)
        if _row_number_starts(row_number):
            if active:
                grouped.append(active)
            active = [line]
        elif active:
            active.append(line)
    if active:
        grouped.append(active)

    row_number_index = next(
        (index for index, column in enumerate(columns) if column.get("role") == "row_number"),
        None,
    )
    rows: list[list[str | None]] = []
    for group in grouped:
        values = [
            " ".join(_line_value(line, column) for line in group if _line_value(line, column)).strip()
            or None
            for column in columns
        ]
        row_number = _line_value(group[0], row_number_column)
        glued_name = _row_number_suffix(row_number)
        if glued_name:
            values[name_index] = " ".join(
                value for value in (glued_name, values[name_index] or "") if value
            )
        if values[name_index] is None:
            continue
        if not any(
            parse_price(values[index]).status in {"ok", "on_request"}
            for index, column in enumerate(columns)
            if column.get("role") == "price"
        ):
            # A numbered caption without a price is a section header. Keep it,
            # with the row number cleared, so the pipeline turns it into
            # category context for the products that follow instead of losing
            # the only category evidence an article-only position has.
            if row_number_index is not None:
                values[row_number_index] = None
            rows.append(values)
            continue
        rows.append(values)
    return rows


def is_pdf_layout_recipe(recipe: dict[str, Any]) -> bool:
    return isinstance(recipe, dict) and recipe.get("format") == PDF_LAYOUT_FORMAT


def _validate_recipe(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    if not is_pdf_layout_recipe(recipe):
        raise ValueError(f"Ожидается рецепт format={PDF_LAYOUT_FORMAT}")
    regions = recipe.get("regions")
    if not isinstance(regions, list) or not regions:
        raise ValueError("В layout-рецепте должна быть хотя бы одна region")
    valid_regions: list[dict[str, Any]] = []
    for region in regions:
        if not isinstance(region, dict):
            continue
        columns = region.get("columns")
        if not isinstance(columns, list):
            continue
        for column in columns:
            if not isinstance(column, dict):
                continue
            value_kind = column.get("attribute_value_kind")
            if value_kind is not None and value_kind not in ATTRIBUTE_VALUE_KINDS:
                raise ValueError(f"Недопустимый тип характеристики: {value_kind}")
        roles = {str(column.get("role")) for column in columns if isinstance(column, dict)}
        if "name" not in roles or "price" not in roles:
            continue
        valid_regions.append(region)
    if not valid_regions:
        raise ValueError("Ни одна region не содержит обязательные name и price")
    return valid_regions


def validate_pdf_layout_recipe(recipe: dict[str, Any]) -> None:
    """Validate the shape of a review-approved layout recipe before storage."""
    _validate_recipe(recipe)


def parse_pdf_layout(data: bytes, recipe: dict[str, Any]) -> ParseResult:
    """Apply a safe ``pdf_layout_v1`` recipe and reuse the normal pipeline."""
    regions = _validate_recipe(recipe)
    grids: list[SheetGrid] = []
    tables: list[TableMapping] = []
    all_pages = list(_pdf_lines(data))
    for region_index, region in enumerate(regions, start=1):
        columns = [column for column in region["columns"] if isinstance(column, dict)]
        raw_rows: list[list[str | None]] = []
        header_info: dict[str, Any] | None = None
        for _page_no, lines in all_pages:
            if region.get("row_grouping") == "row_number":
                page_rows = _region_rows_grouped_by_number(lines, region)
            else:
                page_rows = _region_rows(lines, region)
            raw_rows.extend(page_rows)
            if header_info is None and page_rows:
                header_info = _region_price_column_headers(lines, region)
        header_info = header_info or {"group_header": None, "tiers": {}}
        group_header = header_info.get("group_header")
        tiers: dict[int, str] = header_info.get("tiers", {})

        headers = _column_headers(columns)
        for index, column in enumerate(columns):
            if column.get("role") == "price" and index in tiers:
                headers[index] = tiers[index]

        grid_name = f"pdf_layout_{region_index}"
        rows = [
            [Cell(value=value) for value in headers],
            *[[Cell(value=value) for value in row] for row in raw_rows],
        ]
        grids.append(SheetGrid(name=grid_name, rows=rows))

        column_mappings: list[ColumnMapping] = []
        for index, column in enumerate(columns):
            role = str(column.get("role") or "ignore")
            descriptor: dict[str, str] | None = None
            tier = None
            if role == "price":
                descriptor = _merged_price_descriptor(column, group_header)
                if index in tiers:
                    tier = parse_quantity_tier(tiers[index])
            column_mappings.append(
                ColumnMapping(
                    index=index,
                    role=role,
                    price_descriptor=descriptor,
                    header=headers[index],
                    attribute_code=(
                        str(column["attribute_code"])
                        if column.get("attribute_code")
                        else None
                    ),
                    attribute_value_kind=valid_attribute_value_kind(
                        column.get("attribute_value_kind")
                    ),
                    canonical_unit=(
                        str(column["canonical_unit"])
                        if column.get("canonical_unit")
                        else None
                    ),
                    tier_min_quantity=tier.min_quantity if tier else None,
                    tier_max_quantity=tier.max_quantity if tier else None,
                )
            )
        tables.append(
            TableMapping(
                sheet_name=grid_name,
                header_row_index=0,
                columns=column_mappings,
            )
        )
    decision = MappingDecision(tables=tables, auto_ok=bool(tables))
    return parse_grids(grids, decision)


def _pdf_text_evidence(data: bytes, *, max_pages: int = 3) -> str:
    import pdfplumber

    fragments: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages[:max_pages]:
            fragments.append(page.extract_text() or "")
    return _normalise_evidence("\n".join(fragments))


def recipe_matches_pdf_layout(data: bytes, recipe: dict[str, Any]) -> bool:
    """Match only explicit, stable evidence supplied by a recipe/profile."""
    if not is_pdf_layout_recipe(recipe):
        return False
    match = recipe.get("match")
    required = match.get("required_text") if isinstance(match, dict) else None
    if not isinstance(required, list) or not required:
        return False
    evidence = _pdf_text_evidence(data)
    return all(_normalise_evidence(str(needle)) in evidence for needle in required)


_VAT_INCLUDED = {"kind": "wholesale", "vat_mode": "included"}

# Profiles are intentionally matched by text/geometry, never file names.  They
# cover the three native text PDF shapes in the mandatory corpus and serve as
# concise examples for externally proposed rules.
_BUILTIN_PDF_LAYOUT_RECIPES: tuple[dict[str, Any], ...] = (
    {
        "format": PDF_LAYOUT_FORMAT,
        "name": "НПК Огнеборец — две параллельные таблицы",
        "match": {"required_text": ["огнетуш", "баллоны для технических газов", "цена, руб."]},
        "regions": [
            {
                "name": "огнетушители",
                "y_range": [0.31, 0.76],
                "columns": [
                    {"role": "name", "x_range": [0.09, 0.30], "join_lines": "same_row"},
                    {"role": "price", "x_range": [0.30, 0.41], "price_descriptor": _VAT_INCLUDED},
                ],
            },
            {
                "name": "баллоны",
                "y_range": [0.31, 0.76],
                "post_anchor_gap": 0.03,
                "columns": [
                    {
                        "role": "name",
                        "x_range": [0.41, 0.79],
                        "join_lines": "until_price_anchor_and_after_gap",
                    },
                    {"role": "price", "x_range": [0.79, 1.0], "price_descriptor": _VAT_INCLUDED},
                ],
            },
        ],
    },
    {
        "format": PDF_LAYOUT_FORMAT,
        "name": "Whitespace прайс: размер-вес-объём-цена",
        "match": {"required_text": ["размер", "вес (кг)", "объем (м3)", "цена"]},
        "regions": [
            {
                "name": "товары",
                "y_range": [0.09, 0.90],
                "columns": [
                    {"role": "name", "x_range": [0.05, 0.45], "join_lines": "until_price_anchor"},
                    {"role": "price", "x_range": [0.75, 1.0], "price_descriptor": _VAT_INCLUDED},
                ],
            }
        ],
    },
    {
        "format": PDF_LAYOUT_FORMAT,
        "name": "Тунгус — две ценовые ступени",
        "match": {"required_text": ["прайс-лист, россия", "отпускная цена", "до 50 шт."]},
        "regions": [
            {
                "name": "товары",
                "y_range": [0.02, 0.99],
                "row_grouping": "row_number",
                "columns": [
                    {"role": "row_number", "x_range": [0.00, 0.07]},
                    {"role": "name", "x_range": [0.06, 0.80], "join_lines": "same_row"},
                    {"role": "price", "x_range": [0.80, 0.88], "price_descriptor": _VAT_INCLUDED},
                    {"role": "price", "x_range": [0.88, 1.0], "price_descriptor": _VAT_INCLUDED},
                ],
            }
        ],
    },
)


def find_builtin_pdf_layout_recipe(data: bytes, filename: str) -> dict[str, Any] | None:
    """Return a copy of a matching bundled rule; ``filename`` is ignored.

    Keeping the argument makes it easy for callers to share the same interface
    with future profile registries, while the explicit discard prevents a
    tempting but unstable filename fallback.
    """
    del filename
    for recipe in _BUILTIN_PDF_LAYOUT_RECIPES:
        if recipe_matches_pdf_layout(data, recipe):
            return copy.deepcopy(recipe)
    return None
