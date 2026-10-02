"""Column mapping model and the auto-mapping decision."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from app.parsing.grid import SheetGrid, is_empty_grid
from app.parsing.headers import normalize_header
from app.parsing.headers import (
    AUTO_CONFIDENCE_THRESHOLD,
    EXACT_CONFIDENCE,
    HeaderDetection,
    detect_header_candidates,
    detect_multiline_header_candidates,
    detect_header,
    apply_sheet_vat_statement,
    merge_price_subheader,
    profile_name_column,
    profile_price_columns,
    suggest_role,
)
from app.parsing.normalize import is_known_unit, parse_price

ROLES = {
    "name", "article", "manufacturer", "description", "attribute", "unit", "price",
    "quantity_threshold", "discount_percent", "weight", "volume", "dimensions",
    "stock", "lead_time", "category", "photo", "row_number", "ignore",
}

# This is intentionally a closed, portable set.  Attribute values are stored
# in native scalar columns; ``json``/``jsonb`` is not and never will be a
# value kind.
ATTRIBUTE_VALUE_KINDS = frozenset(
    {"text", "integer", "decimal", "boolean", "date", "datetime", "option"}
)


def valid_attribute_value_kind(value: object) -> str | None:
    """Return a safe declared scalar kind, or no override for stale recipes."""
    return value if isinstance(value, str) and value in ATTRIBUTE_VALUE_KINDS else None

# Stable semantics can be put into auto-generated recipes. A generic source
# header remains unmapped (``attribute`` -> None/text) so it becomes a
# supplier-scoped draft definition instead of an unsafe global merge.
ATTRIBUTE_ROLE_DEFAULTS: dict[str, tuple[str | None, str, str | None]] = {
    "attribute": (None, "text", None),
    "manufacturer": ("manufacturer", "text", None),
    "quantity_threshold": ("quantity_threshold", "decimal", None),
    "discount_percent": ("discount_percent", "decimal", "%"),
    "weight": ("weight", "decimal", "kg"),
    "volume": ("volume", "decimal", "m3"),
    "stock": ("stock", "text", None),
    "lead_time": ("lead_time", "text", None),
    "photo": ("photo", "text", None),
}

# These labels describe a canonical definition, not the spelling of a
# particular supplier header.  The latter is kept in supplier_attribute_keys
# and returned with an item fact.
ATTRIBUTE_DEFINITION_DISPLAY_NAMES: dict[str, str] = {
    "manufacturer": "Производитель",
    "quantity_threshold": "Порог количества",
    "discount_percent": "Скидка",
    "weight": "Вес",
    "volume": "Объём",
    "stock": "Наличие",
    "lead_time": "Срок поставки",
    "photo": "Фото",
}

_GENERIC_SHEET_NAME_RE = re.compile(
    r"^(?:лист\s*\d*|sheet\s*\d*|прайс(?:-?лист)?|price(?:\s*list)?|"
    r"общий|offers?|sa|ver|pdf|docx?|tdsheet|каталог|товары?|номенклатура)$",
    re.IGNORECASE,
)
_CATALOG_SHEET_PREFIX_RE = re.compile(r"^каталог\s*[-—:]\s*", re.IGNORECASE)


def default_category_from_sheet_name(sheet_name: str) -> str | None:
    """Use a meaningful worksheet title, but never a generic/truncated label."""

    source = " ".join(str(sheet_name).split()).strip()
    if not source or _GENERIC_SHEET_NAME_RE.fullmatch(source):
        return None
    # Excel sheet titles are capped at 31 characters. A catalog-prefixed title
    # at that exact limit is usually visibly truncated (``... кабели свя``)
    # and must not create a permanent category from an incomplete word.
    if len(source) >= 31 and _CATALOG_SHEET_PREFIX_RE.match(source):
        return None
    source = _CATALOG_SHEET_PREFIX_RE.sub("", source).strip()
    if (
        not source
        or len(source) > 160
        or _GENERIC_SHEET_NAME_RE.fullmatch(source)
        or not any(char.isalpha() for char in source)
    ):
        return None
    return source


@dataclass
class ColumnMapping:
    index: int
    role: str
    price_descriptor: dict | None = None  # {"kind": ..., "vat_mode": ...}
    header: str = ""
    confidence: float = 1.0
    # Optional canonicalization metadata for a variable characteristic.  The
    # source header and raw cell value are always retained separately; these
    # fields tell the writer how to map that fact into the attribute dictionary.
    attribute_code: str | None = None
    attribute_value_kind: str | None = None
    canonical_unit: str | None = None
    # For a price column, bind its quantity tier to a threshold column in the
    # same physical row and horizontal panel.
    quantity_threshold_column_index: int | None = None
    # Column-level quantity tier for a price column whose header declares the
    # whole column as one tier (e.g. a PDF column headed "до 50 шт."). Inclusive
    # bounds; None on a side means unbounded. Used when a row has no explicit
    # quantity_threshold cell.
    tier_min_quantity: Decimal | None = None
    tier_max_quantity: Decimal | None = None


@dataclass(frozen=True)
class InlineAttributeMapping:
    source_column_index: int
    attribute_code: str
    display_name: str
    value_kind: str
    canonical_unit: str | None
    extractor_kind: str


@dataclass
class TableMapping:
    sheet_name: str
    header_row_index: int | None
    columns: list[ColumnMapping] = field(default_factory=list)
    # Inclusive source-row boundary for a semantic fragment reusing a column
    # header from elsewhere on the sheet. None keeps the legacy header+1 start.
    start_row_index: int | None = None
    # Exclusive source-row boundary.  It lets one worksheet contribute
    # several vertically stacked tables with distinct column layouts.
    end_row_index: int | None = None
    # Ordered, explicitly model-selected columns that identify an item when
    # the primary name cell is blank on a continuation/variant row.  Keeping
    # this separate from ``name`` avoids falsely treating coalesced fields as
    # independent horizontal price panels.
    name_fallback_column_indexes: list[int] = field(default_factory=list)
    inline_attributes: list[InlineAttributeMapping] = field(default_factory=list)
    # Only a source-compiled model rule may retain ranges, lower bounds and
    # unit-qualified prices as reviewable offers. Automatic mappings keep
    # treating them as complex so they cannot silently broaden semantics.
    allow_qualified_prices: bool = False
    # Some XLSX catalogues display rounded formula results while their stored
    # numeric value carries the supplier's exact price. Most imports follow
    # the visible cell; an explicit profile may opt into the underlying value.
    use_display_price_values: bool = True
    source_section_id: str | None = None
    source_panel_index: int | None = None
    source_column_start: int | None = None
    source_column_end: int | None = None
    # A meaningful worksheet title is valid fallback context for otherwise
    # flat price tables. Persisting it in mappings/rules makes future parses
    # deterministic; generic or visibly truncated titles remain unset.
    default_category_path: str | None = None
    # Generated reusable rules locate a semantic header by its saved labels
    # on every parse instead of persisting the row number seen in the source
    # version. ``dynamic_body`` means category/product blocks continue until
    # the next independently matched template (or the end of the sheet).
    locate_header_by_signature: bool = False
    dynamic_body: bool = False

    def has_role(self, role: str) -> bool:
        return any(c.role == role for c in self.columns)


@dataclass
class MappingDecision:
    tables: list[TableMapping] = field(default_factory=list)
    non_price_sheets: list[str] = field(default_factory=list)
    auto_ok: bool = False


def detection_to_mapping(
    sheet_name: str,
    detection: HeaderDetection,
    *,
    retain_unknown_attributes: bool = False,
) -> TableMapping:
    """Turn header suggestions into a table mapping.

    Once a sheet is known to be a price table, an otherwise unknown *named*
    column is still source data worth retaining.  It becomes a supplier-scoped
    draft attribute rather than silently disappearing.  We deliberately do
    not turn a downgraded probable price column into an attribute: its role
    remains ``price`` with low confidence, so it stays out of an automatic
    mapping until a rule is corrected.
    """

    def role_for(suggestion: ColumnSuggestion) -> str:
        if suggestion.confidence >= AUTO_CONFIDENCE_THRESHOLD:
            return suggestion.role
        if (
            retain_unknown_attributes
            and suggestion.role == "ignore"
            and normalize_header(suggestion.header)
        ):
            return "attribute"
        return "ignore"

    cols = [
        ColumnMapping(
            index=s.index,
            role=role_for(s),
            price_descriptor=s.price_descriptor if s.confidence >= AUTO_CONFIDENCE_THRESHOLD else None,
            header=s.header,
            confidence=s.confidence,
            attribute_code=(
                ATTRIBUTE_ROLE_DEFAULTS.get(s.role, (None, "text", None))[0]
                if s.confidence >= AUTO_CONFIDENCE_THRESHOLD
                else None
            ),
            attribute_value_kind=(
                ATTRIBUTE_ROLE_DEFAULTS.get(s.role, (None, "text", None))[1]
                if s.confidence >= AUTO_CONFIDENCE_THRESHOLD and s.role in ATTRIBUTE_ROLE_DEFAULTS
                else None
            ),
            canonical_unit=(
                ATTRIBUTE_ROLE_DEFAULTS.get(s.role, (None, "text", None))[2]
                if s.confidence >= AUTO_CONFIDENCE_THRESHOLD
                else None
            ),
        )
        for s in detection.columns
    ]
    return TableMapping(
        sheet_name=sheet_name,
        header_row_index=detection.header_row_index,
        columns=cols,
        default_category_path=default_category_from_sheet_name(sheet_name),
    )


def detect_headerless_price_table(grid: SheetGrid) -> TableMapping | None:
    """Detect a price table on a sheet without any column header row.

    Some price lists have no column labels at all: sections open with a
    caption merged across the row, then every product row repeats one stable
    column shape — a product text block, optionally a unit cell, and a price
    cell.  When that shape dominates the sheet with strong name and price
    evidence, the sheet is a price table even though no dictionary label is
    present anywhere.  A leading row of pure dictionary labels (for example
    «Наименование | Кол-во | Сумма», whose price label is not recognized) is
    kept as the header row instead of being parsed as an item.
    """

    def real_cells(row) -> list[tuple[int, object]]:
        return [
            (index, cell)
            for index, cell in enumerate(row)
            if not cell.is_merged_fill
            and cell.value is not None
            and not (isinstance(cell.value, str) and not cell.value.strip())
        ]

    def price_evidence(cell) -> Decimal | None | str:
        """Positive amount for a real price, ``on_request`` for a request-only
        cell, ``None`` when the cell is not price evidence.  A published zero
        is not an offered price (it is commonly a cleared formula result)."""
        value = cell.display_value if cell.display_value is not None else cell.value
        parsed = parse_price(value)
        if parsed.status == "on_request":
            return "on_request"
        if parsed.status == "ok" and parsed.amount is not None and parsed.amount > 0:
            return parsed.amount
        return None

    shaped_rows: list[tuple[int, list[tuple[int, object]]]] = [
        (row_index, cells)
        for row_index, row in enumerate(grid.rows)
        if len(cells := real_cells(row)) >= 2
    ]
    if len(shaped_rows) < 2:
        return None

    shapes = Counter(tuple(index for index, _ in cells) for _, cells in shaped_rows)
    shape, shape_count = shapes.most_common(1)[0]
    if shape_count < 2 or shape_count / len(shaped_rows) < 0.6:
        return None
    if not 2 <= len(shape) <= 4:
        return None

    rows_of_shape = [
        (row_index, cells)
        for row_index, cells in shaped_rows
        if tuple(index for index, _ in cells) == shape
    ]

    def is_label_row(cells) -> bool:
        return any(
            suggest_role(cell.value).confidence >= EXACT_CONFIDENCE for _, cell in cells
        ) and not any(price_evidence(cell) is not None for _, cell in cells)

    first_row_index, first_cells = rows_of_shape[0]
    header_row_index = first_row_index if is_label_row(first_cells) else None
    data_rows = [cells for _, cells in rows_of_shape if not is_label_row(cells)]
    if len(data_rows) < 2:
        return None

    def is_product_row(cells) -> bool:
        name = cells[0][1].value
        return (
            isinstance(name, str)
            and any(char.isalpha() for char in name)
            and price_evidence(cells[0][1]) is None
            and price_evidence(cells[-1][1]) is not None
        )

    body_start = None
    body_rows = None
    for position, (row_index, cells) in enumerate(rows_of_shape):
        if not is_product_row(cells):
            continue
        suffix = rows_of_shape[position:]
        product_count = sum(1 for _, candidate in suffix if is_product_row(candidate))
        if product_count >= 2 and product_count / len(suffix) >= 0.6:
            body_start = row_index
            body_rows = [candidate for _, candidate in suffix if is_product_row(candidate)]
            break
    if body_start is None or body_rows is None:
        return None

    price_values = [price_evidence(cells[-1][1]) for cells in body_rows]
    # Two side-by-side price panels can look like one headerless table when
    # only two product rows are available. A second numeric offer in any
    # interior column is ambiguous structure, not an attribute/unit column.
    # Leave it to the section resolver instead of silently keeping only the
    # right-most panel.
    middle_price_indexes: list[int] = []
    middle_name_indexes: list[int] = []
    for middle_index in shape[1:-1]:
        middle_cells = [cells[shape.index(middle_index)][1] for cells in body_rows]
        middle_price_hits = sum(price_evidence(cell) is not None for cell in middle_cells)
        middle_name_hits = sum(
            isinstance(cell.value, str)
            and any(char.isalpha() for char in cell.value)
            and price_evidence(cell) is None
            for cell in middle_cells
        )
        if middle_price_hits / len(body_rows) >= 0.6:
            middle_price_indexes.append(middle_index)
        if middle_name_hits / len(body_rows) >= 0.6:
            middle_name_indexes.append(middle_index)
    if any(
        price_index < name_index
        for price_index in middle_price_indexes
        for name_index in middle_name_indexes
    ):
        return None
    # A contents page or a row-number column also parses as money: a run of
    # consecutive integers starting at 1-2 is pagination, not a price list.
    amounts = sorted({value for value in price_values if isinstance(value, Decimal)})
    if (
        len(amounts) >= 3
        and amounts[0] <= 2
        and all(value == value.to_integral_value() for value in amounts)
        and all(step == 1 for step in (b - a for a, b in zip(amounts, amounts[1:])))
    ):
        return None

    columns = [
        ColumnMapping(
            index=shape[0],
            role="name",
            header="",
            confidence=AUTO_CONFIDENCE_THRESHOLD,
        )
    ]
    for middle_index in shape[1:-1]:
        middle_values = [cells[shape.index(middle_index)][1].value for cells in body_rows]
        unit_share = sum(1 for value in middle_values if is_known_unit(value)) / len(body_rows)
        columns.append(
            ColumnMapping(
                index=middle_index,
                role="unit" if unit_share >= 0.7 else "ignore",
                header="",
                confidence=AUTO_CONFIDENCE_THRESHOLD,
            )
        )
    columns.append(
        ColumnMapping(
            index=shape[-1],
            role="price",
            price_descriptor={"kind": "other", "vat_mode": "unknown"},
            header="",
            confidence=AUTO_CONFIDENCE_THRESHOLD,
        )
    )
    return TableMapping(
        sheet_name=grid.name,
        header_row_index=header_row_index,
        columns=columns,
        start_row_index=body_start,
        default_category_path=default_category_from_sheet_name(grid.name),
    )


def build_auto_mapping(grids: list[SheetGrid]) -> MappingDecision:
    """Auto-mapping succeeds when at least one price-table-looking sheet has a
    name column and >=1 price column, both with confidence >= 0.7."""
    decision = MappingDecision()
    for grid in grids:
        if is_empty_grid(grid):
            continue
        detections = detect_header_candidates(grid)
        if not detections:
            detections = detect_multiline_header_candidates(grid)
        if not detections:
            headerless = detect_headerless_price_table(grid)
            if headerless is not None:
                decision.tables.append(headerless)
            else:
                decision.non_price_sheets.append(grid.name)
            continue
        mapped_any = False
        for index, detection in enumerate(detections):
            # Price profiling must not look through a later table that has a
            # different layout.  Feed it the bounded slice for this section.
            section_end = (
                detections[index + 1].header_row_index
                if index + 1 < len(detections)
                else len(grid.rows)
            )
            section = SheetGrid(name=grid.name, rows=grid.rows[:section_end])
            profile_price_columns(section, detection)
            profile_name_column(section, detection)
            merge_price_subheader(section, detection)
            apply_sheet_vat_statement(section, detection)
            known_table = detection_to_mapping(grid.name, detection)
            if not (known_table.has_role("name") and known_table.has_role("price")):
                continue
            # Only after the mandatory columns are trustworthy do we retain
            # unknown named columns as attributes.  That keeps an arbitrary
            # worksheet from becoming a collection of fake product facts.
            table = detection_to_mapping(
                grid.name,
                detection,
                retain_unknown_attributes=True,
            )
            table.end_row_index = section_end if section_end < len(grid.rows) else None
            decision.tables.append(table)
            mapped_any = True
        if not mapped_any:
            decision.non_price_sheets.append(grid.name)
    grid_order = {grid.name: index for index, grid in enumerate(grids)}
    decision.tables.sort(
        key=lambda table: (
            grid_order.get(table.sheet_name, len(grids)),
            table.header_row_index if table.header_row_index is not None else -1,
        )
    )
    decision.auto_ok = len(decision.tables) > 0
    return decision


def stored_mapping_to_tables(stored: dict, grids: list[SheetGrid]) -> MappingDecision:
    """Build table mappings from an admin-confirmed mapping json.

    A newer mapping can identify a section by ``header_row_index``; old
    mappings without it keep their previous strongest-header behaviour.  A
    sheet may still contain later independently detectable sections, which
    are added through the deterministic auto mapper instead of being parsed
    with the first section's column offsets.

    Explicit mapping roles always win.  For its confirmed price table, a
    *previously unlisted and genuinely unknown* named column is retained as a
    draft attribute.  This keeps newly introduced product characteristics
    without silently converting any recognised price-like column into text.
    """
    decision = MappingDecision()
    grids_by_name = {g.name: g for g in grids}
    auto_decision = build_auto_mapping(grids)
    auto_by_section = {
        (table.sheet_name, table.header_row_index): table
        for table in auto_decision.tables
    }
    stored_by_sheet: dict[str, list[dict]] = {}
    for table in stored.get("tables", []):
        sheet_name = table.get("sheet_name")
        if isinstance(sheet_name, str) and sheet_name:
            stored_by_sheet.setdefault(sheet_name, []).append(table)

    def mark_non_price(sheet_name: str) -> None:
        if sheet_name not in decision.non_price_sheets:
            decision.non_price_sheets.append(sheet_name)

    for sheet_name, sheet_tables in stored_by_sheet.items():
        grid = grids_by_name.get(sheet_name)
        if grid is None:
            mark_non_price(sheet_name)
            continue
        if is_empty_grid(grid):
            continue

        detections = detect_header_candidates(grid)
        detection_by_row = {
            detection.header_row_index: detection
            for detection in detections
            if detection.header_row_index is not None
        }
        fallback_header = detect_header(grid).header_row_index
        manually_mapped_sections: set[tuple[str, int | None]] = set()
        added_manual_table = False

        def source_header(header_row: int | None, index: int, fallback: object = "") -> str:
            if (
                header_row is not None
                and 0 <= header_row < len(grid.rows)
                and 0 <= index < len(grid.rows[header_row])
            ):
                value = grid.rows[header_row][index].value
                if value is not None and str(value).strip():
                    return str(value)
            return str(fallback or "")

        def section_end(header_row: int | None) -> int | None:
            if header_row is None:
                return None
            for detection in detections:
                candidate_row = detection.header_row_index
                if candidate_row is not None and candidate_row > header_row:
                    return candidate_row
            return None

        for position, stored_table in enumerate(sheet_tables):
            requested_header = stored_table.get("header_row_index")
            if isinstance(requested_header, int) and not isinstance(requested_header, bool):
                header_row = requested_header
            elif len(sheet_tables) > 1 and position < len(detections):
                # Transitional convenience for a payload produced before the
                # optional section id was introduced: its table order remains
                # meaningful, while a single legacy mapping keeps old logic.
                header_row = detections[position].header_row_index
            else:
                header_row = fallback_header

            if header_row is not None and not (0 <= header_row < len(grid.rows)):
                continue

            detection = detection_by_row.get(header_row)
            columns: list[ColumnMapping] = []
            explicit_indices: set[int] = set()
            for column in stored_table.get("columns", []):
                if column.get("role", "ignore") not in ROLES:
                    continue
                try:
                    index = int(column["index"])
                except (KeyError, TypeError, ValueError):
                    continue
                explicit_indices.add(index)
                columns.append(
                    ColumnMapping(
                        index=index,
                        role=column.get("role", "ignore"),
                        price_descriptor=column.get("price_descriptor"),
                        header=source_header(header_row, index, column.get("header", "")),
                        attribute_code=column.get("attribute_code"),
                        attribute_value_kind=valid_attribute_value_kind(
                            column.get("attribute_value_kind")
                        ),
                        canonical_unit=column.get("canonical_unit"),
                        quantity_threshold_column_index=(
                            int(column["quantity_threshold_column_index"])
                            if isinstance(
                                column.get("quantity_threshold_column_index"), int
                            )
                            and not isinstance(
                                column.get("quantity_threshold_column_index"), bool
                            )
                            else None
                        ),
                    )
                )

            table = TableMapping(
                sheet_name=sheet_name,
                header_row_index=header_row,
                columns=columns,
                end_row_index=section_end(header_row),
                default_category_path=(
                    str(stored_table["default_category_path"]).strip()
                    if stored_table.get("default_category_path")
                    else default_category_from_sheet_name(sheet_name)
                ),
            )
            if not (table.has_role("name") and table.has_role("price")):
                continue

            # The explicit mapping proved the table boundaries.  Fill only
            # columns absent from it from the current deterministic mapping:
            # this recovers newly recognised description/dimensions/prices as
            # well as draft attributes.  An explicit ``ignore`` remains
            # ignored, so a user can still deliberately suppress a column.
            auto_section = auto_by_section.get((sheet_name, header_row))
            if auto_section is not None:
                for suggested_column in auto_section.columns:
                    if (
                        suggested_column.index in explicit_indices
                        or suggested_column.role == "ignore"
                    ):
                        continue
                    table.columns.append(
                        ColumnMapping(
                            index=suggested_column.index,
                            role=suggested_column.role,
                            price_descriptor=(
                                dict(suggested_column.price_descriptor)
                                if suggested_column.price_descriptor
                                else None
                            ),
                            header=suggested_column.header,
                            confidence=suggested_column.confidence,
                            attribute_code=suggested_column.attribute_code,
                            attribute_value_kind=suggested_column.attribute_value_kind,
                            canonical_unit=suggested_column.canonical_unit,
                        )
                    )
            elif detection is not None:
                # A fully manual table may use custom mandatory headers which
                # the auto mapper cannot recognise. We can still retain only
                # truly unknown named columns; known/price-like cells stay
                # untouched until a user maps them explicitly.
                for suggestion in detection.columns:
                    if (
                        suggestion.index in explicit_indices
                        or suggestion.role != "ignore"
                        or not normalize_header(suggestion.header)
                    ):
                        continue
                    table.columns.append(
                        ColumnMapping(
                            index=suggestion.index,
                            role="attribute",
                            header=suggestion.header,
                            confidence=suggestion.confidence,
                        )
                    )

            decision.tables.append(table)
            manually_mapped_sections.add((sheet_name, header_row))
            added_manual_table = True

        # A manual mapping for the first section must not make a later table
        # inherit its columns.  Safely add any separately detectable sections
        # that the user did not map; each has its own name+price confirmation.
        if added_manual_table:
            for key, auto_table in auto_by_section.items():
                if key[0] == sheet_name and key not in manually_mapped_sections:
                    decision.tables.append(auto_table)
        else:
            mark_non_price(sheet_name)

    grid_order = {grid.name: index for index, grid in enumerate(grids)}
    decision.tables.sort(
        key=lambda table: (
            grid_order.get(table.sheet_name, len(grids)),
            table.header_row_index if table.header_row_index is not None else -1,
        )
    )
    decision.auto_ok = len(decision.tables) > 0
    return decision
