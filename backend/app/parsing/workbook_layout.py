"""Deterministic layouts for workbooks with several independent price panels.

Most spreadsheets can safely travel through header detection.  Some supplier
files intentionally place two catalogues next to each other or use a price
matrix.  Treating such a sheet as one wide table attaches values from the
right panel to a name from the left panel.  This module describes those
layouts explicitly, reconstructs a narrow synthetic catalogue and delegates
normalisation and validation to the regular parsing pipeline.

The bundled profile is matched from workbook structure and sheet contents;
the uploaded filename is never involved.  It is deliberately data-only: no
OCR and no model call are needed to apply it.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from app.parsing.grid import Cell, SheetGrid
from app.parsing.headers import normalize_header
from app.parsing.mapping import (
    ColumnMapping,
    MappingDecision,
    TableMapping,
    build_auto_mapping,
    default_category_from_sheet_name,
)
from app.parsing.normalize import parse_price
from app.parsing.pipeline import ParseResult, parse_grids


WORKBOOK_LAYOUT_FORMAT = "workbook_layout_v1"
_PRICE_DESCRIPTOR = {"kind": "other", "vat_mode": "unknown"}


@dataclass(frozen=True)
class _CatalogPrice:
    label: str
    value: object
    number_format: str | None


@dataclass(frozen=True)
class _CatalogRow:
    """One logical source product, ready for a narrow synthetic grid."""

    name: str
    unit: object | None
    prices: tuple[_CatalogPrice, ...]
    source_sheet: str
    source_row: int
    source_panel_index: int = 0
    category_path: str | None = None


def is_workbook_layout_recipe(recipe: dict[str, Any]) -> bool:
    return isinstance(recipe, dict) and recipe.get("format") == WORKBOOK_LAYOUT_FORMAT


def _text(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _cell(grid: SheetGrid, row_index: int, column_index: int) -> Cell | None:
    if not (0 <= row_index < len(grid.rows)):
        return None
    row = grid.rows[row_index]
    if not (0 <= column_index < len(row)):
        return None
    return row[column_index]


def _original_value(grid: SheetGrid, row_index: int, column_index: int) -> object | None:
    cell = _cell(grid, row_index, column_index)
    if cell is None or cell.is_merged_fill:
        return None
    return cell.value


def _catalog_price(
    grid: SheetGrid,
    row_index: int,
    column_index: int,
    label: str,
) -> _CatalogPrice | None:
    cell = _cell(grid, row_index, column_index)
    if cell is None or cell.is_merged_fill or not _safe_price(cell.value):
        return None
    return _CatalogPrice(label=label, value=cell.value, number_format=cell.number_format)


def _safe_price(value: object | None) -> bool:
    """Accept a price cell, not a prose note which happens to say 'запрос'."""
    parsed = parse_price(value)
    if parsed.status == "ok":
        return True
    if parsed.status != "on_request":
        return False
    # A price cell may be ``По запросу``; an explanatory paragraph containing
    # the same words must never become a product price.
    return len(str(value).strip()) <= 40


def _standard_sheet_signature(grid: SheetGrid) -> bool:
    if grid.n_cols != 7:
        return False
    for row in grid.rows[:12]:
        values = [_text(cell.value) or "" for cell in row]
        if len(values) < 7:
            continue
        if (
            "наименование" in values[0].lower()
            and "изм" in values[1].lower()
            and "расч" in values[2].lower()
            and "наименование" in values[4].lower()
            and "изм" in values[5].lower()
            and "расч" in values[6].lower()
        ):
            return True
    return False


def _header_panels(grid: SheetGrid, row_index: int) -> list[dict[str, Any]]:
    """Return isolated panels announced by a normal price-list header row."""
    if not (0 <= row_index < len(grid.rows)):
        return []
    name_columns: list[int] = []
    price_columns: list[int] = []
    unit_columns: list[int] = []
    for column_index, cell in enumerate(grid.rows[row_index]):
        if cell.is_merged_fill:
            continue
        header = normalize_header(cell.value)
        if "наименование" in header:
            name_columns.append(column_index)
        elif "цена" in header:
            price_columns.append(column_index)
        elif header in {"изм", "ед изм", "единица измерения"}:
            unit_columns.append(column_index)

    panels: list[dict[str, Any]] = []
    for index, name_column in enumerate(name_columns):
        boundary = name_columns[index + 1] if index + 1 < len(name_columns) else grid.n_cols
        panel_prices = [
            column for column in price_columns if name_column < column < boundary
        ]
        if not panel_prices:
            continue
        panel_units = [column for column in unit_columns if name_column < column < boundary]
        panels.append(
            {
                "name": name_column,
                "unit": panel_units[0] if panel_units else None,
                "prices": panel_prices,
            }
        )
    return panels


def _named_price_header_rows(grid: SheetGrid) -> list[int]:
    return [
        row_index
        for row_index in range(len(grid.rows))
        if _header_panels(grid, row_index)
    ]


def recipe_matches_workbook_layout(grids: list[SheetGrid], recipe: dict[str, Any]) -> bool:
    """Return whether a layout's explicit, stable workbook evidence matches."""
    if not is_workbook_layout_recipe(recipe):
        return False
    if recipe.get("profile") == "generated_matrix_v1":
        regions = recipe.get("matrix_regions")
        if not isinstance(regions, list) or not regions:
            return False
        names = {normalize_header(grid.name) for grid in grids}
        return all(
            isinstance(region, dict)
            and isinstance((region_match := region.get("match")), dict)
            and normalize_header(region_match.get("name_pattern") or "") in names
            for region in regions
        )
    match = recipe.get("match")
    if not isinstance(match, dict):
        return False
    expected = match.get("required_sheet_names")
    if not isinstance(expected, list) or not expected:
        return False
    names = {grid.name for grid in grids}
    if not all(str(name) in names for name in expected):
        return False
    if "minimum_standard_panels" in match:
        try:
            return sum(_standard_sheet_signature(grid) for grid in grids) >= int(
                match["minimum_standard_panels"]
            )
        except (TypeError, ValueError):
            return False
    if "minimum_named_price_headers" in match:
        try:
            return sum(bool(_named_price_header_rows(grid)) for grid in grids) >= int(
                match["minimum_named_price_headers"]
            )
        except (TypeError, ValueError):
            return False
    return True


def _panel_rows(grid: SheetGrid, panels: list[dict[str, Any]]) -> list[_CatalogRow]:
    rows: list[_CatalogRow] = []
    for panel_index, panel in enumerate(panels):
        try:
            name_col = int(panel["name"])
            unit_col = int(panel["unit"])
            price_col = int(panel["price"])
        except (KeyError, TypeError, ValueError):
            continue
        inherited_name: str | None = None
        for row_index in range(len(grid.rows)):
            name_cell = _cell(grid, row_index, name_col)
            raw_name = _original_value(grid, row_index, name_col)
            name = _text(raw_name)
            if name is not None:
                inherited_name = name
            elif name_cell is not None and name_cell.is_merged_fill:
                # Vertical merges can hold one product name for several price
                # rows.  Inherit only inside the same logical panel.
                name = inherited_name
            elif panel.get("inherit_blank_name"):
                # Some wide catalogues omit a repeated model name in its
                # alternate configuration row.  This opt-in is only used by
                # the reviewed upper panel; it never crosses panel bounds.
                name = inherited_name
            if name is None:
                continue
            price = _catalog_price(grid, row_index, price_col, "Расч.")
            if price is None:
                continue
            rows.append(
                _CatalogRow(
                    name=name,
                    unit=_original_value(grid, row_index, unit_col),
                    prices=(price,),
                    source_sheet=grid.name,
                    source_row=row_index + 1,
                    source_panel_index=panel_index,
                )
            )
    return rows


def _matrix_name(product: str, outlet: object | None) -> str:
    outlet_text = _text(outlet)
    if outlet_text is None:
        return product
    if outlet_text.lower().endswith("мм"):
        return f"{product} (отросток {outlet_text})"
    return f"{product} (отросток {outlet_text} мм)"


def _section_path(sheet_name: str, caption: str | None = None) -> str:
    """Build a source-derived category path for an isolated layout section."""
    if caption and normalize_header(caption) != normalize_header(sheet_name):
        return f"{sheet_name} / {caption}"
    return sheet_name


def _filled_caption(grid: SheetGrid, row_index: int, column_index: int = 0) -> str | None:
    """Return a visually explicit section caption from a source cell."""
    cell = _cell(grid, row_index, column_index)
    value = _text(_original_value(grid, row_index, column_index))
    if (
        value is None
        or cell is None
        or not cell.is_bold
        or cell.fill_type != "solid"
    ):
        return None
    return value


def _matrix_rows(grid: SheetGrid, mapping: dict[str, Any]) -> list[_CatalogRow]:
    try:
        name_col = int(mapping["name"])
        unit_col = int(mapping["unit"])
        outlet_col = int(mapping["outlet"])
        price_columns = [int(index) for index in mapping["price_columns"]]
    except (KeyError, TypeError, ValueError):
        return []

    rows: list[_CatalogRow] = []
    current_product: str | None = None
    current_unit: object | None = None
    for row_index in range(len(grid.rows)):
        row_name = _text(_original_value(grid, row_index, name_col))
        if row_name is not None:
            current_product = row_name
            current_unit = _original_value(grid, row_index, unit_col)

        outlet = _original_value(grid, row_index, outlet_col)
        candidates = [
            (column, _original_value(grid, row_index, column))
            for column in price_columns
        ]
        candidates = [(column, price) for column, price in candidates if _safe_price(price)]
        # In the final Колено row J is no longer a dimension axis: it contains
        # one more literal ``По запросу`` price.  Promote it back to a price
        # cell instead of rendering it as an outlet description.
        if _safe_price(outlet) and parse_price(outlet).status == "on_request":
            candidates.append((outlet_col, outlet))
            outlet = None
        if not candidates or current_product is None:
            continue

        if outlet is not None:
            # Matrix data row.  Its outlet axis (J) is descriptive and is
            # not a price, so each remaining price cell becomes one variant.
            item_name = _matrix_name(current_product, outlet)
        elif row_name is not None:
            # Direct product rows use monetary values or short request values.
            # Header rows only repeat small diameters (100, 150, …) and are
            # excluded by this guard.
            if not any(
                parse_price(value).status == "on_request"
                or (parse_price(value).amount is not None and parse_price(value).amount > 1000)
                for _column, value in candidates
            ):
                continue
            item_name = current_product
        else:
            # The final Колено row carries thirteen short request cells but
            # no repeated product name or outlet axis.
            if not all(parse_price(value).status == "on_request" for _column, value in candidates):
                continue
            item_name = current_product

        for column, price in candidates:
            rows.append(
                _CatalogRow(
                    name=item_name,
                    unit=current_unit,
                    prices=(
                        _CatalogPrice(
                            label=f"Расч. {column + 1}",
                            value=price,
                            number_format=(
                                _cell(grid, row_index, column).number_format
                                if _cell(grid, row_index, column) is not None
                                else None
                            ),
                        ),
                    ),
                    source_sheet=grid.name,
                    source_row=row_index + 1,
                )
            )

    simple = mapping.get("simple_panel")
    if not isinstance(simple, dict):
        return rows
    rows.extend(_panel_rows(grid, [simple]))
    return rows


def _ordinary_rows(
    grid: SheetGrid,
    configured_sections: list[dict[str, Any]] | None = None,
) -> list[_CatalogRow]:
    """Extract one logical item per row from normal, possibly parallel panels."""
    rows: list[_CatalogRow] = []
    detected_header_rows = _named_price_header_rows(grid)
    if configured_sections is None:
        section_mappings = [
            {"header_row": header_row, "panels": _header_panels(grid, header_row)}
            for header_row in detected_header_rows
        ]
    else:
        section_mappings = configured_sections
    resolved_sections: list[tuple[int, list[dict[str, Any]]]] = []
    for section_index, section in enumerate(section_mappings):
        if not isinstance(section, dict):
            continue
        configured_header = section.get("header_row")
        header_row = (
            configured_header
            if isinstance(configured_header, int)
            and not isinstance(configured_header, bool)
            and configured_header in detected_header_rows
            else (
                detected_header_rows[section_index]
                if section_index < len(detected_header_rows)
                else None
            )
        )
        panels = section.get("panels")
        if header_row is None or not isinstance(panels, list):
            continue
        resolved_sections.append((header_row, [panel for panel in panels if isinstance(panel, dict)]))

    for header_position, (header_row, panels) in enumerate(resolved_sections):
        end_row = (
            resolved_sections[header_position + 1][0]
            if header_position + 1 < len(resolved_sections)
            else len(grid.rows)
        )
        for panel_index, panel in enumerate(panels):
            name_column = int(panel["name"])
            unit_column = panel.get("unit")
            price_columns = [int(column) for column in panel["prices"]]
            labels = {
                column: _text(_original_value(grid, header_row, column))
                or f"Цена {column + 1}"
                for column in price_columns
            }
            current_category: str | None = None
            pending_category: str | None = None
            pending_category_tint: float | None = None
            for row_index in range(header_row + 1, end_row):
                name_cell = _cell(grid, row_index, name_column)
                name = _text(name_cell.value) if name_cell is not None else None
                if name is None:
                    continue
                prices = tuple(
                    _CatalogPrice(
                        label=labels[column],
                        value=value,
                        number_format=(
                            _cell(grid, row_index, column).number_format
                            if _cell(grid, row_index, column) is not None
                            else None
                        ),
                    )
                    for column in price_columns
                    if _safe_price(value := _original_value(grid, row_index, column))
                )
                if not prices:
                    unit = (
                        _original_value(grid, row_index, int(unit_column))
                        if isinstance(unit_column, int)
                        else None
                    )
                    # Multi-panel source sheets announce a new catalogue
                    # section with a bold, filled caption merged across its
                    # panel.  Keep the first caption in a consecutive group:
                    # suppliers often follow it with an equally formatted
                    # explanatory note.  This is structural evidence only;
                    # no supplier names or row coordinates enter the rule.
                    panel_end = (
                        min(price_columns) + 1 if price_columns else name_column + 1
                    )
                    has_merged_span = any(
                        cell.is_merged_fill
                        for cell in grid.rows[row_index][name_column:panel_end]
                    )
                    if (
                        name is not None
                        and unit is None
                        and name_cell is not None
                        and name_cell.is_bold
                        and name_cell.fill_type == "solid"
                        and has_merged_span
                    ):
                        candidate_tint = name_cell.fill_tint
                        # A darker caption supersedes a preceding light
                        # informational banner.  Equally formatted consecutive
                        # rows keep the first caption and treat the following
                        # rows as notes.
                        if (
                            pending_category is None
                            or (
                                candidate_tint is not None
                                and pending_category_tint is not None
                                and candidate_tint < pending_category_tint
                            )
                        ):
                            pending_category = name
                            pending_category_tint = candidate_tint
                    continue
                if pending_category is not None:
                    current_category = pending_category
                    pending_category = None
                    pending_category_tint = None
                unit = (
                    _original_value(grid, row_index, int(unit_column))
                    if isinstance(unit_column, int)
                    else None
                )
                rows.append(
                    _CatalogRow(
                        name=name,
                        unit=unit,
                        prices=prices,
                        source_sheet=grid.name,
                        source_row=row_index + 1,
                        source_panel_index=panel_index,
                        category_path=current_category,
                    )
                )
    return rows


def _price_new_matrix_rows(grid: SheetGrid, mapping: dict[str, Any]) -> list[_CatalogRow]:
    """Group the diameter matrix into logical product rows with price variants."""
    try:
        name_column = int(mapping["name"])
        unit_column = int(mapping["unit"])
        outlet_column = int(mapping["outlet"])
        price_columns = [int(column) for column in mapping["price_columns"]]
    except (KeyError, TypeError, ValueError):
        return []

    rows: list[_CatalogRow] = []
    current_product: str | None = None
    current_unit: object | None = None
    current_section: str | None = None
    for row_index in range(len(grid.rows)):
        section = _filled_caption(grid, row_index)
        if section is not None and "наименован" not in normalize_header(section):
            current_section = section
        row_name = _text(_original_value(grid, row_index, name_column))
        if row_name is not None:
            current_product = row_name
            current_unit = _original_value(grid, row_index, unit_column)

        outlet = _original_value(grid, row_index, outlet_column)
        candidates = [
            (column, f"Расч. {column + 1}", _original_value(grid, row_index, column))
            for column in price_columns
        ]
        candidates = [
            (column, label, value)
            for column, label, value in candidates
            if _safe_price(value)
        ]
        if _safe_price(outlet) and parse_price(outlet).status == "on_request":
            candidates.append((outlet_column, f"Расч. {outlet_column + 1}", outlet))
            outlet = None
        if not candidates or current_product is None:
            continue

        if outlet is not None:
            item_name = _matrix_name(current_product, outlet)
        elif row_name is not None:
            # The product rows carry real money or a short request marker;
            # diameter header rows only contain small axis values.
            if not any(
                parse_price(value).status == "on_request"
                or (parse_price(value).amount is not None and parse_price(value).amount > 1000)
                for _column, _label, value in candidates
            ):
                continue
            item_name = current_product
        else:
            # The final Колено row is a compact row of thirteen request cells.
            if not all(
                parse_price(value).status == "on_request"
                for _column, _label, value in candidates
            ):
                continue
            item_name = current_product

        rows.append(
            _CatalogRow(
                name=item_name,
                unit=current_unit,
                prices=tuple(
                    _CatalogPrice(
                        label=label,
                        value=value,
                        number_format=(
                            _cell(grid, row_index, column).number_format
                            if _cell(grid, row_index, column) is not None
                            else None
                        ),
                    )
                    for column, label, value in candidates
                ),
                source_sheet=grid.name,
                source_row=row_index + 1,
                category_path=_section_path(grid.name, current_section),
            )
        )

    simple = mapping.get("simple_panel")
    if not isinstance(simple, dict):
        return rows
    try:
        simple_name = int(simple["name"])
        simple_unit = int(simple["unit"])
        simple_prices = [int(column) for column in simple["prices"]]
    except (KeyError, TypeError, ValueError):
        return rows
    for row_index in range(len(grid.rows)):
        name = _text(_original_value(grid, row_index, simple_name))
        if name is None:
            continue
        prices = tuple(
            _CatalogPrice(
                label=f"Цена {column + 1}",
                value=value,
                number_format=(
                    _cell(grid, row_index, column).number_format
                    if _cell(grid, row_index, column) is not None
                    else None
                ),
            )
            for column in simple_prices
            if _safe_price(value := _original_value(grid, row_index, column))
        )
        if prices:
            rows.append(
                _CatalogRow(
                    name=name,
                    unit=_original_value(grid, row_index, simple_unit),
                    prices=prices,
                    source_sheet=grid.name,
                    source_row=row_index + 1,
                    category_path=_section_path(grid.name),
                )
            )
    return rows


def workbook_layout_header_coordinates(
    grids: list[SheetGrid], recipe: dict[str, Any]
) -> set[tuple[str, int]]:
    """Return physical matrix-axis rows proven to be non-position headers.

    In a diameter matrix the first row of each product block repeats the
    product label and places an increasing series of nominal diameters in the
    same columns used by prices below it.  A generic numeric-cell audit would
    otherwise report those axis labels as missed prices.  The evidence is
    intentionally strict: a matched deterministic layout, at least two exact
    integer values, and a strictly increasing sequence.
    """
    mapping = (
        recipe.get("hydrant_matrix")
        if recipe.get("profile") == "price_new_v1"
        else recipe.get("matrix")
    )
    if not isinstance(mapping, dict):
        return set()
    try:
        sheet_name = str(mapping["sheet"])
        name_column = int(mapping["name"])
        outlet_column = int(mapping["outlet"])
        price_columns = [int(column) for column in mapping["price_columns"]]
    except (KeyError, TypeError, ValueError):
        return set()
    grid = next((candidate for candidate in grids if candidate.name == sheet_name), None)
    if grid is None:
        return set()

    coordinates: set[tuple[str, int]] = set()
    for row_index in range(len(grid.rows)):
        if _text(_original_value(grid, row_index, name_column)) is None:
            continue
        if _original_value(grid, row_index, outlet_column) is not None:
            continue
        amounts = []
        for column in price_columns:
            parsed = parse_price(_original_value(grid, row_index, column))
            if parsed.status == "ok" and parsed.amount is not None:
                amounts.append(parsed.amount)
        if (
            len(amounts) >= 2
            and all(amount == amount.to_integral_value() for amount in amounts)
            and all(left < right for left, right in zip(amounts, amounts[1:]))
        ):
            coordinates.add((grid.name, row_index + 1))
    return coordinates


def _price_new_service_rows(grid: SheetGrid, mapping: dict[str, Any]) -> list[_CatalogRow]:
    try:
        linear = mapping["linear"]
        sign = mapping["sign"]
        linear_name = int(linear["name"])
        linear_unit = int(linear["unit"])
        linear_price = int(linear["price"])
        sign_name = int(sign["name"])
        sign_prices = [int(column) for column in sign["price_columns"]]
    except (KeyError, TypeError, ValueError):
        return []

    rows: list[_CatalogRow] = []
    category: str | None = None
    for row_index in range(len(grid.rows)):
        name = _text(_original_value(grid, row_index, linear_name))
        price = _catalog_price(grid, row_index, linear_price, "Расч.")
        if price is None:
            caption = _filled_caption(grid, row_index, linear_name)
            if caption is not None:
                category = caption
        if name is not None and price is not None:
            rows.append(
                _CatalogRow(
                    name=name,
                    unit=_original_value(grid, row_index, linear_unit),
                    prices=(price,),
                    source_sheet=grid.name,
                    source_row=row_index + 1,
                    category_path=_section_path(grid.name, category),
                )
            )

    category = None
    for row_index in range(len(grid.rows)):
        name = _text(_original_value(grid, row_index, sign_name))
        prices = tuple(
            _CatalogPrice(
                label=f"Материал {column}",
                value=value,
                number_format=(
                    _cell(grid, row_index, column).number_format
                    if _cell(grid, row_index, column) is not None
                    else None
                ),
            )
            for column in sign_prices
            if _safe_price(value := _original_value(grid, row_index, column))
        )
        if prices and name is not None:
            prefix = f"{category} — " if category else ""
            rows.append(
                _CatalogRow(
                    name=f"{prefix}{name}",
                    unit=None,
                    prices=prices,
                    source_sheet=grid.name,
                    source_row=row_index + 1,
                    category_path=_section_path(grid.name, category),
                )
            )
        elif name is not None and normalize_header(name) != "размер":
            caption = _filled_caption(grid, row_index, sign_name)
            if caption is not None:
                category = caption
    return rows


def _price_new_rows(grids: list[SheetGrid], recipe: dict[str, Any]) -> list[_CatalogRow]:
    excluded = {str(name) for name in recipe.get("ordinary_exclusions", [])}
    configured = recipe.get("ordinary_sheets")
    configured_by_sheet = {
        str(sheet.get("sheet")): sheet.get("sections")
        for sheet in configured
        if isinstance(sheet, dict) and isinstance(sheet.get("sections"), list)
    } if isinstance(configured, list) else None
    rows = []
    for grid in grids:
        if grid.name in excluded:
            continue
        if configured_by_sheet is not None:
            sections = configured_by_sheet.get(grid.name)
            if not isinstance(sections, list):
                continue
            rows.extend(_ordinary_rows(grid, sections))
        else:
            rows.extend(_ordinary_rows(grid))
    rows.extend(_price_new_special_rows(grids, recipe))
    return rows


def _price_new_special_rows(
    grids: list[SheetGrid], recipe: dict[str, Any], *, include_hydrant: bool = True
) -> list[_CatalogRow]:
    rows: list[_CatalogRow] = []
    hydrant = recipe.get("hydrant_matrix")
    service = recipe.get("service_matrix")
    for grid in grids:
        if (
            include_hydrant
            and isinstance(hydrant, dict)
            and grid.name == hydrant.get("sheet")
        ):
            rows.extend(_price_new_matrix_rows(grid, hydrant))
        if isinstance(service, dict) and grid.name == service.get("sheet"):
            rows.extend(_price_new_service_rows(grid, service))
    return rows


def _validate_matrix_regions(regions: object) -> None:
    if not isinstance(regions, list) or not regions:
        raise ValueError("В generated matrix rule отсутствуют matrix_regions")
    if any(
        not isinstance(region, dict)
        or region.get("format") != "matrix-unfold-v1"
        or not isinstance(region.get("blocks"), list)
        or not region["blocks"]
        for region in regions
    ):
        raise ValueError("В generated matrix rule некорректный matrix region")


def _validate_recipe(recipe: dict[str, Any]) -> None:
    if not is_workbook_layout_recipe(recipe):
        raise ValueError(f"Ожидается рецепт format={WORKBOOK_LAYOUT_FORMAT}")
    if recipe.get("profile") == "generated_matrix_v1":
        sheets = recipe.get("sheets")
        regions = recipe.get("matrix_regions")
        if not isinstance(sheets, list):
            raise ValueError("В generated matrix rule отсутствует список sheets")
        _validate_matrix_regions(regions)
        return
    if recipe.get("profile") == "price_new_v1":
        if not isinstance(recipe.get("hydrant_matrix"), dict):
            raise ValueError("В layout-рецепте отсутствует блок hydrant_matrix")
        if not isinstance(recipe.get("service_matrix"), dict):
            raise ValueError("В layout-рецепте отсутствует блок service_matrix")
        if "matrix_regions" in recipe:
            _validate_matrix_regions(recipe["matrix_regions"])
        ordinary_sheets = recipe.get("ordinary_sheets")
        if ordinary_sheets is not None:
            if not isinstance(ordinary_sheets, list) or not ordinary_sheets:
                raise ValueError("ordinary_sheets должен быть непустым списком")
            for sheet in ordinary_sheets:
                if not isinstance(sheet, dict) or not isinstance(sheet.get("sheet"), str):
                    raise ValueError("В ordinary_sheets отсутствует имя листа")
                sections = sheet.get("sections")
                if not isinstance(sections, list) or not sections:
                    raise ValueError("В ordinary_sheets отсутствуют секции")
                for section in sections:
                    if not isinstance(section, dict):
                        raise ValueError("В ordinary_sheets некорректная секция")
                    panels = section.get("panels")
                    if not isinstance(section.get("header_row"), int) or not isinstance(panels, list) or not panels:
                        raise ValueError("В ordinary_sheets некорректная секция")
                    for panel in panels:
                        prices = panel.get("prices") if isinstance(panel, dict) else None
                        if (
                            not isinstance(panel.get("name"), int)
                            or not isinstance(prices, list)
                            or not prices
                            or not all(isinstance(column, int) for column in prices)
                        ):
                            raise ValueError("В ordinary_sheets некорректная панель")
        return
    if not isinstance(recipe.get("standard"), dict):
        raise ValueError("В layout-рецепте отсутствует блок standard")
    if not isinstance(recipe.get("wide"), list):
        raise ValueError("В layout-рецепте отсутствует список wide")
    if not isinstance(recipe.get("matrix"), dict):
        raise ValueError("В layout-рецепте отсутствует блок matrix")


def validate_workbook_layout_recipe(recipe: dict[str, Any]) -> None:
    """Validate a workbook layout before it is stored as executable data."""
    _validate_recipe(recipe)


def _catalog_rows_result(rows: list[_CatalogRow]) -> ParseResult:
    """Parse profile-specific rows through the common value normalizer."""
    price_width = max((len(row.prices) for row in rows), default=1)
    headers = [
        Cell(value="Наименование"),
        Cell(value="Изм."),
        Cell(value="Категория"),
        *[Cell(value=f"Расч. {index + 1}") for index in range(price_width)],
    ]

    synthetic = SheetGrid(
        name="workbook_layout",
        rows=[
            headers,
            *[
                [
                    Cell(value=row.name),
                    Cell(value=row.unit),
                    Cell(
                        value=row.category_path
                        or default_category_from_sheet_name(row.source_sheet)
                    ),
                    *[
                        Cell(value=price.value, number_format=price.number_format)
                        for price in row.prices
                    ],
                    *[Cell(value=None) for _ in range(price_width - len(row.prices))],
                ]
                for row in rows
            ],
        ],
    )
    decision = MappingDecision(
        tables=[
            TableMapping(
                sheet_name=synthetic.name,
                header_row_index=0,
                columns=[
                    ColumnMapping(index=0, role="name", header="Наименование"),
                    ColumnMapping(index=1, role="unit", header="Изм."),
                    ColumnMapping(index=2, role="category", header="Категория"),
                    *[
                        ColumnMapping(
                            index=index + 3,
                            role="price",
                            header=f"Расч. {index + 1}",
                            price_descriptor=dict(_PRICE_DESCRIPTOR),
                        )
                        for index in range(price_width)
                    ],
                ],
            )
        ],
        auto_ok=bool(rows),
    )
    result = parse_grids([synthetic], decision)
    result.coordinate_coverage_verified = True
    # The synthetic grid protects panel isolation.  Restore source tracing so
    # persisted records still point to the original worksheet and row.
    for item, source in zip(result.items, rows, strict=False):
        item.source_sheet = source.source_sheet
        item.source_row = source.source_row
        item.source_panel_index = source.source_panel_index
        for value, source_price in zip(item.values, source.prices, strict=False):
            value.source_label = source_price.label
    for issue in result.issues:
        if issue.sheet != synthetic.name or issue.row_no is None:
            continue
        source_index = issue.row_no - 2  # row 1 is the synthetic header
        if 0 <= source_index < len(rows):
            source = rows[source_index]
            issue.sheet = source.source_sheet
            issue.row_no = source.source_row
    return result


def parse_workbook_layout(grids: list[SheetGrid], recipe: dict[str, Any]) -> ParseResult:
    """Apply an explicit workbook layout through the normal parsing pipeline."""
    _validate_recipe(recipe)
    if recipe.get("profile") == "generated_matrix_v1":
        from app.parsing.matrix_unfold import parse_matrix_regions
        from app.parsing.rules import recipe_to_decision

        ordinary = parse_grids(grids, recipe_to_decision(recipe, grids))
        matrix = parse_matrix_regions(grids, recipe["matrix_regions"])
        return ParseResult(
            needs_mapping=ordinary.needs_mapping or matrix.needs_mapping,
            coordinate_coverage_verified=True,
            tables=[*ordinary.tables, *matrix.tables],
            items=[*ordinary.items, *matrix.items],
            issues=[*ordinary.issues, *matrix.issues],
            rows_accepted=ordinary.rows_accepted + matrix.rows_accepted,
            rows_skipped_error=(
                ordinary.rows_skipped_error + matrix.rows_skipped_error
            ),
        )
    if recipe.get("profile") == "price_new_v1":
        sheets = recipe.get("sheets")
        if isinstance(sheets, list) and sheets:
            from app.parsing.matrix_unfold import parse_matrix_regions
            from app.parsing.rules import recipe_to_decision

            excluded = {str(name) for name in recipe.get("ordinary_exclusions", [])}
            matrix_regions = recipe.get("matrix_regions")
            matrix_sheet_names = {
                str(match["name_pattern"])
                for region in matrix_regions
                if isinstance(region, dict)
                and isinstance((match := region.get("match")), dict)
                and isinstance(match.get("name_pattern"), str)
            } if isinstance(matrix_regions, list) else set()
            ordinary_grids = [
                grid
                for grid in grids
                if grid.name not in excluded or grid.name in matrix_sheet_names
            ]
            ordinary = parse_grids(
                ordinary_grids, recipe_to_decision(recipe, ordinary_grids)
            )
            # These source rows are expected catalogue/category references in
            # this verified profile.  They were silent in the profile parser
            # before ordinary sections switched to the full editable recipe.
            ordinary.issues = [
                issue for issue in ordinary.issues if issue.code != "NO_PRICE"
            ]
            category_by_source = {
                (row.source_sheet, row.source_row, row.source_panel_index): (
                    row.category_path
                    or default_category_from_sheet_name(row.source_sheet)
                )
                for grid in ordinary_grids
                for row in _ordinary_rows(grid)
            }
            for item in ordinary.items:
                category = category_by_source.get(
                    (item.source_sheet, item.source_row, item.source_panel_index or 0)
                )
                if category is not None:
                    item.category_path = category
            special = _catalog_rows_result(
                _price_new_special_rows(
                    grids, recipe, include_hydrant=not bool(matrix_regions)
                )
            )
            matrix = (
                parse_matrix_regions(grids, matrix_regions)
                if isinstance(matrix_regions, list)
                else ParseResult(coordinate_coverage_verified=True)
            )
            return ParseResult(
                needs_mapping=(
                    ordinary.needs_mapping
                    or special.needs_mapping
                    or matrix.needs_mapping
                ),
                coordinate_coverage_verified=True,
                tables=[*ordinary.tables, *special.tables, *matrix.tables],
                items=[*ordinary.items, *special.items, *matrix.items],
                issues=[*ordinary.issues, *special.issues, *matrix.issues],
                rows_accepted=(
                    ordinary.rows_accepted
                    + special.rows_accepted
                    + matrix.rows_accepted
                ),
                rows_skipped_error=(
                    ordinary.rows_skipped_error
                    + special.rows_skipped_error
                    + matrix.rows_skipped_error
                ),
            )
        return _catalog_rows_result(_price_new_rows(grids, recipe))

    rows: list[_CatalogRow] = []
    standard = recipe["standard"]
    panels = standard.get("panels") if isinstance(standard, dict) else None
    if isinstance(panels, list):
        for grid in grids:
            if _standard_sheet_signature(grid):
                rows.extend(_panel_rows(grid, panels))

    for wide in recipe["wide"]:
        if not isinstance(wide, dict):
            continue
        names = {str(name) for name in wide.get("sheets", [])}
        panels = wide.get("panels")
        if not isinstance(panels, list):
            continue
        for grid in grids:
            if grid.name in names:
                rows.extend(_panel_rows(grid, panels))

    matrix = recipe["matrix"]
    matrix_sheet = matrix.get("sheet") if isinstance(matrix, dict) else None
    if isinstance(matrix_sheet, str):
        for grid in grids:
            if grid.name == matrix_sheet:
                rows.extend(_matrix_rows(grid, matrix))
                break
    return _catalog_rows_result(rows)
