"""PDF table detection by word boxes and vector grid lines."""

from __future__ import annotations

import re

from app.modules.extraction.matrix_types import Cell, Table
from app.modules.extraction.matrix_utils import looks_numeric


def _same_line(a: tuple, b: tuple, factor: float = 0.4) -> bool:
    y0a, y1a = a[1], a[3]
    y0b, y1b = b[1], b[3]
    height = min(y1a - y0a, y1b - y0b)
    inter = min(y1a, y1b) - max(y0a, y0b)
    return inter > factor * height


def group_words_into_lines(words: list[tuple]) -> list[list[tuple]]:
    words = sorted(words, key=lambda word: (round(word[1], 1), word[0]))
    lines: list[list[tuple]] = []
    for word in words:
        if lines and _same_line(lines[-1][-1], word):
            lines[-1].append(word)
        else:
            lines.append([word])
    for line in lines:
        line.sort(key=lambda word: word[0])
    return [line for line in lines if line]


def _split_cells(line: list[tuple], gap_threshold: float) -> list[tuple]:
    if not line:
        return []
    cells: list[list[tuple]] = [[line[0]]]
    for word in line[1:]:
        if word[0] - cells[-1][-1][2] > gap_threshold:
            cells.append([word])
        else:
            cells[-1].append(word)
    out = []
    for group in cells:
        x0 = min(item[0] for item in group)
        y0 = min(item[1] for item in group)
        x1 = max(item[2] for item in group)
        y1 = max(item[3] for item in group)
        text = " ".join(item[4] for item in group)
        out.append((x0, y0, x1, y1, text))
    return out


def _column_separators(lines_cells: list[list[tuple]], tol: float) -> list[float]:
    separators: list[float] = []
    for line in lines_cells:
        for index in range(len(line) - 1):
            separators.append((line[index][2] + line[index + 1][0]) / 2.0)
    if not separators:
        return []
    separators.sort()
    clusters: list[list[float]] = [[separators[0]]]
    for separator in separators[1:]:
        if separator - clusters[-1][-1] <= tol:
            clusters[-1].append(separator)
        else:
            clusters.append([separator])
    return [sum(cluster) / len(cluster) for cluster in clusters]


def _assign_col(x0: float, x1: float, bounds: list[float]) -> int:
    best, best_overlap = 0, -1.0
    for index in range(len(bounds) - 1):
        lo, hi = bounds[index], bounds[index + 1]
        overlap = max(0.0, min(x1, hi) - max(x0, lo))
        if overlap > best_overlap:
            best, best_overlap = index, overlap
    if best_overlap <= 0:
        center = (x0 + x1) / 2
        best = min(
            range(len(bounds) - 1),
            key=lambda idx: abs((bounds[idx] + bounds[idx + 1]) / 2 - center),
        )
    return best


def detect_tables_on_page(
    words: list[tuple], page_number: int, min_rows: int = 3
) -> list[Table]:
    if len(words) < 6:
        return []
    heights = [word[3] - word[1] for word in words]
    median_h = sorted(heights)[len(heights) // 2] or 8.0
    gap = max(median_h * 0.55, 2.0)

    raw_lines = group_words_into_lines(words)
    lines_cells = [_split_cells(line, gap) for line in raw_lines]
    lines_cells = [line for line in lines_cells if line]
    if not lines_cells:
        return []

    separators = _column_separators(lines_cells, max(median_h * 1.2, 4.0))
    if len(separators) < 1:
        return []
    right_edge = max(cell[2] for line in lines_cells for cell in line) + 4.0
    bounds = [0.0, *separators, right_edge]
    n_cols = len(bounds) - 1

    rows: list[list[tuple | None]] = []
    for line in lines_cells:
        row: list[tuple | None] = [None] * n_cols
        for x0, y0, x1, y1, text in line:
            col = _assign_col(x0, x1, bounds)
            if not 0 <= col < len(row):
                continue
            if row[col] is None:
                row[col] = (x0, y0, x1, y1, text)
            else:
                cur = row[col]
                row[col] = (
                    min(cur[0], x0),
                    min(cur[1], y0),
                    max(cur[2], x1),
                    max(cur[3], y1),
                    cur[4] + " " + text,
                )
        rows.append(row)

    col_counts = [sum(1 for row in rows if row[j] is not None) for j in range(n_cols)]
    keep = [j for j in range(n_cols) if col_counts[j] >= 2]
    if len(keep) < 2:
        keep = sorted(range(n_cols), key=lambda j: -col_counts[j])[:2]
    rows = [[row[j] for j in keep] for row in rows]
    n_cols = len(keep)

    def nonempty_count(row) -> int:
        return sum(1 for cell in row if cell is not None)

    blocks: list[list[int]] = []
    current: list[int] = []
    for index, row in enumerate(rows):
        if nonempty_count(row) >= 2:
            current.append(index)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    tables: list[Table] = []
    for index, block in enumerate(blocks):
        if len(block) < min_rows:
            continue
        start, end = block[0], block[-1]
        while start > 0:
            row = rows[start - 1]
            if nonempty_count(row) >= 2:
                break
            text = next((cell[4] for cell in row if cell is not None), "")
            if text and len(text) <= 90 and not looks_numeric(text):
                start -= 1
            else:
                break
        while end < len(rows) - 1:
            row = rows[end + 1]
            if nonempty_count(row) >= 2:
                break
            text = next((cell[4] for cell in row if cell is not None), "")
            if text and len(text) <= 90 and not looks_numeric(text):
                end += 1
            else:
                break

        cells: list[list[Cell]] = []
        for row_index in range(start, end + 1):
            row_cells: list[Cell] = []
            for col_index in range(n_cols):
                cell = rows[row_index][col_index]
                if cell is None:
                    row_cells.append(
                        Cell(text="", row=row_index - start, col=col_index, page=page_number)
                    )
                else:
                    x0, y0, x1, y1, text = cell
                    row_cells.append(
                        Cell(
                            text=text,
                            row=row_index - start,
                            col=col_index,
                            page=page_number,
                            x0=x0,
                            y0=y0,
                            x1=x1,
                            y1=y1,
                        )
                    )
            cells.append(row_cells)

        tables.append(
            Table(
                id=f"p{page_number + 1}-t{index + 1}",
                page=page_number,
                rows=cells,
                bbox=_table_bbox(cells),
                source="word",
            )
        )
    return tables


def _cluster_coords(values, tol=0.8):
    values = sorted(values)
    groups = []
    for value in values:
        if groups and value - groups[-1][-1] <= tol:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups]


def _nearest_line(value, centers, tol=1.2):
    if not centers:
        return None
    best = min(range(len(centers)), key=lambda index: abs(centers[index] - value))
    return best if abs(centers[best] - value) <= tol else None


def _drawing_segments(rects):
    vert, hor = [], []
    for x0, y0, x1, y1 in rects:
        width = x1 - x0
        height = y1 - y0
        if width < 1.4 and height >= 3.0:
            vert.append((x0, y0, x1, y1))
        elif height < 1.4 and width >= 3.0:
            hor.append((x0, y0, x1, y1))
    return vert, hor


def _covered_fraction(a, b):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area = (ax1 - ax0) * (ay1 - ay0)
    return inter / area if area > 0 else 0.0


def detect_tables_from_grid(
    words: list[tuple], rects: list[tuple], page_number: int, min_rows: int = 2
) -> list[Table]:
    vert, hor = _drawing_segments(rects or [])
    if len(vert) < 4 or len(hor) < 4:
        return []
    vvals = [(x0 + x1) / 2 for x0, _y0, x1, _y1 in vert]
    hvals = [(y0 + y1) / 2 for _x0, y0, _x1, y1 in hor]
    vlines = _cluster_coords(vvals)
    hlines = _cluster_coords(hvals)

    def support(vals, centers):
        counts = [0] * len(centers)
        for value in vals:
            index = _nearest_line(value, centers)
            if index is not None:
                counts[index] += 1
        return counts

    vlines = [x for x, count in zip(vlines, support(vvals, vlines)) if count >= 2]
    hlines = [y for y, count in zip(hlines, support(hvals, hlines)) if count >= 2]
    if len(vlines) < 3 or len(hlines) < 2:
        return []

    band_verticals = []
    for index in range(len(hlines) - 1):
        lo, hi = hlines[index], hlines[index + 1]
        idxs = set()
        for x0, y0, x1, y1 in vert:
            if y0 <= lo + 1.0 and y1 >= hi - 1.0:
                line_index = _nearest_line((x0 + x1) / 2, vlines)
                if line_index is not None:
                    idxs.add(line_index)
        band_verticals.append((lo, hi, sorted(idxs)))

    runs = []
    current = []
    for index, (_lo, _hi, idxs) in enumerate(band_verticals):
        if len(idxs) >= 2:
            current.append(index)
        elif current:
            runs.append(current)
            current = []
    if current:
        runs.append(current)

    tables = []
    for run in runs:
        rows_info = [(index, *band_verticals[index]) for index in run]
        counts = {}
        for _index, _lo, _hi, idxs in rows_info:
            for col_index in idxs:
                counts[col_index] = counts.get(col_index, 0) + 1
        threshold = max(2, round(len(run) * 0.35))
        col_idx = sorted(
            index for index in range(len(vlines)) if counts.get(index, 0) >= threshold
        )
        if len(col_idx) < 3:
            continue

        model_rows = []
        for _index, lo, hi, idxs in rows_info:
            cells_text = {}
            for index in range(len(idxs) - 1):
                x0 = vlines[idxs[index]]
                x1 = vlines[idxs[index + 1]]
                found = []
                for word in words:
                    yc = (word[1] + word[3]) / 2
                    xc = (word[0] + word[2]) / 2
                    if lo <= yc < hi and x0 <= xc < x1:
                        found.append(word)
                if found:
                    lo_idx = max(i for i in range(len(col_idx)) if col_idx[i] <= idxs[index])
                    cells_text.setdefault(lo_idx, []).extend(found)
            row_cells = []
            for col in range(len(col_idx) - 1):
                found = cells_text.get(col, [])
                if not found:
                    row_cells.append(Cell(text="", row=len(model_rows), col=col, page=page_number))
                    continue
                found.sort(key=lambda word: (round(word[1], 1), word[0]))
                row_cells.append(
                    Cell(
                        text=" ".join(word[4] for word in found),
                        row=len(model_rows),
                        col=col,
                        page=page_number,
                        x0=min(word[0] for word in found),
                        y0=min(word[1] for word in found),
                        x1=max(word[2] for word in found),
                        y1=max(word[3] for word in found),
                    )
                )
            model_rows.append(row_cells)

        content = [
            row for row in model_rows if sum(1 for cell in row if cell.text.strip()) >= 2
        ]
        if len(content) < min_rows:
            continue
        tables.append(
            Table(
                id=f"p{page_number + 1}-t{len(tables) + 1}",
                page=page_number,
                rows=model_rows,
                bbox=_table_bbox(model_rows),
                source="grid",
            )
        )
    return tables


_GRID_MODEL_CODE_RE = re.compile(
    r"^[А-ЯЁа-яёA-Za-z0-9]{0,6}?\d{1,3}-\d{1,4}(?:-\d{1,4})?$"
)


def _is_grid_model_code(text: str) -> bool:
    value = re.sub(r"\s+", " ", text).strip()
    value = re.sub(r"[.\u2026]+$", "", value).rstrip("-–—")
    return bool(value) and re.search(r"\d", value) and bool(_GRID_MODEL_CODE_RE.match(value))


def _grid_is_transposed(table: Table) -> bool:
    for row in table.rows[:4]:
        hits = [cell for cell in row[1:] if cell.text.strip() and _is_grid_model_code(cell.text)]
        if len(hits) >= 3:
            return True
    return False


def merge_page_tables(
    page_number: int, grid_tables: list[Table], word_tables: list[Table]
) -> list[Table]:
    kept_grid = []
    for grid in grid_tables:
        duplicated = any(_covered_fraction(grid.bbox, word.bbox) > 0.5 for word in word_tables)
        if duplicated or not _grid_is_transposed(grid):
            continue
        kept_grid.append(grid)
    kept_word = []
    for word in word_tables:
        covered = any(_covered_fraction(word.bbox, grid.bbox) > 0.5 for grid in kept_grid)
        if not covered:
            kept_word.append(word)
    out = [*kept_word, *kept_grid]
    for index, table in enumerate(out):
        table.id = f"p{page_number + 1}-t{index + 1}"
    return out


def _table_bbox(rows: list[list[Cell]]) -> list[float]:
    xs = [cell.x0 for row in rows for cell in row if cell.x0 is not None] or [0.0]
    x2 = [cell.x1 for row in rows for cell in row if cell.x1 is not None] or [0.0]
    ys = [cell.y0 for row in rows for cell in row if cell.y0 is not None] or [0.0]
    y2 = [cell.y1 for row in rows for cell in row if cell.y1 is not None] or [0.0]
    return [min(xs), min(ys), max(x2), max(y2)]
