"""Parse-rule mechanics that do not touch the DB: fingerprints, recipes,
detection similarity and validation metrics.

Fingerprint invariant: it is computed from the file structure only (header
row position + normalized header texts), never from data rows — so the same
supplier price list with more rows / new dates keeps the same header_hash.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.parsing.grid import SheetGrid, is_empty_grid
from app.parsing.headers import (
    detect_header,
    detect_header_candidates,
    normalize_header,
    repeating_section_count,
)
from app.parsing.mapping import (
    ColumnMapping,
    InlineAttributeMapping,
    MappingDecision,
    ROLES,
    TableMapping,
    build_auto_mapping,
    default_category_from_sheet_name,
    valid_attribute_value_kind,
)
from app.parsing.inline_attributes import INLINE_EXTRACTOR_KINDS
from app.parsing.pipeline import ParseResult

DEFAULT_OPTIONS = {
    "repeat_headers": "skip",
    "category_rows": "context",
    "default_unit": None,
    # Currency must come from source evidence or the external request hint;
    # a recipe created from structure alone cannot safely invent RUB.
    "currency": None,
}

PARTIAL_JACCARD_THRESHOLD = 0.6

# Validation thresholds (draft -> active gate)
MIN_PRICE_RECOGNIZED_SHARE = 0.95

# These roles are materialized in SupplierPriceItem / SupplierPriceValue.  An
# exact recipe that ignores one now recognized by the detector is stale: its
# header hash still matches, but it no longer describes the data we can retain.
MATERIALIZED_ROLES = {
    "name",
    "article",
    "unit",
    "category",
    "dimensions",
    "description",
    "attribute",
    "manufacturer",
    "quantity_threshold",
    "discount_percent",
    "weight",
    "volume",
    "stock",
    "lead_time",
    "photo",
    "price",
}


# --- fingerprint --------------------------------------------------------------

def sheet_headers(grid: SheetGrid, header_row_index: int) -> list[str]:
    """Normalized header texts of a row, trailing empties stripped."""
    row = grid.rows[header_row_index] if header_row_index < len(grid.rows) else []
    headers = [normalize_header(cell.value) for cell in row]
    while headers and not headers[-1]:
        headers.pop()
    return headers


def compute_fingerprint(loader: str, grids: list[SheetGrid]) -> dict:
    """Deterministic structure fingerprint: for every detected table header —
    its normalized sheet name, row index and header tuple.

    Computed the same way at detection time and at rule-creation time, so an
    identical file structure always produces an identical fingerprint.  A
    vertically stacked second table is part of that structure; omitting it
    could make a two-table recipe match a one-table file and address rows that
    no longer exist.
    """
    signatures = []
    for grid in grids:
        for detection in detect_header_candidates(grid):
            if detection.header_row_index is None:
                continue
            signatures.append(
                {
                    "sheet_name": normalize_header(grid.name),
                    "header_row": detection.header_row_index,
                    "headers": sheet_headers(grid, detection.header_row_index),
                }
            )
    return {"loader": loader, "sheet_signatures": signatures}


def compute_header_hash(fingerprint: dict) -> str:
    loader = fingerprint.get("loader", "")
    sheet_keys = sorted(
        "\x1f".join([str(sig.get("header_row", "")), *sig.get("headers", [])])
        for sig in fingerprint.get("sheet_signatures", [])
    )
    canonical = loader + "\x1e" + "\x1d".join(sheet_keys)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fingerprint_header_set(fingerprint: dict) -> set[str]:
    headers: set[str] = set()
    for sig in fingerprint.get("sheet_signatures", []):
        headers.update(h for h in sig.get("headers", []) if h)
    return headers


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


@dataclass
class PartialCandidate:
    rule_id: str
    score: float


def partial_similarity(fingerprint_a: dict, fingerprint_b: dict) -> float:
    """Max Jaccard similarity between per-sheet header sets of two fingerprints."""
    best = 0.0
    sigs_a = fingerprint_a.get("sheet_signatures", [])
    sigs_b = fingerprint_b.get("sheet_signatures", [])
    for sa in sigs_a:
        set_a = {h for h in sa.get("headers", []) if h}
        for sb in sigs_b:
            set_b = {h for h in sb.get("headers", []) if h}
            best = max(best, jaccard(set_a, set_b))
    return best


# --- recipe <-> mapping -------------------------------------------------------

def recipe_from_decision(decision: MappingDecision, grids: list[SheetGrid]) -> dict:
    """Serialize an applied mapping into a self-sufficient recipe. Each column
    keeps the normalized header text so a partial match can re-align columns
    by name when their positions shift."""
    grid_index = {g.name: i for i, g in enumerate(grids)}
    grids_by_name = {g.name: g for g in grids}
    sheets = []
    for table in decision.tables:
        grid = grids_by_name.get(table.sheet_name)
        headers = (
            sheet_headers(grid, table.header_row_index)
            if grid is not None and table.header_row_index is not None
            else []
        )
        columns = []
        for col in table.columns:
            entry: dict = {"index": col.index, "role": col.role}
            if col.price_descriptor:
                entry["price_descriptor"] = dict(col.price_descriptor)
            if col.attribute_code:
                entry["attribute_code"] = col.attribute_code
            if col.attribute_value_kind:
                entry["attribute_value_kind"] = col.attribute_value_kind
            if col.canonical_unit:
                entry["canonical_unit"] = col.canonical_unit
            if col.quantity_threshold_column_index is not None:
                entry["quantity_threshold_column_index"] = (
                    col.quantity_threshold_column_index
                )
            if col.index < len(headers) and headers[col.index]:
                entry["header"] = headers[col.index]
            columns.append(entry)
        # Excel's used range often extends dozens of formatted but completely
        # empty columns past the actual table. Keep internal blank columns —
        # their indexes describe real separators between parallel panels — but
        # do not persist a meaningless empty tail in the executable recipe.
        while (
            columns
            and columns[-1].get("role") == "ignore"
            and not columns[-1].get("header")
        ):
            columns.pop()
        # A sheet that repeats the same section layout (category caption ->
        # identical header -> positions) is stored as one template instead of a
        # fixed header row, so the rule keeps working when the supplier adds or
        # removes sections and the editor does not show N duplicate entries.
        section_count = (
            repeating_section_count(grid, table.header_row_index)
            if grid is not None and table.header_row_index is not None
            else 0
        )
        is_repeating = section_count >= 2
        sheet_category = default_category_from_sheet_name(table.sheet_name)
        category_from_sheet_name = (
            sheet_category is not None
            and table.default_category_path == sheet_category
        )
        sheets.append(
            {
                "match": {
                    "name_pattern": table.sheet_name,
                    "or_index": grid_index.get(table.sheet_name, 0),
                },
                "skip": False,
                "header_row": (
                    None
                    if is_repeating or table.locate_header_by_signature
                    else table.header_row_index
                ),
                **(
                    {"headerless": True}
                    if table.header_row_index is None
                    else {}
                ),
                **(
                    {"repeat": True, "repeat_sections": section_count}
                    if is_repeating
                    else {}
                ),
                **(
                    {"header_match": "signature"}
                    if table.locate_header_by_signature
                    else {}
                ),
                "start_row": None if table.dynamic_body else table.start_row_index,
                "end_row": None if table.dynamic_body else table.end_row_index,
                **({"dynamic_body": True} if table.dynamic_body else {}),
                "name_fallback_column_indexes": list(
                    table.name_fallback_column_indexes
                ),
                "source_section_id": table.source_section_id,
                "source_panel_index": table.source_panel_index,
                "source_column_start": table.source_column_start,
                "source_column_end": table.source_column_end,
                "default_category_source": (
                    "sheet_name" if category_from_sheet_name else None
                ),
                "default_category_path": (
                    None
                    if category_from_sheet_name
                    else table.default_category_path
                ),
                **(
                    {"allow_qualified_prices": True}
                    if table.allow_qualified_prices
                    else {}
                ),
                **(
                    {"use_display_price_values": False}
                    if not table.use_display_price_values
                    else {}
                ),
                "columns": columns,
                "inline_attributes": [
                    {
                        "source_column_index": attribute.source_column_index,
                        "attribute_code": attribute.attribute_code,
                        "display_name": attribute.display_name,
                        "value_kind": attribute.value_kind,
                        "canonical_unit": attribute.canonical_unit,
                        "extractor_kind": attribute.extractor_kind,
                    }
                    for attribute in table.inline_attributes
                ],
            }
        )
    return {"sheets": sheets, "options": dict(DEFAULT_OPTIONS)}


def _resolve_grid(match: dict, grids: list[SheetGrid]) -> SheetGrid | None:
    """Sheet lookup: normalized name first, positional index as fallback."""
    wanted = normalize_header(match.get("name_pattern") or "")
    if wanted:
        for grid in grids:
            if normalize_header(grid.name) == wanted:
                return grid
    idx = match.get("or_index")
    if isinstance(idx, int) and 0 <= idx < len(grids):
        return grids[idx]
    return None


def _match_repeating_header_row(grid: SheetGrid, columns: list[dict]) -> int | None:
    """First source row whose labels match a generated header template.

    A repeating-section recipe does not pin a header row number, so the row is
    re-located by structure on every parse: the template's expected headers are
    compared against each detected header row.  A non-empty cell must equal the
    stored header; an empty cell falls back to the stored header — the same
    semantics as the exact-match validation in ``recipe_to_decision``.
    """
    expected = [
        (int(column["index"]), normalize_header(column["header"]))
        for column in columns
        if column.get("header")
        and isinstance(column.get("index"), int)
        and not isinstance(column.get("index"), bool)
    ]
    if not expected:
        return None
    for row_index, row in enumerate(grid.rows):

        def cell_matches(index: int, wanted: str) -> bool:
            if not 0 <= index < len(row):
                return False
            # Spreadsheet loaders deliberately copy a merged header's source
            # value into covered cells and mark them as merged fills. A body
            # column can legitimately live under one of those covered cells
            # (for example names in C under a merged A:F label), so the copied
            # value is valid signature evidence even though it is not a data
            # cell.
            value = row[index].value
            return bool(value is not None and str(value).strip()) and (
                normalize_header(value) == wanted
            )

        if all(cell_matches(index, wanted) for index, wanted in expected):
            return row_index
    return None


def recipe_to_decision(recipe: dict, grids: list[SheetGrid]) -> MappingDecision:
    """Apply a recipe verbatim (exact match): columns by index, header row
    from the recipe (auto-detected when null)."""
    decision = MappingDecision()
    matched_names: set[str] = set()
    for sheet in recipe.get("sheets", []):
        if sheet.get("skip"):
            continue
        grid = _resolve_grid(sheet.get("match", {}), grids)
        if grid is None:
            continue
        if is_empty_grid(grid):
            continue
        matched_names.add(grid.name)
        header_row = sheet.get("header_row")
        headerless = bool(sheet.get("headerless")) and header_row is None
        repeating = bool(sheet.get("repeat")) and not headerless
        signature_header = sheet.get("header_match") == "signature" and not headerless
        if header_row is None and not headerless:
            if repeating or signature_header:
                header_row = _match_repeating_header_row(grid, sheet.get("columns", []))
            if header_row is None:
                header_row = detect_header(grid).header_row_index
        # Rules are persisted data.  A stale recipe must never address a row
        # outside the current grid (or a row whose selected headers changed),
        # otherwise a multi-table rule can turn an ordinary refresh into an
        # IndexError.  A later auto/partial decision handles the file safely.
        if not headerless and (
            not isinstance(header_row, int) or not (0 <= header_row < len(grid.rows))
        ):
            continue
        source_header_row = (
            grid.rows[header_row] if isinstance(header_row, int) else None
        )

        def source_header(column: dict) -> str:
            index = column.get("index")
            if (
                source_header_row is not None
                and isinstance(index, int)
                and 0 <= index < len(source_header_row)
            ):
                value = source_header_row[index].value
                if value is not None and str(value).strip():
                    return str(value)
            return str(column.get("header", ""))

        # Recipes made before a supplier changes a header should not be
        # treated as an exact mapping merely because their row number survives.
        # Older recipes without header evidence remain supported.
        expected_headers = [
            column
            for column in sheet.get("columns", [])
            if column.get("header") and isinstance(column.get("index"), int)
        ]
        if any(
            normalize_header(source_header(column)) != normalize_header(column["header"])
            for column in expected_headers
        ):
            continue
        columns = [
            ColumnMapping(
                index=int(col["index"]),
                role=col.get("role", "ignore"),
                price_descriptor=col.get("price_descriptor"),
                # Persisted recipes deliberately use normalized headers for
                # matching.  Facts, however, retain the exact source spelling
                # so exports/UI do not turn «Толщина металла» into lowercase.
                header=source_header(col),
                attribute_code=col.get("attribute_code"),
                attribute_value_kind=valid_attribute_value_kind(col.get("attribute_value_kind")),
                canonical_unit=col.get("canonical_unit"),
                quantity_threshold_column_index=(
                    int(col["quantity_threshold_column_index"])
                    if isinstance(col.get("quantity_threshold_column_index"), int)
                    and not isinstance(col.get("quantity_threshold_column_index"), bool)
                    else None
                ),
            )
            for col in sheet.get("columns", [])
            if col.get("role", "ignore") in ROLES
        ]
        table = TableMapping(
            sheet_name=grid.name,
            header_row_index=header_row,
            columns=columns,
            start_row_index=(
                int(sheet["start_row"])
                if isinstance(sheet.get("start_row"), int)
                and not isinstance(sheet.get("start_row"), bool)
                and 0 <= sheet["start_row"] < len(grid.rows)
                else None
            ),
            end_row_index=(
                int(sheet["end_row"])
                if isinstance(sheet.get("end_row"), int)
                and not isinstance(sheet.get("end_row"), bool)
                and 0 < sheet["end_row"] <= len(grid.rows)
                else None
            ),
            name_fallback_column_indexes=[
                index
                for index in sheet.get("name_fallback_column_indexes", [])
                if isinstance(index, int)
                and not isinstance(index, bool)
                and any(column.index == index for column in columns)
            ],
            allow_qualified_prices=bool(sheet.get("allow_qualified_prices", False)),
            use_display_price_values=bool(
                sheet.get("use_display_price_values", True)
            ),
            source_section_id=(
                str(sheet["source_section_id"])
                if sheet.get("source_section_id") is not None
                else None
            ),
            source_panel_index=(
                int(sheet["source_panel_index"])
                if isinstance(sheet.get("source_panel_index"), int)
                and not isinstance(sheet.get("source_panel_index"), bool)
                else None
            ),
            source_column_start=(
                int(sheet["source_column_start"])
                if isinstance(sheet.get("source_column_start"), int)
                and not isinstance(sheet.get("source_column_start"), bool)
                else None
            ),
            source_column_end=(
                int(sheet["source_column_end"])
                if isinstance(sheet.get("source_column_end"), int)
                and not isinstance(sheet.get("source_column_end"), bool)
                else None
            ),
            default_category_path=(
                default_category_from_sheet_name(grid.name)
                if sheet.get("default_category_source") == "sheet_name"
                else (
                    str(sheet["default_category_path"]).strip()
                    if sheet.get("default_category_path")
                    else default_category_from_sheet_name(grid.name)
                )
            ),
            locate_header_by_signature=signature_header,
            dynamic_body=bool(sheet.get("dynamic_body", False)),
            inline_attributes=[
                InlineAttributeMapping(
                    source_column_index=int(attribute["source_column_index"]),
                    attribute_code=str(attribute["attribute_code"]),
                    display_name=str(attribute["display_name"]),
                    value_kind=str(attribute["value_kind"]),
                    canonical_unit=attribute.get("canonical_unit"),
                    extractor_kind=str(attribute["extractor_kind"]),
                )
                for attribute in sheet.get("inline_attributes", [])
                if isinstance(attribute, dict)
                and isinstance(attribute.get("source_column_index"), int)
                and attribute.get("extractor_kind") in INLINE_EXTRACTOR_KINDS
                and valid_attribute_value_kind(attribute.get("value_kind")) is not None
            ],
        )
        if table.has_role("name") and table.has_role("price"):
            decision.tables.append(table)
    decision.non_price_sheets = [
        g.name for g in grids if g.name not in matched_names and not is_empty_grid(g)
    ]
    tables_by_sheet: dict[str, list[TableMapping]] = {}
    for table in decision.tables:
        tables_by_sheet.setdefault(table.sheet_name, []).append(table)
    for tables in tables_by_sheet.values():
        tables.sort(
            key=lambda table: (
                table.start_row_index
                if table.start_row_index is not None
                else (
                    table.header_row_index
                    if table.header_row_index is not None
                    else float("inf")
                )
            )
        )
        for index, table in enumerate(tables[:-1]):
            if table.end_row_index is None:
                next_table = tables[index + 1]
                table.end_row_index = (
                    next_table.start_row_index
                    if next_table.start_row_index is not None
                    else next_table.header_row_index
                )
    decision.auto_ok = len(decision.tables) > 0
    return decision


def recipe_mapping_gaps(recipe: dict, grids: list[SheetGrid]) -> list[dict]:
    """Return recognized persisted columns that an exact recipe leaves wrong.

    Exact matching is structural, so a saved recipe can outlive a newly added
    header alias.  The recipe must therefore still cover every high-confidence
    field the current deterministic detector recognizes.
    """
    detected = build_auto_mapping(grids)
    applied = recipe_to_decision(recipe, grids)
    applied_by_section = {
        (table.sheet_name, table.header_row_index): table for table in applied.tables
    }
    gaps: list[dict] = []

    for detected_table in detected.tables:
        applied_table = applied_by_section.get(
            (detected_table.sheet_name, detected_table.header_row_index)
        )
        roles_by_index = (
            {column.index: column.role for column in applied_table.columns}
            if applied_table is not None
            else {}
        )
        for detected_column in detected_table.columns:
            if detected_column.role not in MATERIALIZED_ROLES:
                continue
            actual_role = roles_by_index.get(detected_column.index, "ignore")
            if actual_role != detected_column.role:
                gaps.append(
                    {
                        "sheet": detected_table.sheet_name,
                        "index": detected_column.index,
                        "header": detected_column.header,
                        "expected_role": detected_column.role,
                        "actual_role": actual_role,
                    }
                )
    return gaps


def recipe_prefill_mapping(recipe: dict, grids: list[SheetGrid]) -> dict:
    """Partial match: prefill a stored-mapping json from a similar rule's
    recipe, re-aligning columns by normalized header name (positions may have
    shifted); columns whose header is gone are dropped."""
    tables = []
    for sheet in recipe.get("sheets", []):
        if sheet.get("skip"):
            continue
        grid = _resolve_grid(sheet.get("match", {}), grids)
        if grid is None:
            continue
        detection = detect_header(grid)
        current_headers = (
            sheet_headers(grid, detection.header_row_index)
            if detection.header_row_index is not None
            else []
        )
        index_by_header: dict[str, int] = {}
        for i, h in enumerate(current_headers):
            if h and h not in index_by_header:
                index_by_header[h] = i
        columns = []
        for col in sheet.get("columns", []):
            role = col.get("role", "ignore")
            if role == "ignore" or role not in ROLES:
                continue
            header = col.get("header")
            if header and header in index_by_header:
                new_index = index_by_header[header]
            elif not current_headers and isinstance(col.get("index"), int):
                new_index = col["index"]
            else:
                continue
            entry: dict = {"index": new_index, "role": role}
            if col.get("price_descriptor"):
                entry["price_descriptor"] = dict(col["price_descriptor"])
            if col.get("attribute_code"):
                entry["attribute_code"] = str(col["attribute_code"])
            attribute_value_kind = valid_attribute_value_kind(col.get("attribute_value_kind"))
            if attribute_value_kind:
                entry["attribute_value_kind"] = attribute_value_kind
            if col.get("canonical_unit"):
                entry["canonical_unit"] = str(col["canonical_unit"])
            columns.append(entry)
        if columns:
            tables.append({"sheet_name": grid.name, "columns": columns})
    return {"tables": tables}


# --- validation metrics -------------------------------------------------------

def validation_metrics(result: ParseResult) -> dict:
    """Rule quality gate over a completed parse."""
    rows_accepted = result.rows_accepted
    items_with_price = sum(1 for item in result.items if item.values)
    price_share = items_with_price / rows_accepted if rows_accepted else 0.0
    blocking_errors = sum(
        1 for i in result.issues if i.severity == "error" and i.code == "PRICE_PARSE_ERROR"
    )
    no_name_rows = sum(1 for i in result.issues if i.code == "NO_NAME")
    metrics = {
        "rows_accepted": rows_accepted,
        "items_with_price": items_with_price,
        "price_recognized_share": round(price_share, 4),
        "blocking_errors": blocking_errors,
        "no_name_rows": no_name_rows,
        "errors_count": result.errors_count,
        "warnings_count": result.warnings_count,
        "quality_score": result.quality_score,
    }
    passed = (
        rows_accepted > 0
        and price_share >= MIN_PRICE_RECOGNIZED_SHARE
        and blocking_errors == 0
        and no_name_rows == 0
    )
    return {"metrics": metrics, "passed": passed}
