"""Schemas for parameter-level HITL operations."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.core.schemas import BaseSchema


class ManualParameterCreate(BaseSchema):
    role: Literal["REQUIREMENT", "FACT"]
    name: str = Field(min_length=1, max_length=500)
    value_text: str | None = Field(default=None, max_length=1000)
    unit_text: str | None = Field(default=None, max_length=100)
    reason_code: str = Field(min_length=1, max_length=100)
    comment: str = Field(min_length=1, max_length=1000)


class ParameterRevisionRequest(BaseSchema):
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=500)
    value_text: str | None = Field(default=None, max_length=1000)
    unit_text: str | None = Field(default=None, max_length=100)
    reason_code: str = Field(min_length=1, max_length=100)
    comment: str = Field(min_length=1, max_length=1000)


class ParameterStateRequest(BaseSchema):
    expected_version: int = Field(ge=1)
    reason_code: str = Field(min_length=1, max_length=100)
    comment: str = Field(min_length=1, max_length=1000)
