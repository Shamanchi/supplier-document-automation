"""Small text helpers shared by table detection and matrix extraction."""

from __future__ import annotations

import re
import unicodedata


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    return " ".join(normalized.replace("\u00a0", " ").split())


def collapse_ws(value: str) -> str:
    return " ".join(str(value).replace("\u00a0", " ").split())


def normalize_header(value: str) -> str:
    text = normalize_text(value).lower()
    return text.strip(" :;,.")


def looks_numeric(value: str) -> bool:
    return bool(re.match(r"^\s*[№#]?\s*\d+([.,]\d+)?\s*$", value or ""))
