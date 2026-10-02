"""Public schemas for extraction presets and document workbench views."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from app.core.schemas import BaseSchema


class ExtractionPresetResponse(BaseSchema):
    code: str
    name: str
    description: str = ""
    doc_type: str = "passport"
    columns: dict[str, Any] = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)
    section_keywords: list[str] = Field(default_factory=list)
    header_keywords: list[str] = Field(default_factory=list)
    units: list[str] = Field(default_factory=list)
    include_all: bool = False
    keyvalue: bool = False
    fingerprint: str | None = None
    builtin: bool = False
    version: int


class ExtractionPresetRequest(BaseSchema):
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    doc_type: str = "passport"
    columns: dict[str, Any] = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)
    section_keywords: list[str] = Field(default_factory=list)
    header_keywords: list[str] = Field(default_factory=list)
    units: list[str] = Field(default_factory=list)
    include_all: bool = False
    keyvalue: bool = False
    fingerprint: str | None = None


class CertificateReprocessRequest(BaseSchema):
    preset_code: str | None = None
    table_id: str | None = None
    include_all: bool = False
    use_llm: bool = False


class PresetFromExtractionRequest(BaseSchema):
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    preset_code: str | None = None


class StructureCell(BaseSchema):
    text: str
    row: int
    col: int
    page: int | None = None
    x0: float | None = None
    y0: float | None = None
    x1: float | None = None
    y1: float | None = None


class StructureTable(BaseSchema):
    id: str
    page: int | None = None
    sheet: str | None = None
    bbox: list[float | None] = Field(default_factory=list)
    source: str | None = None
    n_rows: int
    n_cols: int
    cells: list[StructureCell]


class CertificateStructureResponse(BaseSchema):
    certificate_id: str
    document_id: str
    file_name: str
    pages: int
    auto_preset_code: str | None = None
    auto_preset_score: float | None = None
    auto_preset_method: str | None = None
    auto_table_id: str | None = None
    tables: list[StructureTable] = Field(default_factory=list)


class PageInfoResponse(BaseSchema):
    page: int
    width: int
    height: int
    pdf_width: float
    pdf_height: float
