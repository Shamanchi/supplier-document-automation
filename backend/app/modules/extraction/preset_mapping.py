"""Preset-based extraction strategy ported from the Phase 2 import workbench."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from app.modules.extraction.matrix_extraction import extract_matrices
from app.modules.extraction.matrix_types import Cell, Table
from app.modules.extraction.parsers import (
    ParameterCandidate,
    ParsedDocument,
    ParsedFragment,
    normalize_text,
)

PARTIAL_MATCH_THRESHOLD = 0.45

DEFAULT_UNITS = [
    "м3/ч",
    "м3/час",
    "м³/ч",
    "м3/мин",
    "л/с",
    "л/мин",
    "м3",
    "м³",
    "л",
    "кг",
    "т",
    "т/ч",
    "кВт",
    "квт",
    "вт",
    "л.с.",
    "квт/ч",
    "об/мин",
    "об/с",
    "мин-1",
    "мин",
    "с",
    "ч",
    "мм",
    "см",
    "м",
    "км",
    "°с",
    "°с.",
    "%",
    "в",
    "а",
    "гц",
    "кпа",
    "мпа",
    "бар",
    "кгс/см2",
    "кгс/см²",
    "н",
    "кн",
    "па",
    "мм рт. ст.",
    "атм",
    "градус",
    "шт",
    "раз",
]

SKIP_LABELS = {"№", "№ п/п", "п/п", "nn", "номер", "кол-во", "количество"}
HEADER_VOCAB = {
    "наименование",
    "показатель",
    "характеристика",
    "параметр",
    "значение",
    "величина",
    "ед. изм.",
    "единица измерения",
    "единица",
    "кол-во",
    "количество",
    "номер",
    "п/п",
    "норма",
    "допуск",
    "тип",
    "марка",
    "модель",
}


@dataclass(frozen=True)
class PresetConfig:
    code: str
    name: str
    doc_type: str = "passport"
    columns: dict[str, Any] = field(default_factory=dict)
    labels: list[str] = field(default_factory=list)
    section_keywords: list[str] = field(default_factory=list)
    header_keywords: list[str] = field(default_factory=list)
    units: list[str] = field(default_factory=list)
    include_all: bool = False
    keyvalue: bool = False
    fingerprint: str | None = None


@dataclass(frozen=True)
class PresetMatch:
    preset: PresetConfig | None
    score: float
    method: str
    table_id: str | None = None


@dataclass(frozen=True)
class ExtractedWithPreset:
    candidates: list[ParameterCandidate]
    preset: PresetConfig | None
    score: float | None
    method: str | None
    table_id: str | None
    warnings: list[str]


@dataclass(frozen=True)
class _Row:
    fragment_index: tuple[int, int]
    cells: tuple[str, ...]
    text: str
    locator: dict[str, Any]
    page_no: int
    row_no: int
    cell_boxes: tuple[dict, ...] | None = None


@dataclass(frozen=True)
class _Table:
    id: str
    rows: list[_Row]
    page_no: int | None = None
    sheet: str | None = None
    bbox: tuple[float | None, ...] | None = None
    source: str | None = None

    @property
    def n_cols(self) -> int:
        return max((len(row.cells) for row in self.rows), default=0)

    @property
    def table_id(self) -> str:
        return self.id

    @property
    def sheet_name(self) -> str | None:
        return self.sheet

    @property
    def n_rows(self) -> int:
        return len(self.rows)


def preset_from_model(model) -> PresetConfig:
    """Build a pure preset config from an ORM model or schema-like object."""
    return PresetConfig(
        code=model.code,
        name=model.name,
        doc_type=getattr(model, "doc_type", "passport") or "passport",
        columns=dict(getattr(model, "columns", {}) or {}),
        labels=list(getattr(model, "labels", []) or []),
        section_keywords=list(getattr(model, "section_keywords", []) or []),
        header_keywords=list(getattr(model, "header_keywords", []) or []),
        units=list(getattr(model, "units", []) or []),
        include_all=bool(getattr(model, "include_all", False)),
        keyvalue=bool(getattr(model, "keyvalue", False)),
        fingerprint=getattr(model, "fingerprint", None),
    )


def extract_with_presets(
    document: ParsedDocument,
    presets: list[PresetConfig],
    *,
    preset_code: str | None = None,
    table_id: str | None = None,
    include_all: bool = False,
) -> ExtractedWithPreset:
    tables = tables_from_document(document)
    selected = _select_preset(presets, tables, preset_code=preset_code)
    warnings: list[str] = []
    if selected.preset is None:
        if preset_code:
            warnings.append(f"Preset {preset_code} not found")
        return ExtractedWithPreset([], None, selected.score, selected.method, None, warnings)

    target_table_id = table_id or selected.table_id
    if _is_pump_main(selected.preset):
        candidates = _extract_pump_main(document, tables)
        if not candidates:
            warnings.append("Pump matrix parameters were not found")
        return ExtractedWithPreset(
            candidates,
            selected.preset,
            selected.score,
            selected.method,
            target_table_id,
            warnings,
        )

    if selected.preset.keyvalue:
        candidates = _extract_keyvalue(document, selected.preset, include_all)
        return ExtractedWithPreset(
            candidates,
            selected.preset,
            selected.score,
            selected.method,
            target_table_id,
            warnings,
        )

    if not tables:
        warnings.append("Tables were not found in the parsed document")
        return ExtractedWithPreset(
            [],
            selected.preset,
            selected.score,
            selected.method,
            target_table_id,
            warnings,
        )

    target = _table_by_id(tables, target_table_id) or tables[0]
    candidates = _extract_from_table(
        target,
        selected.preset,
        include_all=include_all,
    )
    return ExtractedWithPreset(
        candidates,
        selected.preset,
        selected.score,
        selected.method,
        target.id,
        warnings,
    )


def tables_from_document(document: ParsedDocument) -> list[_Table]:
    grouped: dict[str, _Table] = {}
    for page_index, page in enumerate(document.pages):
        for fragment_index, fragment in enumerate(page.fragments):
            if fragment.cells is None:
                continue
            table_id = _fragment_table_id(fragment, fallback=f"p{page.page_no}")
            row_no = int(fragment.locator.get("row") or len(grouped.get(table_id, _Table(table_id, [])).rows) + 1)
            table = grouped.get(table_id)
            if table is None:
                table = _Table(
                    id=table_id,
                    rows=[],
                    page_no=page.page_no,
                    sheet=fragment.locator.get("sheet"),
                    bbox=(
                        tuple(fragment.table_bbox)
                        if fragment.table_bbox is not None
                        else None
                    ),
                    source=fragment.table_source,
                )
                grouped[table_id] = table
            table.rows.append(
                _Row(
                    fragment_index=(page_index, fragment_index),
                    cells=tuple(fragment.cells),
                    text=fragment.text,
                    locator=fragment.locator,
                    page_no=page.page_no,
                    row_no=row_no,
                    cell_boxes=fragment.cell_boxes,
                )
            )
    return list(grouped.values())


def document_signature(document: ParsedDocument) -> dict[str, Any]:
    tables = []
    for table in tables_from_document(document):
        header_row = candidate_header_row(table)
        tables.append(
            {
                "id": table.id,
                "cols": table.n_cols,
                "headers": (
                    [normalize_header(cell) for cell in table.rows[header_row].cells]
                    if header_row is not None
                    else []
                ),
                "first_col_labels": first_col_labels(table),
            }
        )
    loader = _loader_name(document)
    return {"loader": loader, "tables": tables}


def compute_exact_fingerprint(signature: dict[str, Any]) -> str:
    parts = []
    for table in sorted(signature["tables"], key=lambda item: item["id"]):
        parts.append(f"{table['cols']}|" + "\x1f".join(table["headers"]))
    canonical = signature["loader"] + "\x1e" + "\x1d".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def match_presets(presets: list[PresetConfig], signature: dict[str, Any]) -> PresetMatch:
    exact = compute_exact_fingerprint(signature)
    best = PresetMatch(None, 0.0, "none")
    for preset in presets:
        if preset.fingerprint == exact:
            return PresetMatch(preset, 1.0, "exact")
        score = 0.0
        table_id = None
        for table in signature["tables"]:
            candidate = _table_score(preset, table)
            if candidate > score:
                score = candidate
                table_id = table["id"]
        if score > best.score:
            best = PresetMatch(preset, score, "partial", table_id)
    if best.preset is None or best.score < PARTIAL_MATCH_THRESHOLD:
        return PresetMatch(None, best.score, "none")
    return best


def _select_preset(
    presets: list[PresetConfig],
    tables: list[_Table],
    *,
    preset_code: str | None,
) -> PresetMatch:
    if preset_code:
        preset = next((item for item in presets if item.code == preset_code), None)
        return PresetMatch(preset, 1.0 if preset else 0.0, "manual")
    if not tables:
        return PresetMatch(None, 0.0, "none")
    signature = {
        "loader": _loader_from_tables(tables),
        "tables": [
            {
                "id": table.id,
                "cols": table.n_cols,
                "headers": (
                    [normalize_header(cell) for cell in table.rows[header].cells]
                    if (header := candidate_header_row(table)) is not None
                    else []
                ),
                "first_col_labels": first_col_labels(table),
            }
            for table in tables
        ],
    }
    return match_presets(presets, signature)


def _extract_from_table(
    table: _Table,
    preset: PresetConfig,
    *,
    include_all: bool,
) -> list[ParameterCandidate]:
    cfg = preset.columns or {}
    col_roles = dict(cfg.get("roles", {}))
    header_map = cfg.get("header", {})
    start_row = 0
    if cfg.get("has_header") and header_map:
        for index, row in enumerate(table.rows[:12]):
            mapped = {normalize_header(cell): col for col, cell in enumerate(row.cells) if normalize_header(cell)}
            matched: dict[str, int] = {}
            for text, col in mapped.items():
                role = header_map.get(text)
                if role is None:
                    for key, candidate_role in header_map.items():
                        norm_key = normalize_header(key)
                        if norm_key and (norm_key in text or text in norm_key):
                            role = candidate_role
                            break
                if role:
                    matched.setdefault(role, col)
            if matched:
                col_roles = {**col_roles, **matched}
                start_row = index + 1
                break

    label_col = int(col_roles.get("label", 0))
    value_col = int(col_roles.get("value", label_col + 1))
    unit_col = col_roles.get("unit")
    unit_col = int(unit_col) if unit_col is not None else None
    units = list(preset.units or DEFAULT_UNITS)
    vocab = {normalize_header(label) for label in preset.labels if label.strip()}
    section_keywords = list(preset.section_keywords)
    allow_all = include_all or preset.include_all
    current_section: str | None = None
    seen_names: set[str] = set()
    candidates: list[ParameterCandidate] = []

    for row_index, row in enumerate(table.rows):
        if row_index < start_row:
            continue
        section = _section(row, section_keywords)
        if section:
            current_section = section
            continue

        label_text = _cell(row, label_col)
        if not label_text or looks_numeric(label_text):
            continue
        matched = _match_label(label_text, vocab)
        if not matched and not allow_all:
            continue

        value_text = _cell(row, value_col)
        if not value_text:
            continue
        if unit_col is not None:
            value, unit = collapse_ws(value_text), _cell(row, unit_col) or None
        else:
            value, unit = _split_value_unit(value_text, units)
        if not value:
            continue

        name, label_unit = _split_label_unit(label_text, units)
        unit = unit or label_unit
        if current_section:
            if name in seen_names:
                name = f"{name} ({current_section})"
            else:
                seen_names.add(name)
        candidates.append(
            ParameterCandidate(
                fragment_index=row.fragment_index,
                name=name,
                value=value,
                unit=unit,
                quote=row.text,
                method="TEMPLATE",
                confidence=0.95,
            )
        )
    return candidates


def _extract_keyvalue(
    document: ParsedDocument, preset: PresetConfig, include_all: bool
) -> list[ParameterCandidate]:
    units = list(preset.units or DEFAULT_UNITS)
    vocab = {normalize_header(label) for label in preset.labels if label.strip()}
    allow_all = include_all or preset.include_all
    seen: set[str] = set()
    candidates: list[ParameterCandidate] = []
    for page_index, page in enumerate(document.pages):
        for fragment_index, fragment in enumerate(page.fragments):
            match = re.match(r"^\s*(.+?)\s*[:–—\-]\s*(.+?)\s*$", fragment.text)
            if match is None:
                continue
            label_raw, value_raw = match.group(1).strip(), match.group(2).strip()
            name, label_unit = _split_label_unit(label_raw, units)
            matched = _match_label(name, vocab)
            if not matched and not allow_all:
                continue
            value, unit = _split_value_unit(value_raw, units)
            unit = unit or label_unit
            if not value or (name in seen and not allow_all):
                continue
            seen.add(name)
            candidates.append(
                ParameterCandidate(
                    fragment_index=(page_index, fragment_index),
                    name=name,
                    value=value,
                    unit=unit,
                    quote=fragment.text,
                    method="TEMPLATE",
                    confidence=0.9,
                )
            )
    return candidates


def _is_pump_main(preset: PresetConfig) -> bool:
    strategy = str(preset.columns.get("strategy") or preset.columns.get("mode") or "")
    return preset.code == "pump_main" or strategy == "pump_main"


def _extract_pump_main(
    document: ParsedDocument,
    tables: list[_Table],
) -> list[ParameterCandidate]:
    if not tables and not document.page_words:
        return []
    matrix_tables = [_to_matrix_table(table) for table in tables]
    raw_parameters = extract_matrices(
        matrix_tables,
        doc_name="document",
        units=DEFAULT_UNITS,
        page_words=[list(page) for page in document.page_words],
    )
    row_by_table: dict[tuple[str, int], _Row] = {}
    for table in tables:
        for row in table.rows:
            row_by_table[(table.id, row.row_no)] = row
    page_context = _page_context_fragments(document)
    candidates: list[ParameterCandidate] = []
    seen: set[tuple[str, str, str | None, tuple[int, int]]] = set()
    for parameter in raw_parameters:
        evidence = parameter.evidence
        row = None
        if evidence and evidence.table and evidence.row:
            row = row_by_table.get((evidence.table, evidence.row))
        if row is not None:
            fragment_index = row.fragment_index
            quote = row.text
        elif evidence and evidence.page is not None:
            fragment_index, quote = page_context.get(evidence.page, ((0, 0), ""))
        else:
            continue
        if not quote:
            continue
        candidate = ParameterCandidate(
            fragment_index=fragment_index,
            name=parameter.name,
            value=parameter.value,
            unit=parameter.unit,
            quote=quote,
            method="TEMPLATE",
            confidence=0.97,
        )
        key = (
            candidate.name.casefold(),
            candidate.value.casefold(),
            candidate.unit.casefold() if candidate.unit else None,
            candidate.fragment_index,
        )
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


def _to_matrix_table(table: _Table) -> Table:
    rows: list[list[Cell]] = []
    for row_index, row in enumerate(table.rows):
        boxes = list(row.cell_boxes or ())
        cells: list[Cell] = []
        for col_index, text in enumerate(row.cells):
            box = boxes[col_index] if col_index < len(boxes) else {}
            cells.append(
                Cell(
                    text=text,
                    row=row_index,
                    col=col_index,
                    page=(row.page_no - 1) if row.page_no else None,
                    x0=_float_or_none(box.get("x0")),
                    y0=_float_or_none(box.get("y0")),
                    x1=_float_or_none(box.get("x1")),
                    y1=_float_or_none(box.get("y1")),
                )
            )
        rows.append(cells)
    return Table(
        id=table.id,
        rows=rows,
        sheet=table.sheet,
        page=(table.page_no - 1) if table.page_no else None,
        bbox=list(table.bbox or ()),
        source=table.source,
    )


def _page_context_fragments(
    document: ParsedDocument,
) -> dict[int, tuple[tuple[int, int], str]]:
    result: dict[int, tuple[tuple[int, int], str]] = {}
    for page_index, page in enumerate(document.pages):
        for fragment_index, fragment in enumerate(page.fragments):
            if fragment.locator.get("pageWords"):
                result[page.page_no] = ((page_index, fragment_index), fragment.text)
                break
    return result


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fragment_table_id(fragment: ParsedFragment, *, fallback: str) -> str:
    locator = fragment.locator
    if "table" in locator:
        return f"table:{locator['table']}"
    if "sheet" in locator:
        return f"sheet:{locator['sheet']}"
    return fallback


def _table_by_id(tables: list[_Table], table_id: str | None) -> _Table | None:
    if table_id is None:
        return None
    return next((table for table in tables if table.id == table_id), None)


def _cell(row: _Row, index: int | None) -> str:
    if index is None or index >= len(row.cells):
        return ""
    return row.cells[index].strip()


def _split_value_unit(value_text: str, units: list[str]) -> tuple[str, str | None]:
    value = collapse_ws(value_text)
    lowered = value.lower()
    for unit in sorted(units, key=len, reverse=True):
        candidate = unit.strip().lower()
        if candidate and lowered.endswith(candidate):
            index = len(lowered) - len(candidate)
            if index == 0 or lowered[index - 1].isspace():
                return value[:index].strip(), value[index:].strip()
    return value, None


def _split_label_unit(label_text: str, units: list[str]) -> tuple[str, str | None]:
    label = label_text.strip()
    match = re.search(r"\(([^()]+)\)\s*$", label)
    unit_set = {unit.lower() for unit in units}
    if match and match.group(1).strip().lower() in unit_set:
        return label[: match.start()].strip(), match.group(1).strip()
    for unit in sorted(units, key=len, reverse=True):
        for separator in (", ", " "):
            suffix = separator + unit.lower()
            if label.lower().endswith(suffix):
                return label[: -len(suffix)].strip(), unit
    return label, None


def _match_label(cell_text: str, vocab: set[str]) -> str | None:
    text = normalize_header(cell_text)
    if not text or text in SKIP_LABELS:
        return None
    if not vocab:
        return text
    if text in vocab:
        return text
    return next(
        (
            label
            for label in vocab
            if label and len(label) >= 3 and (label in text or text in label)
        ),
        None,
    )


def _section(row: _Row, section_keywords: list[str]) -> str | None:
    filled = [cell for cell in row.cells if cell.strip()]
    if len(filled) != 1:
        return None
    text = filled[0].strip()
    normalized = normalize_header(text)
    if looks_numeric(text):
        return None
    for keyword in section_keywords:
        candidate = normalize_header(keyword)
        if candidate and (candidate in normalized or normalized in candidate):
            return collapse_ws(text)
    if len(text) <= 70 and text.isupper():
        return collapse_ws(text)
    return None


def candidate_header_row(table: _Table, max_scan: int = 8) -> int | None:
    for index, row in enumerate(table.rows[:max_scan]):
        texts = [cell.strip() for cell in row.cells]
        if not any(texts) or any(looks_numeric(text) for text in texts):
            continue
        if _has_header_vocab(texts) and all(0 < len(text) <= 60 for text in texts if text):
            return index
    for index, row in enumerate(table.rows[:4]):
        texts = [cell.strip() for cell in row.cells]
        if not any(texts) or any(looks_numeric(text) for text in texts):
            continue
        if all(0 < len(text) <= 60 for text in texts if text):
            return index
    return None


def first_col_labels(table: _Table, limit: int = 12) -> list[str]:
    labels: list[str] = []
    for row in table.rows:
        if not row.cells:
            continue
        text = row.cells[0].strip()
        if not text or looks_numeric(text) or len(text) > 90:
            continue
        labels.append(normalize_header(text))
        if len(labels) >= limit:
            break
    return labels


def _table_score(preset: PresetConfig, signature: dict[str, Any]) -> float:
    preset_columns = preset.columns.get("count")
    column_factor = 1.0 if preset_columns is None or preset_columns == signature["cols"] else 0.3
    header_set = {normalize_header(item) for item in preset.header_keywords}
    header_set |= {normalize_header(item) for item in preset.columns.get("header", {})}
    label_set = {normalize_header(item) for item in preset.labels}
    header_score = _jaccard(header_set, set(signature["headers"]))
    label_score = _jaccard(label_set, set(signature["first_col_labels"]))
    return column_factor * (0.35 * header_score + 0.65 * label_score)


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _has_header_vocab(texts: list[str]) -> bool:
    normalized = {normalize_header(text) for text in texts}
    return any(
        vocab == text or (vocab and (vocab in text or text in vocab))
        for vocab in HEADER_VOCAB
        for text in normalized
    )


def normalize_header(value: str) -> str:
    text = normalize_text(value).lower()
    return text.strip(" :;,.")


def collapse_ws(value: str) -> str:
    return " ".join(str(value).replace("\u00a0", " ").split())


def looks_numeric(value: str) -> bool:
    return bool(re.match(r"^\s*[№#]?\s*\d+([.,]\d+)?\s*$", value or ""))


def _loader_name(document: ParsedDocument) -> str:
    kinds = {
        fragment.locator_kind.lower()
        for page in document.pages
        for fragment in page.fragments
    }
    return "+".join(sorted(kinds)) or "document"


def _loader_from_tables(tables: list[_Table]) -> str:
    if any(table.sheet for table in tables):
        return "xlsx"
    if any(table.page_no for table in tables):
        return "pdf"
    return "document"
