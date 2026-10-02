"""Internal table/value types used by pump matrix extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Cell:
    text: str
    row: int
    col: int
    page: int | None = None
    x0: float | None = None
    y0: float | None = None
    x1: float | None = None
    y1: float | None = None

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "row": self.row,
            "col": self.col,
            "page": self.page,
            "x0": self.x0,
            "y0": self.y0,
            "x1": self.x1,
            "y1": self.y1,
        }


@dataclass
class Table:
    id: str
    rows: list[list[Cell]]
    sheet: str | None = None
    page: int | None = None
    header_row: int | None = None
    bbox: list[float | None] = field(default_factory=list)
    source: str | None = None

    @property
    def n_cols(self) -> int:
        return max((len(row) for row in self.rows), default=0)


@dataclass(frozen=True)
class Evidence:
    document: str
    page: int | None = None
    quote: str = ""
    table: str | None = None
    row: int | None = None
    fragment: str | None = None


@dataclass(frozen=True)
class Parameter:
    name: str
    value: str
    unit: str | None = None
    evidence: Evidence | None = None
    source: Literal["preset", "llm"] = "preset"
    verified: bool = True
