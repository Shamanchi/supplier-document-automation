"""Parsing pipeline orchestration.

parse_workbook() is pure (bytes in, ParseResult out) so it is unit-testable
against corpus files without DB/MinIO; persistence and status transitions
live in app.tasks.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from app.parsing.grid import SheetGrid, is_empty_grid
from app.parsing.headers import AUTO_CONFIDENCE_THRESHOLD, normalize_header, suggest_role
from app.parsing.loaders import detect_loader, load_grids_for
from app.parsing.mapping import (
    ATTRIBUTE_ROLE_DEFAULTS,
    MappingDecision,
    TableMapping,
    build_auto_mapping,
    stored_mapping_to_tables,
)
from app.parsing.inline_attributes import extract_inline_values

__all__ = [
    "ParseResult",
    "ParsedAttribute",
    "ParsedItem",
    "ParsedValue",
    "ParseIssue",
    "parse_workbook",
    "parse_grids",
]
from app.parsing.normalize import (
    currency_code,
    currency_from_number_format,
    is_absent_numeric_marker,
    infer_article_from_name,
    normalize_article,
    normalize_name,
    normalize_unit,
    parse_price,
)
from app.parsing.quantity_tiers import quantity_bounds
from app.parsing.rows import ROW_CATEGORY, ROW_EMPTY, ROW_ITEM, ROW_NOTE, classify_row


ATTRIBUTE_ROLES = {
    "attribute",
    "manufacturer",
    "quantity_threshold",
    "discount_percent",
    "weight",
    "volume",
    "stock",
    "lead_time",
    "photo",
}

# Supplier taxonomies may legitimately have several nested levels, but an
# unbounded category stack is always parser leakage: flat section captions or
# unpriced product rows have been mistaken for successive parents. Keep the
# most local context when source formatting claims an implausibly deep tree.
MAX_CATEGORY_DEPTH = 6


@dataclass
class _CategoryFrame:
    caption: str
    indent: int | None = None
    outline_level: int | None = None
    # 1-based grid row the caption was read from; None for synthetic context
    # seeded from a pre-header or a default category path.
    row_no: int | None = None


@dataclass
class ParsedValue:
    kind: str
    amount: Decimal | None
    vat_mode: str
    is_on_request: bool
    source_label: str | None
    qualifier: str = "exact"
    amount_from: Decimal | None = None
    amount_to: Decimal | None = None
    currency: str | None = None
    currency_source: str = "unresolved"
    min_quantity: Decimal | None = None
    max_quantity: Decimal | None = None
    basis_quantity: Decimal | None = None
    basis_unit: str | None = None
    raw_value: str | None = None
    source_column_index: int | None = None
    # Ставка, вычитанная из самой ячейки или её колонки. None означает «в
    # источнике ставки нет» — тогда применяется ставка импорта целиком, а не
    # подставляется значение по умолчанию.
    vat_rate_percent: Decimal | None = None


@dataclass
class ParsedAttribute:
    """One non-empty source cell that represents a variable characteristic.

    The list form is deliberate: dictionaries silently collapse repeated
    headers, while a price list can legitimately contain several values for a
    characteristic.  Type conversion happens during persistence, so a parser
    rule never has to turn a malformed source string into a made-up value.
    """

    source_header: str
    raw_value: str
    role: str
    attribute_code: str | None
    value_kind: str
    canonical_unit: str | None
    ordinal: int
    source_column_index: int


@dataclass
class ParsedItem:
    source_name: str
    normalized_name: str
    source_article: str | None
    normalized_article: str | None
    unit_code: str | None
    category_path: str | None
    dimensions: str | None
    description: str | None
    attribute_values: list[ParsedAttribute]
    source_sheet: str
    source_row: int  # 1-based, as in the spreadsheet
    source_section_id: str | None = None
    source_panel_index: int | None = None
    source_column_start: int | None = None
    source_column_end: int | None = None
    values: list[ParsedValue] = field(default_factory=list)
    # Raw source evidence, as [column_index, cell_text] pairs, captured from
    # the grid the parser actually read. Lets a reviewer verify a persisted
    # position against the original document without re-opening the file.
    source_header_cells: list[list[object]] | None = None
    source_data_cells: list[list[object]] | None = None
    source_category_cells: list[list[list[object]]] | None = None
    # Excel rows whose quantity-tier prices the parser glued into this
    # parent position (merged-fill name ladders). Review gates count
    # these source coordinates as covered by this item.
    glued_source_rows: list[int] = field(default_factory=list)


@dataclass
class ParseIssue:
    severity: str  # "error" | "warning"
    code: str
    sheet: str | None = None
    row_no: int | None = None
    column_ref: str | None = None
    source_value: str | None = None
    message: str = ""


@dataclass
class ParseResult:
    needs_mapping: bool = False
    # Deterministic workbook-layout profiles reconcile source coordinates with
    # panel/matrix semantics themselves; the generic review heuristic must not
    # reinterpret their axes as missed product rows.
    coordinate_coverage_verified: bool = False
    tables: list[TableMapping] = field(default_factory=list)
    items: list[ParsedItem] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)
    rows_accepted: int = 0
    rows_skipped_error: int = 0

    @property
    def errors_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warnings_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    @property
    def quality_score(self) -> float | None:
        denom = self.rows_accepted + self.rows_skipped_error
        if denom == 0:
            return None
        return self.rows_accepted / denom


def _col_ref(index: int) -> str:
    """0-based column index -> spreadsheet letter (0 -> A)."""
    ref = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        ref = chr(ord("A") + rem) + ref
    return ref


def _first_value(row, index: int):
    if 0 <= index < len(row):
        cell = row[index]
        if not cell.is_merged_fill:
            return cell.value
        return cell.value  # merged master value is still the cell's value
    return None


def _first_price_value(row, index: int):
    if 0 <= index < len(row):
        cell = row[index]
        # Loaders repeat a merged master's display value into covered cells.
        # In body rows a product name can span across an otherwise price
        # column; treating that visual fill as money creates false blocking
        # PRICE_PARSE_ERROR records.  The real master cell is parsed through
        # its own mapped column, while merged continuations are structural.
        if cell.is_merged_fill:
            return None
        # The stored value is the price of record. A cell holding 1058.5661
        # under a zero-decimal format displays as 1059, and persisting the
        # display silently rounds every such price to whole roubles.
        return cell.value if cell.value is not None else cell.display_value
    return None


def _spans_below(grid: SheetGrid, row_index: int, column: int) -> bool:
    """Ячейка объединена вниз: значение относится ко всему блоку строк.

    Дилерская цена в многоярусном блоке объединена на все ярусы — она не
    зависит от размера партии, в отличие от розничной. Приписать ей порог
    первого яруса значило бы выдумать условие, которого в прайсе нет.
    """
    below = row_index + 1
    if below >= len(grid.rows) or column >= len(grid.rows[below]):
        return False
    return grid.rows[below][column].is_merged_fill


def _raw_row_cells(row) -> list[list[object]]:
    """Snapshot a grid row as ``[column_index, cell_text]`` pairs.

    Used for the raw source-evidence shown next to a persisted position.
    Merged-fill copies are skipped (the master cell already carries the
    value) and the visible ``display_value`` wins over the underlying number,
    matching what a reviewer sees in the original document.
    """
    cells: list[list[object]] = []
    for index, cell in enumerate(row):
        if cell.is_merged_fill:
            continue
        value = cell.display_value if cell.display_value is not None else cell.value
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        cells.append([index, text])
    return cells


def _non_price_sheet_context(grid: SheetGrid) -> str:
    """Compact evidence shown in the report for a skipped non-empty sheet."""
    values: list[str] = []
    for row in grid.rows:
        for cell in row:
            if cell.value is None or cell.is_merged_fill:
                continue
            text = " ".join(str(cell.value).split())
            if text:
                values.append(text)
            if len(values) == 3:
                break
        if len(values) == 3:
            break

    non_empty_cells = sum(
        1
        for row in grid.rows
        for cell in row
        if cell.value is not None
        and (not isinstance(cell.value, str) or bool(cell.value.strip()))
    )
    def quantity(value: int, one: str, few: str, many: str) -> str:
        remainder = value % 100
        if 11 <= remainder <= 14:
            suffix = many
        else:
            suffix = {1: one, 2: few, 3: few, 4: few}.get(value % 10, many)
        return f"{value} {suffix}"

    summary = (
        f"Размер: {quantity(len(grid.rows), 'строка', 'строки', 'строк')} × "
        f"{quantity(grid.n_cols, 'колонка', 'колонки', 'колонок')}; "
        f"заполнено ячеек: {non_empty_cells}."
    )
    if values:
        return f"{summary} Пример: " + " · ".join(f"«{value}»" for value in values)
    return summary


def _preheader_category_context(grid: SheetGrid, header_row_index: int | None) -> list[str]:
    """Pick a section caption immediately above a table header.

    Price lists often put the category in a standalone, merged title row and
    leave a blank row before the actual table header.  The regular row parser
    starts below that header, so without this hand-off the first section loses
    its category.
    """
    if header_row_index is None:
        return []

    for row in reversed(grid.rows[:header_row_index]):
        values = [
            str(cell.value).strip()
            for cell in row
            if not cell.is_merged_fill
            and cell.value is not None
            and str(cell.value).strip()
        ]
        if not values:
            continue
        if len(values) != 1:
            return []

        caption = " ".join(values[0].split())
        normalized_caption = normalize_header(caption)
        letters = "".join(char for char in caption if char.isalpha())
        is_section_caption = (
            bool(letters)
            and "прайс" not in caption.lower()
            and ":" not in caption
            and not normalized_caption.startswith(("цен", "стоимост"))
            and suggest_role(caption).role != "price"
        )
        return [caption] if is_section_caption else []
    return []


def parse_workbook(data: bytes, filename: str, stored_mapping: dict | None = None) -> ParseResult:
    loader = detect_loader(data, filename)
    grids = load_grids_for(loader, data, filename)
    if stored_mapping:
        decision = stored_mapping_to_tables(stored_mapping, grids)
    else:
        decision = build_auto_mapping(grids)
    return parse_grids(grids, decision)


def parse_grids(grids: list[SheetGrid], decision: MappingDecision) -> ParseResult:
    """Parse pre-loaded grids with a ready mapping decision. Grids may come
    from any loader (xlsx/xls/pdf_text/docx/doc/txt); the decision may come from
    auto-mapping, a stored user mapping or a parse-rule recipe."""
    result = ParseResult(tables=decision.tables)
    grids_by_name = {g.name: g for g in grids}
    for sheet_name in decision.non_price_sheets:
        grid = grids_by_name.get(sheet_name)
        if grid is not None and is_empty_grid(grid):
            continue
        result.issues.append(
            ParseIssue(
                severity="warning",
                code="NON_PRICE_SHEET",
                sheet=sheet_name,
                source_value=_non_price_sheet_context(grid) if grid is not None else None,
                message="Лист пропущен: не найдена шапка одновременно с «Наименованием» и «Ценой»",
            )
        )

    if not decision.auto_ok:
        result.needs_mapping = True
        return result

    format_currencies = Counter(
        detected
        for grid in grids
        for row in grid.rows
        for cell in row
        if not cell.is_merged_fill
        if (detected := currency_from_number_format(cell.number_format)) is not None
    )
    dominant_currency = format_currencies.most_common(1)
    document_currency = None
    if dominant_currency:
        currency, count = dominant_currency[0]
        total = sum(format_currencies.values())
        if count >= 10 and count / total >= 0.95:
            document_currency = currency
    for table in decision.tables:
        grid = grids_by_name[table.sheet_name]
        _parse_table(grid, table, result, document_currency=document_currency)
    return result


def _horizontal_panel_mappings(table: TableMapping) -> list[TableMapping] | None:
    """Split a genuinely parallel row layout into independent table panels.

    A number of supplier workbooks put two catalogues side by side, for
    example ``Name | Unit | Price | Name | Unit | Price``.  Treating that as
    one table makes the price in the right panel a second price for the left
    product.  It is a data-integrity issue rather than a cosmetic layout
    detail.

    The split is deliberately conservative.  It activates only when there
    are at least two ``name`` columns and *every* name-delimited panel has its
    own price column.  A shape with several names but no one-to-one price
    association is ambiguous and is left for the caller to reject safely.
    ``None`` means the ordinary single-table parser should be used; an empty
    list means an ambiguous horizontal layout was found.
    """

    name_indices = sorted({column.index for column in table.columns if column.role == "name"})
    if len(name_indices) < 2:
        return None

    price_columns = [column for column in table.columns if column.role == "price"]
    if len(price_columns) < 2 or any(column.index < name_indices[0] for column in price_columns):
        return []

    columns_by_index = {column.index: column for column in table.columns}
    panel_starts = [min(columns_by_index)]
    for panel_index, name_index in enumerate(name_indices[1:], start=1):
        # A local row-number/category column immediately before a repeated
        # name header belongs to that panel. PDF table extraction frequently
        # stores a caption spanning the panel ("2. Перезарядка") in precisely
        # this leading cell. Splitting at the name column would instead attach
        # the caption to the left table and duplicate its row number on the
        # right table.
        start = name_index
        lower_bound = name_indices[panel_index - 1] + 1
        while start > lower_bound:
            prefix = columns_by_index.get(start - 1)
            if prefix is None or prefix.role not in {"category", "row_number", "ignore"}:
                break
            start -= 1
        panel_starts.append(start)

    panel_columns: list[list] = []
    for panel_index, start in enumerate(panel_starts):
        end = (
            panel_starts[panel_index + 1]
            if panel_index + 1 < len(panel_starts)
            else float("inf")
        )
        columns = [column for column in table.columns if start <= column.index < end]
        if not any(column.role == "name" for column in columns):
            return []
        if not any(column.role == "price" for column in columns):
            return []
        panel_columns.append(columns)

    # A column before the first product name is ordinarily a row number or a
    # shared category. Preserve it for the left panel; duplicate only an
    # explicit category role for the right panels. Row numbers and unknown
    # columns are panel-local because copying them can turn the left panel's
    # caption into context for every neighbouring product.
    prefix_columns = [column for column in table.columns if column.index < name_indices[0]]
    shared_prefix = [column for column in prefix_columns if column.role == "category"]
    shared_prefix_indices = {column.index for column in shared_prefix}

    panels: list[TableMapping] = []
    for panel_index, columns in enumerate(panel_columns):
        panels.append(
            TableMapping(
                sheet_name=table.sheet_name,
                header_row_index=table.header_row_index,
                columns=(
                    columns
                    if panel_index == 0
                    else shared_prefix + [
                        column
                        for column in columns
                        if column.index not in shared_prefix_indices
                    ]
                ),
                start_row_index=table.start_row_index,
                end_row_index=table.end_row_index,
                name_fallback_column_indexes=[
                    index
                    for index in table.name_fallback_column_indexes
                    if any(column.index == index for column in columns)
                ],
                inline_attributes=list(table.inline_attributes),
                allow_qualified_prices=table.allow_qualified_prices,
                use_display_price_values=table.use_display_price_values,
                source_section_id=table.source_section_id,
                source_panel_index=panel_index,
                source_column_start=int(panel_starts[panel_index]),
                source_column_end=(
                    int(panel_starts[panel_index + 1])
                    if panel_index + 1 < len(panel_starts)
                    else max((column.index for column in columns), default=name_indices[panel_index]) + 1
                ),
            )
        )
    return panels


def _parse_table(
    grid: SheetGrid,
    table: TableMapping,
    result: ParseResult,
    *,
    document_currency: str | None = None,
) -> None:
    horizontal_panels = _horizontal_panel_mappings(table)
    if horizontal_panels is not None:
        if not horizontal_panels:
            # Do not silently attach a neighbouring panel's price to the
            # first name.  Marking this parse as needing a mapping makes a
            # reparse retain its last good snapshot and gives a new upload an
            # explicit, reviewable state instead of corrupted associations.
            result.needs_mapping = True
            result.issues.append(
                ParseIssue(
                    severity="error",
                    code="AMBIGUOUS_HORIZONTAL_PANELS",
                    sheet=grid.name,
                    row_no=(table.header_row_index + 1)
                    if table.header_row_index is not None
                    else None,
                    message=(
                        "В строке шапки найдено несколько колонок «Наименование», "
                        "но их нельзя однозначно связать с отдельными ценовыми блоками"
                    ),
                )
            )
            return
        for panel in horizontal_panels:
            _parse_table(
                grid,
                panel,
                result,
                document_currency=document_currency,
            )
        return

    start = (
        table.start_row_index
        if table.start_row_index is not None
        else (table.header_row_index + 1 if table.header_row_index is not None else 0)
    )
    preheader_context = (
        []
        if table.start_row_index is not None
        and table.header_row_index is not None
        and table.start_row_index <= table.header_row_index
        else _preheader_category_context(grid, table.header_row_index)
    )
    fallback_context = (
        [
            segment.strip()
            for segment in table.default_category_path.split(" / ")
            if segment.strip()
        ]
        if table.default_category_path
        else []
    )
    category_stack: list[_CategoryFrame] = [
        _CategoryFrame(caption=caption)
        for caption in (preheader_context or fallback_context)
    ]
    items_since_category = False
    note_after_pending_category = False
    note_after_completed_items = False
    clear_before_next_item = False

    def apply_category_context(rc, row_no: int) -> None:
        nonlocal items_since_category, note_after_pending_category
        nonlocal note_after_completed_items, clear_before_next_item
        category_text = rc.category_text or ""
        category_indent = rc.category_indent
        outline_level = rc.category_outline_level
        has_indented_context = category_indent is not None and category_indent > 0
        if has_indented_context:
            # Leading whitespace is explicit hierarchy evidence in many
            # exported catalogues. Replace the same/deeper level instead
            # of allowing every later section to grow the path forever.
            parent_count = 0
            for index, frame in enumerate(category_stack):
                if frame.indent is None or frame.indent < category_indent:
                    parent_count = index + 1
            category_stack[parent_count:] = [
                _CategoryFrame(
                    caption=category_text,
                    indent=category_indent,
                    outline_level=outline_level,
                    row_no=row_no,
                )
            ]
        elif outline_level is not None:
            # Flat exports frequently encode hierarchy only in a leading
            # outline number. A new 3.4 heading is a sibling of 3.3 and must
            # also discard unnumbered subheadings accumulated below 3.3.
            replace_at = next(
                (
                    index
                    for index, frame in enumerate(category_stack)
                    if frame.outline_level is not None
                    and frame.outline_level >= outline_level
                ),
                None,
            )
            if replace_at is not None:
                del category_stack[replace_at:]
            elif not any(frame.outline_level is not None for frame in category_stack):
                category_stack.clear()
            category_stack.append(
                _CategoryFrame(
                    caption=category_text,
                    indent=category_indent,
                    outline_level=outline_level,
                    row_no=row_no,
                )
            )
        # Without indentation or an outline marker there is no reliable
        # parent/child evidence. Consecutive flat captions are siblings.
        elif not category_stack:
            category_stack.append(
                _CategoryFrame(caption=category_text, indent=category_indent, row_no=row_no)
            )
        elif (
            category_stack[-1].row_no is None
            and normalize_header(category_text).startswith(
                normalize_header(category_stack[-1].caption) + " "
            )
        ):
            # A generated fallback is a parent only when the local caption is
            # visibly its refinement. Unrelated flat captions keep the normal
            # sibling/replacement semantics below.
            category_stack.append(
                _CategoryFrame(caption=category_text, indent=category_indent, row_no=row_no)
            )
        elif category_stack[-1].outline_level is not None:
            category_stack.append(
                _CategoryFrame(caption=category_text, indent=category_indent, row_no=row_no)
            )
        else:
            category_stack[-1] = _CategoryFrame(
                caption=category_text, indent=category_indent, row_no=row_no
            )
        if len(category_stack) > MAX_CATEGORY_DEPTH:
            category_stack[:] = category_stack[-MAX_CATEGORY_DEPTH:]
        items_since_category = False
        note_after_pending_category = False
        note_after_completed_items = False
        clear_before_next_item = False

    name_cols = [c for c in table.columns if c.role == "name"]
    article_cols = [c for c in table.columns if c.role == "article"]
    unit_cols = [c for c in table.columns if c.role == "unit"]
    price_cols = [c for c in table.columns if c.role == "price"]
    category_cols = [c for c in table.columns if c.role == "category"]
    dimensions_cols = [c for c in table.columns if c.role == "dimensions"]
    description_cols = [c for c in table.columns if c.role == "description"]
    attribute_cols = [c for c in table.columns if c.role in ATTRIBUTE_ROLES]
    threshold_cols = [c for c in table.columns if c.role == "quantity_threshold"]

    header_row = grid.rows[table.header_row_index] if table.header_row_index is not None else None
    section_currency_codes: set[str] = set()
    if table.header_row_index is not None:
        for context_row in grid.rows[max(0, table.header_row_index - 3) : table.header_row_index + 1]:
            for cell in context_row:
                if cell.is_merged_fill:
                    continue
                detected = currency_code(cell.value)
                if detected:
                    section_currency_codes.add(detected)
    detected_sheet_currency = currency_code(grid.name)
    if detected_sheet_currency:
        section_currency_codes.add(detected_sheet_currency)
    section_currency = (
        next(iter(section_currency_codes)) if len(section_currency_codes) == 1 else None
    )
    # XLS/XLSX loaders repeat a merged cell's master value in the covered
    # cells and mark those copies as ``is_merged_fill``.  Suppliers sometimes
    # stop merging on a later item row and put the product name into one of
    # those covered columns.  The column is not a second semantic field, but
    # it is a safe continuation of the mapped name when the primary name cell
    # is empty.
    name_continuation_cols = []
    if header_row is not None:
        name_continuation_cols = [
            column
            for column in table.columns
            if column.role == "ignore"
            and column.index < len(header_row)
            and header_row[column.index].is_merged_fill
        ]

    def attribute_key(column) -> str:
        header = column.header
        if not header and header_row is not None:
            value = _first_value(header_row, column.index)
            header = "" if value is None else str(value)
        return " ".join(str(header).split()) or f"Колонка {column.index + 1}"

    def is_repeated_header(row) -> bool:
        # Long price lists often repeat the header row before each section;
        # such rows must be skipped, not reported as price parse errors.
        if header_row is None:
            return False
        matches = 0
        for col in table.columns:
            if col.role == "ignore":
                continue
            hv = header_row[col.index].value if col.index < len(header_row) else None
            rv = row[col.index].value if col.index < len(row) else None
            if hv is None or rv is None:
                continue
            if str(hv).strip() == str(rv).strip():
                matches += 1
        if matches >= 2:
            return True

        # Multi-level headers often repeat a merged name label and put the
        # concrete price/unit labels on the next row.  The text is not
        # identical to the first header row, but it is still entirely made of
        # recognised labels and must never enter category context.
        semantic_labels = 0
        data_cells = 0
        for col in table.columns:
            if col.index >= len(row):
                continue
            cell = row[col.index]
            value = cell.value
            if value is None or not str(value).strip():
                continue
            suggested = suggest_role(value)
            if (
                suggested.confidence >= AUTO_CONFIDENCE_THRESHOLD
                and (col.role == suggested.role or col.role == "ignore")
            ):
                semantic_labels += 1
            elif col.role != "ignore" and not cell.is_merged_fill:
                data_cells += 1
        return semantic_labels >= 2 and data_cells == 0

    end = min(table.end_row_index or len(grid.rows), len(grid.rows))
    # Headerless tables may have supplier metadata and section captions above
    # the first proven product row. Preserve the old category-state semantics
    # for those rows, but never parse them as products or price errors.
    if table.header_row_index is None and start > 0:
        for context_index, context_row in enumerate(grid.rows[:start]):
            context_class = classify_row(context_row, table)
            if context_class.kind == ROW_CATEGORY:
                apply_category_context(context_class, context_index + 1)

    for r in range(start, end):
        row = grid.rows[r]
        row_no = r + 1
        if is_repeated_header(row):
            continue
        rc = classify_row(row, table)

        if rc.kind == ROW_NOTE:
            if category_stack and items_since_category:
                note_after_completed_items = True
                note_after_pending_category = False
            elif category_stack:
                note_after_pending_category = True
            continue

        if rc.kind == ROW_EMPTY:
            if note_after_pending_category or note_after_completed_items:
                clear_before_next_item = True
                note_after_pending_category = False
                note_after_completed_items = False
            continue

        if rc.kind == ROW_CATEGORY:
            apply_category_context(rc, row_no)
            continue

        # --- item row ---
        if clear_before_next_item:
            category_stack.clear()
        clear_before_next_item = False
        note_after_pending_category = False
        note_after_completed_items = False
        items_since_category = bool(category_stack)
        name_val = None
        for col in name_cols:
            v = _first_value(row, col.index)
            if v is not None and str(v).strip():
                name_val = str(v).strip()
                break
        if name_val is None:
            continuation_candidates = [
                (str(value).strip(), column.index)
                for column in name_continuation_cols
                if (value := _first_value(row, column.index)) is not None
                and str(value).strip()
                and not row[column.index].is_merged_fill
            ]
            unique_continuations = {value for value, _index in continuation_candidates}
            if len(unique_continuations) == 1:
                name_val, fallback_index = continuation_candidates[0]
                result.issues.append(
                    ParseIssue(
                        severity="warning",
                        code="NAME_FROM_MERGED_CONTINUATION",
                        sheet=grid.name,
                        row_no=row_no,
                        column_ref=_col_ref(fallback_index),
                        source_value=name_val,
                        message=(
                            "Наименование прочитано из продолжения объединённой "
                            "колонки заголовка"
                        ),
                    )
                )
        if name_val is None:
            fallback_candidates: list[tuple[str, int]] = []
            for index in table.name_fallback_column_indexes:
                value = _first_value(row, index)
                text = str(value).strip() if value is not None else ""
                if text and len(text) <= 512 and "\n" not in text:
                    fallback_candidates.append((text, index))
            row_has_price = any(
                parse_price(
                    _first_price_value(row, col.index),
                    allow_qualified=table.allow_qualified_prices,
                ).status
                in {"ok", "on_request"}
                for col in price_cols
            )
            if row_has_price and fallback_candidates:
                name_val, fallback_index = fallback_candidates[0]
                result.issues.append(
                    ParseIssue(
                        severity="warning",
                        code="NAME_FROM_EXPLICIT_FALLBACK",
                        sheet=grid.name,
                        row_no=row_no,
                        column_ref=_col_ref(fallback_index),
                        source_value=name_val,
                        message=(
                            "Наименование взято из явно подтверждённой "
                            "резервной колонки той же строки"
                        ),
                    )
                )
        if name_val is None:
            # Some catalogues reserve a name column but put a product variant
            # in a concise description cell on continuation rows.  Retaining
            # that row is safer than dropping a valid set of prices, provided
            # there is exactly one short description candidate and a real
            # price in the same row.  The warning keeps this inference fully
            # auditable; long prose is never promoted to a product name.
            description_candidates: list[tuple[str, int]] = []
            for col in description_cols:
                v = _first_value(row, col.index)
                text = str(v).strip() if v is not None else ""
                if text and len(text) <= 240 and "\n" not in text:
                    description_candidates.append((text, col.index))
            row_has_price = any(
                parse_price(
                    _first_price_value(row, col.index),
                    allow_qualified=table.allow_qualified_prices,
                ).status
                in {"ok", "on_request"}
                for col in price_cols
            )
            unique_candidates = {candidate for candidate, _index in description_candidates}
            if row_has_price and len(unique_candidates) == 1:
                name_val, fallback_index = description_candidates[0]
                result.issues.append(
                    ParseIssue(
                        severity="warning",
                        code="NAME_FROM_DESCRIPTION",
                        sheet=grid.name,
                        row_no=row_no,
                        column_ref=_col_ref(fallback_index),
                        source_value=name_val,
                        message=(
                            "Наименование отсутствовало; использована краткая "
                            "характеристика из той же строки"
                        ),
                    )
                )
            else:
                parsed_price_cells = [
                    parse_price(
                        _first_price_value(row, col.index),
                        allow_qualified=table.allow_qualified_prices,
                    )
                    for col in price_cols
                ]
                present_prices = [
                    parsed
                    for parsed in parsed_price_cells
                    if parsed.status != "empty"
                ]
                if not present_prices:
                    # Structural separator/variant rows may populate a unit,
                    # dimensions or another product column while every mapped
                    # price cell is empty.  They are not malformed priced
                    # items and must not poison the rule quality gate with a
                    # false NO_NAME error.
                    result.issues.append(
                        ParseIssue(
                            severity="warning",
                            code="NO_PRICE",
                            sheet=grid.name,
                            row_no=row_no,
                            message="Строка без наименования и цены пропущена",
                        )
                    )
                    continue
                non_price_identity = any(
                    _first_value(row, column.index) not in {None, ""}
                    for column in [
                        *article_cols,
                        *description_cols,
                        *attribute_cols,
                        *unit_cols,
                    ]
                )
                zero_placeholder = (
                    bool(present_prices)
                    and not non_price_identity
                    and all(
                        parsed.status == "ok" and parsed.amount == Decimal("0.00")
                        for parsed in present_prices
                    )
                )
                if zero_placeholder:
                    result.issues.append(
                        ParseIssue(
                            severity="warning",
                            code="ORPHAN_ZERO_PRICE_PLACEHOLDER",
                            sheet=grid.name,
                            row_no=row_no,
                            message=(
                                "Строка без идентификатора содержит только "
                                "нулевой заполнитель цены и пропущена"
                            ),
                        )
                    )
                    continue
                result.issues.append(
                    ParseIssue(
                        severity="error",
                        code="NO_NAME",
                        sheet=grid.name,
                        row_no=row_no,
                        message="Строка с ценой, но без наименования — пропущена",
                    )
                )
                result.rows_skipped_error += 1
                continue

        article_val = None
        for col in article_cols:
            v = _first_value(row, col.index)
            if v is not None and str(v).strip():
                article_val = str(v).strip()
                if isinstance(v, float) and v == int(v):
                    article_val = str(int(v))
                break
        if article_val is None:
            article_val = infer_article_from_name(name_val)

        unit_raw = None
        for col in unit_cols:
            v = _first_value(row, col.index)
            if v is not None and str(v).strip():
                unit_raw = v
                break

        category_path: str | None = None
        for col in category_cols:
            v = _first_value(row, col.index)
            if v is not None and str(v).strip():
                category_path = str(v).strip()
                break
        if category_path is None and category_stack:
            category_path = " / ".join(frame.caption for frame in category_stack)

        dimensions = None
        for col in dimensions_cols:
            v = _first_value(row, col.index)
            if v is not None and str(v).strip():
                dimensions = str(v).strip()
                break

        description_parts: list[str] = []
        for col in description_cols:
            v = _first_value(row, col.index)
            if v is not None and str(v).strip():
                description_parts.append(str(v).strip())
        description = "\n\n".join(description_parts) or None

        attribute_values: list[ParsedAttribute] = []
        attribute_ordinals: dict[str, int] = {}
        for col in attribute_cols:
            v = _first_value(row, col.index)
            if v is not None and str(v).strip():
                source_header = attribute_key(col)
                default_code, default_kind, default_unit = ATTRIBUTE_ROLE_DEFAULTS[col.role]
                attribute_code = col.attribute_code or default_code
                value_kind = col.attribute_value_kind or default_kind
                canonical_unit = col.canonical_unit or default_unit
                if value_kind in {"integer", "decimal"} and is_absent_numeric_marker(str(v)):
                    # A dash or «по запросу» in a numeric characteristic is
                    # an empty source cell, not an invalid product property.
                    continue
                # The relational source-key uniqueness uses the normalized
                # header too.  Keep the ordinal in the same namespace so
                # ``Материал`` and ``Материал.`` remain two facts instead of
                # colliding on (item, source_key, ordinal).
                ordinal_key = normalize_header(source_header) or "unnamed"
                ordinal = attribute_ordinals.get(ordinal_key, 0) + 1
                attribute_ordinals[ordinal_key] = ordinal
                attribute_values.append(
                    ParsedAttribute(
                        source_header=source_header,
                        raw_value=str(v).strip(),
                        role=col.role,
                        attribute_code=attribute_code,
                        value_kind=value_kind,
                        canonical_unit=canonical_unit,
                        ordinal=ordinal,
                        source_column_index=col.index,
                    )
                )

        for inline in table.inline_attributes:
            source = _first_value(row, inline.source_column_index)
            if source is None or not str(source).strip():
                continue
            for extracted in extract_inline_values(
                str(source),
                extractor_kind=inline.extractor_kind,
                canonical_unit=inline.canonical_unit,
            ):
                ordinal_key = normalize_header(inline.display_name) or inline.attribute_code
                ordinal = attribute_ordinals.get(ordinal_key, 0) + 1
                attribute_ordinals[ordinal_key] = ordinal
                attribute_values.append(
                    ParsedAttribute(
                        source_header=inline.display_name,
                        raw_value=extracted.raw_value,
                        role="attribute",
                        attribute_code=inline.attribute_code,
                        value_kind=inline.value_kind,
                        canonical_unit=inline.canonical_unit,
                        ordinal=ordinal,
                        source_column_index=inline.source_column_index,
                    )
                )

        values: list[ParsedValue] = []
        row_has_price_error = False
        ambiguous_tier = (
            len(price_cols) > 1
            and bool(threshold_cols)
            and any(
                column.quantity_threshold_column_index is None
                for column in price_cols
            )
        )
        if ambiguous_tier:
            result.issues.append(
                ParseIssue(
                    severity="error",
                    code="PRICE_TIER_AMBIGUOUS",
                    sheet=grid.name,
                    row_no=row_no,
                    message=(
                        "Несколько ценовых колонок не связаны явно с "
                        "quantity_threshold; ценовые ярусы требуют проверки"
                    ),
                )
            )

        def threshold_for_price(price_column) -> Decimal | None:
            linked_index = price_column.quantity_threshold_column_index
            threshold_col = next(
                (column for column in threshold_cols if column.index == linked_index),
                None,
            )
            if threshold_col is None and len(price_cols) == 1 and len(threshold_cols) == 1:
                threshold_col = threshold_cols[0]
            if threshold_col is None:
                return None
            threshold_raw = _first_value(row, threshold_col.index)
            if threshold_raw is None or not str(threshold_raw).strip():
                return None
            try:
                return Decimal(
                    str(threshold_raw).replace("\u00a0", "").replace(" ", "").replace(",", ".")
                ).quantize(Decimal("0.001"))
            except (InvalidOperation, ValueError):
                result.issues.append(
                    ParseIssue(
                        severity="warning",
                        code="QUANTITY_THRESHOLD_PARSE_ERROR",
                        sheet=grid.name,
                        row_no=row_no,
                        column_ref=_col_ref(threshold_col.index),
                        source_value=str(threshold_raw),
                        message="Не удалось разобрать порог ценового яруса",
                    )
                )
                return None

        for col in price_cols:
            min_quantity = threshold_for_price(col)
            if min_quantity is None:
                min_quantity = col.tier_min_quantity
            max_quantity = col.tier_max_quantity
            v = (
                _first_price_value(row, col.index)
                if table.use_display_price_values
                else _first_value(row, col.index)
            )
            parsed = parse_price(v, allow_qualified=table.allow_qualified_prices)
            desc = col.price_descriptor or {}
            kind = desc.get("kind") or "other"
            vat_mode = desc.get("vat_mode") or "unknown"
            column_currency = currency_code(col.header) or desc.get("currency")
            format_currency = (
                currency_from_number_format(row[col.index].number_format)
                if 0 <= col.index < len(row) and not row[col.index].is_merged_fill
                else None
            )
            currency_evidence = {
                value
                for value in (
                    parsed.currency,
                    column_currency,
                    section_currency,
                    format_currency,
                )
                if value is not None
            }
            if len(currency_evidence) > 1 or len(section_currency_codes) > 1:
                result.issues.append(
                    ParseIssue(
                        severity="error",
                        code="PRICE_CURRENCY_CONFLICT",
                        sheet=grid.name,
                        row_no=row_no,
                        column_ref=_col_ref(col.index),
                        source_value=str(v),
                        message=(
                            "Явные источники валюты противоречат друг другу: "
                            + ", ".join(sorted(currency_evidence | section_currency_codes))
                        ),
                    )
                )
            resolved_currency = (
                parsed.currency
                or column_currency
                or section_currency
                or format_currency
                or document_currency
            )
            currency_source = parsed.currency_source
            if resolved_currency is not None and parsed.currency is None:
                if column_currency:
                    currency_source = "column"
                elif section_currency:
                    currency_source = "section"
                else:
                    currency_source = (
                        "number_format" if format_currency else "workbook_number_format"
                    )
            if parsed.status == "empty":
                continue
            if parsed.status == "error":
                row_has_price_error = True
                result.issues.append(
                    ParseIssue(
                        severity="error",
                        code="PRICE_PARSE_ERROR",
                        sheet=grid.name,
                        row_no=row_no,
                        column_ref=_col_ref(col.index),
                        source_value=str(v),
                        message="Не удалось разобрать значение цены",
                    )
                )
                continue
            if parsed.status == "on_request":
                values.append(
                    ParsedValue(
                        kind,
                        None,
                        vat_mode,
                        True,
                        col.header or None,
                        qualifier=parsed.qualifier,
                        currency=resolved_currency,
                        currency_source=currency_source,
                        min_quantity=min_quantity,
                        max_quantity=max_quantity,
                        raw_value=parsed.raw_value,
                        source_column_index=col.index,
                    )
                )
                continue
            if parsed.amount == Decimal("0.00"):
                result.issues.append(
                    ParseIssue(
                        severity="warning",
                        code="ZERO_PRICE",
                        sheet=grid.name,
                        row_no=row_no,
                        column_ref=_col_ref(col.index),
                        source_value=str(v),
                        message="Нулевая цена",
                    )
                )
            values.append(
                ParsedValue(
                    kind,
                    parsed.amount,
                    vat_mode,
                    False,
                    col.header or None,
                    qualifier=parsed.qualifier,
                    amount_from=parsed.amount_from,
                    amount_to=parsed.amount_to,
                    currency=resolved_currency,
                    currency_source=currency_source,
                    min_quantity=min_quantity,
                    max_quantity=max_quantity,
                    basis_quantity=parsed.basis_quantity,
                    basis_unit=parsed.basis_unit,
                    raw_value=parsed.raw_value,
                    source_column_index=col.index,
                )
            )

        if not values and row_has_price_error:
            # Every price cell failed to parse — the row is unusable.
            result.rows_skipped_error += 1
            continue
        if not values:
            # A named row below a price header but with every price cell empty
            # is catalogue/reference content, not a priced offer. Keeping it
            # as an item would make the version look partially parsed and
            # prevents an otherwise exact rule from passing its quality gate.
            result.issues.append(
                ParseIssue(
                    severity="warning",
                    code="NO_PRICE",
                    sheet=grid.name,
                    row_no=row_no,
                    source_value=name_val,
                    message="Позиция без цены пропущена",
                )
            )
            continue

        # Ярус цены по количеству — не позиция каталога, а уровень цены той же
        # позиции. Признак яруса: имя пришло из объединения сверху, а колонка
        # количества держит диапазон («31-60»), а не единицу («шт.»). Без этого
        # в каталоге оказываются четыре неразличимых «Допинг 2.02т» с разной
        # ценой, и сопоставление выбирает из них наугад.
        tier_low, tier_high = quantity_bounds(unit_raw)
        if (tier_low, tier_high) != (None, None):
            for value in values:
                # Цена, объединённая вниз на весь блок ярусов, от партии не
                # зависит — приписать ей порог значило бы выдумать условие.
                if value.source_column_index is not None and _spans_below(
                    grid, r, value.source_column_index
                ):
                    continue
                if value.min_quantity is None:
                    value.min_quantity = Decimal(tier_low) if tier_low is not None else None
                if value.max_quantity is None:
                    value.max_quantity = Decimal(tier_high) if tier_high is not None else None

            name_from_merge = bool(name_cols) and all(
                col.index < len(row) and row[col.index].is_merged_fill for col in name_cols
            )
            if name_from_merge:
                parent = next(
                    (
                        item
                        for item in reversed(result.items)
                        if item.source_sheet == grid.name
                        and item.normalized_name == normalize_name(name_val)
                    ),
                    None,
                )
                if parent is not None:
                    parent.glued_source_rows.append(row_no)
                    parent.values.extend(values)
                    continue

        result.items.append(
            ParsedItem(
                source_name=name_val,
                normalized_name=normalize_name(name_val),
                source_article=article_val,
                normalized_article=normalize_article(article_val) if article_val else None,
                unit_code=normalize_unit(unit_raw),
                category_path=category_path,
                dimensions=dimensions,
                description=description,
                attribute_values=attribute_values,
                source_sheet=grid.name,
                source_row=row_no,
                source_section_id=(
                    table.source_section_id
                    or f"{grid.name}:{table.header_row_index}:{table.start_row_index}:{table.end_row_index}"
                ),
                source_panel_index=(
                    table.source_panel_index if table.source_panel_index is not None else 0
                ),
                source_column_start=(
                    table.source_column_start
                    if table.source_column_start is not None
                    else min((column.index for column in table.columns), default=0)
                ),
                source_column_end=(
                    table.source_column_end
                    if table.source_column_end is not None
                    else max((column.index for column in table.columns), default=0) + 1
                ),
                values=values,
                source_header_cells=(
                    _raw_row_cells(grid.rows[table.header_row_index])
                    if table.header_row_index is not None
                    and 0 <= table.header_row_index < len(grid.rows)
                    else None
                ),
                source_data_cells=_raw_row_cells(row),
                source_category_cells=(
                    [
                        _raw_row_cells(grid.rows[frame.row_no - 1])
                        for frame in category_stack
                        if frame.row_no is not None
                        and 0 < frame.row_no <= len(grid.rows)
                    ]
                    or None
                ),
            )
        )
        result.rows_accepted += 1
