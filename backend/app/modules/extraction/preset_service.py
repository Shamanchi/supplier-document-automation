"""Application service for managed extraction presets."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.modules.extraction.models import ExtractionPreset
from app.modules.extraction.preset_mapping import (
    PresetConfig,
    compute_exact_fingerprint,
    document_signature,
    preset_from_model,
)
from app.modules.identity.models import User


@dataclass(frozen=True)
class PresetInput:
    name: str
    description: str
    doc_type: str
    columns: dict[str, Any]
    labels: list[str]
    section_keywords: list[str]
    header_keywords: list[str]
    units: list[str]
    include_all: bool
    keyvalue: bool
    fingerprint: str | None = None


class ExtractionPresetService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(self) -> list[ExtractionPreset]:
        return list(
            (
                await self._session.scalars(
                    select(ExtractionPreset).order_by(
                        ExtractionPreset.builtin.desc(),
                        ExtractionPreset.name,
                        ExtractionPreset.code,
                    )
                )
            ).all()
        )

    async def configs(self) -> list[PresetConfig]:
        return [preset_from_model(item) for item in await self.list()]

    async def get(self, code: str) -> ExtractionPreset:
        preset = await self._session.scalar(
            select(ExtractionPreset).where(ExtractionPreset.code == code)
        )
        if preset is None:
            raise ApiError(
                "EXTRACTION_PRESET_NOT_FOUND",
                "Набор извлечения не найден",
                status_code=404,
                details={"code": code},
            )
        return preset

    async def create(self, user: User, data: PresetInput) -> ExtractionPreset:
        code = await self._available_code(_slug(data.name))
        preset = ExtractionPreset(
            id=uuid.uuid4(),
            code=code,
            name=data.name.strip(),
            description=data.description.strip(),
            doc_type=data.doc_type.strip() or "passport",
            columns=data.columns,
            labels=data.labels,
            section_keywords=data.section_keywords,
            header_keywords=data.header_keywords,
            units=data.units,
            include_all=data.include_all,
            keyvalue=data.keyvalue,
            fingerprint=data.fingerprint,
            builtin=False,
            created_by=user.id,
        )
        self._session.add(preset)
        await self._session.flush()
        return preset

    async def update(
        self, user: User, code: str, data: PresetInput, *, expected_version: int
    ) -> ExtractionPreset:
        preset = await self.get(code)
        if preset.builtin:
            raise ApiError(
                "BUILTIN_PRESET_IMMUTABLE",
                "Встроенный набор нельзя изменить",
                status_code=409,
                details={"code": code},
            )
        if preset.version != expected_version:
            raise ApiError(
                "EDIT_CONFLICT",
                "Набор изменён другим пользователем",
                status_code=409,
                details={
                    "currentVersion": preset.version,
                    "expectedVersion": expected_version,
                },
            )
        preset.name = data.name.strip()
        preset.description = data.description.strip()
        preset.doc_type = data.doc_type.strip() or "passport"
        preset.columns = data.columns
        preset.labels = data.labels
        preset.section_keywords = data.section_keywords
        preset.header_keywords = data.header_keywords
        preset.units = data.units
        preset.include_all = data.include_all
        preset.keyvalue = data.keyvalue
        preset.fingerprint = data.fingerprint
        preset.version += 1
        preset.created_by = preset.created_by or user.id
        await self._session.flush()
        return preset

    async def delete(self, code: str) -> None:
        preset = await self.get(code)
        if preset.builtin:
            raise ApiError(
                "BUILTIN_PRESET_IMMUTABLE",
                "Встроенный набор нельзя удалить",
                status_code=409,
                details={"code": code},
            )
        await self._session.delete(preset)
        await self._session.flush()

    async def snapshot(self, code: str | None) -> dict[str, Any] | None:
        if not code:
            return None
        preset = await self.get(code)
        return {
            "code": preset.code,
            "name": preset.name,
            "docType": preset.doc_type,
            "columns": preset.columns,
            "labels": preset.labels,
            "sectionKeywords": preset.section_keywords,
            "headerKeywords": preset.header_keywords,
            "units": preset.units,
            "includeAll": preset.include_all,
            "keyvalue": preset.keyvalue,
            "fingerprint": preset.fingerprint,
            "version": preset.version,
        }

    async def create_from_analysis(
        self,
        user: User,
        *,
        name: str,
        description: str,
        source_preset_code: str | None,
        parsed_document,
        labels_from_result: list[str] | None = None,
    ) -> ExtractionPreset:
        source = await self.get(source_preset_code) if source_preset_code else None
        signature = document_signature(parsed_document)
        fingerprint = compute_exact_fingerprint(signature)
        labels = _unique([*(labels_from_result or []), *((source.labels if source else []))])
        if source is not None:
            data = PresetInput(
                name=name,
                description=description or f"Создан из набора {source.name}",
                doc_type=source.doc_type,
                columns=source.columns,
                labels=labels,
                section_keywords=source.section_keywords,
                header_keywords=source.header_keywords,
                units=source.units,
                include_all=source.include_all,
                keyvalue=source.keyvalue,
                fingerprint=fingerprint,
            )
        else:
            data = PresetInput(
                name=name,
                description=description,
                doc_type="passport",
                columns={
                    "count": 2,
                    "has_header": False,
                    "roles": {"label": 0, "value": 1},
                },
                labels=labels,
                section_keywords=[],
                header_keywords=[],
                units=[],
                include_all=True,
                keyvalue=False,
                fingerprint=fingerprint,
            )
        return await self.create(user, data)

    async def _available_code(self, base: str) -> str:
        code = base
        suffix = 2
        while await self._session.scalar(
            select(ExtractionPreset.id).where(ExtractionPreset.code == code)
        ):
            code = f"{base}-{suffix}"
            suffix += 1
        return code


def preset_input_from_api(payload) -> PresetInput:
    return PresetInput(
        name=payload.name.strip(),
        description=payload.description.strip(),
        doc_type=payload.doc_type.strip() or "passport",
        columns=dict(payload.columns or {}),
        labels=[item.strip() for item in payload.labels if item.strip()],
        section_keywords=[
            item.strip() for item in payload.section_keywords if item.strip()
        ],
        header_keywords=[item.strip() for item in payload.header_keywords if item.strip()],
        units=[item.strip() for item in payload.units if item.strip()],
        include_all=payload.include_all,
        keyvalue=payload.keyvalue,
        fingerprint=payload.fingerprint,
    )


def preset_config_from_snapshot(snapshot: dict[str, Any]) -> PresetConfig:
    return PresetConfig(
        code=snapshot["code"],
        name=snapshot["name"],
        doc_type=snapshot.get("docType") or "passport",
        columns=dict(snapshot.get("columns") or {}),
        labels=list(snapshot.get("labels") or []),
        section_keywords=list(snapshot.get("sectionKeywords") or []),
        header_keywords=list(snapshot.get("headerKeywords") or []),
        units=list(snapshot.get("units") or []),
        include_all=bool(snapshot.get("includeAll")),
        keyvalue=bool(snapshot.get("keyvalue")),
        fingerprint=snapshot.get("fingerprint"),
    )


def _slug(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9а-яё\-_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:60] or "preset"


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result
