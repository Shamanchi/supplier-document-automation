"""Public schemas for the certificate registry."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Literal

from pydantic import Field

from app.core.schemas import BaseSchema


class CertificateEvidence(BaseSchema):
    fragment_id: uuid.UUID
    document: str
    page: int | None = None
    quote: str
    verified: bool
    locator: dict[str, Any] | None = None


class CertificateParameter(BaseSchema):
    name: str
    value: Any
    evidence: CertificateEvidence


class CertificateResponse(BaseSchema):
    id: uuid.UUID
    supplier: str
    product: str
    number: str
    status: Literal["ACTIVE", "EXPIRED"]
    valid_until: date | None = None
    document_id: uuid.UUID
    processing_status: Literal["UPLOADED", "PROCESSING", "READY", "FAILED"]
    processing_error: str | None = None
    processing_error_code: str | None = None
    processing_error_details: dict[str, Any] | None = None
    processing_progress: int = Field(ge=0, le=100)
    processing_stage: str | None = None
    parameters: list[CertificateParameter] = Field(default_factory=list)
    version: int


class CertificateReprocessAccepted(BaseSchema):
    certificate_id: uuid.UUID
    extraction_run_id: uuid.UUID
    processing_status: Literal["UPLOADED"]
    version: int
