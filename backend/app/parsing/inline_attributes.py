"""Closed inline-attribute extractor DSL; arbitrary regex is not supported."""
from __future__ import annotations

import re
from dataclasses import dataclass


INLINE_EXTRACTOR_KINDS = frozenset(
    {"number_with_unit", "ip_code", "dimension_triplet", "standard_code", "fire_class"}
)


@dataclass(frozen=True)
class InlineExtraction:
    raw_value: str


_UNIT_ALIASES: dict[str, tuple[str, ...]] = {
    "V": ("В", "V"),
    "kV": ("кВ", "kV"),
    "W": ("Вт", "W"),
    "kW": ("кВт", "kW"),
    "mm": ("мм", "mm"),
    "cm": ("см", "cm"),
    "m": ("м", "m"),
    "kg": ("кг", "kg"),
    "g": ("г", "g"),
    "MPa": ("МПа", "MPa"),
    "bar": ("бар", "bar"),
    "°C": ("°C", "°С", "C", "С"),
}

_CANONICAL_UNIT_ALIASES = {
    "в": "V",
    "кв": "kV",
    "вт": "W",
    "квт": "kW",
    "мм": "mm",
    "см": "cm",
    "м": "m",
    "кг": "kg",
    "г": "g",
    "мпа": "MPa",
    "бар": "bar",
}


def canonicalize_inline_unit(value: str | None) -> str | None:
    """Normalize model-facing RU/EN spellings to the closed DSL unit key."""

    if value is None:
        return None
    stripped = " ".join(value.split())
    if stripped in _UNIT_ALIASES:
        return stripped
    return _CANONICAL_UNIT_ALIASES.get(stripped.casefold(), stripped)


def extract_inline_values(
    text: str, *, extractor_kind: str, canonical_unit: str | None
) -> list[InlineExtraction]:
    if extractor_kind not in INLINE_EXTRACTOR_KINDS:
        return []
    if extractor_kind == "ip_code":
        return [
            InlineExtraction(match.group(0).upper().replace(" ", ""))
            for match in re.finditer(r"(?i)\bIP\s*[0-6X][0-9X]\b", text)
        ]
    if extractor_kind == "number_with_unit":
        aliases = _UNIT_ALIASES.get(canonicalize_inline_unit(canonical_unit) or "", ())
        if not aliases:
            return []
        unit_pattern = "|".join(re.escape(value) for value in aliases)
        pattern = re.compile(
            rf"(?<![\w.,])([+-]?\d+(?:[.,]\d+)?)\s*(?:{unit_pattern})(?!\w)",
            re.IGNORECASE,
        )
        return [InlineExtraction(match.group(1).replace(",", ".")) for match in pattern.finditer(text)]
    if extractor_kind == "dimension_triplet":
        pattern = re.compile(r"(?<!\d)(\d+(?:[.,]\d+)?\s*[xх×]\s*\d+(?:[.,]\d+)?(?:\s*[xх×]\s*\d+(?:[.,]\d+)?)?)(?!\d)", re.IGNORECASE)
        return [InlineExtraction(" ".join(match.group(1).split())) for match in pattern.finditer(text)]
    if extractor_kind == "standard_code":
        pattern = re.compile(r"(?i)\b(?:ГОСТ|GOST|DIN|EN|ISO)\s*[A-ZА-Я0-9][A-ZА-Я0-9.\-/ ]{1,30}")
        return [InlineExtraction(" ".join(match.group(0).split())) for match in pattern.finditer(text)]
    pattern = re.compile(r"(?i)\b(?:класс\s+)?(?:A|B|C|D|E|F)(?:\s*[-–]\s*\d+)?\b")
    return [InlineExtraction(" ".join(match.group(0).split())) for match in pattern.finditer(text)]
