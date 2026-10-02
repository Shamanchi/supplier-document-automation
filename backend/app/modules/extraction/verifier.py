"""Hard gate that rebinds extracted values to persisted source text."""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.extraction.parsers import normalize_text

VERIFIER_VERSION = "substring-v1"


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    method: str
    rejection_reason: str | None
    quote_start: int | None
    quote_end: int | None
    value_start: int | None
    value_end: int | None


def verify_evidence(
    *, source_text: str, quote: str, value_text: str, unit_text: str | None
) -> VerificationResult:
    source = normalize_text(source_text)
    normalized_quote = normalize_text(quote)
    value = normalize_text(value_text)
    unit = normalize_text(unit_text) if unit_text else None

    if not normalized_quote or not value:
        return _rejected("EMPTY_QUOTE_OR_VALUE")

    quote_start = source.find(normalized_quote)
    if quote_start < 0:
        return _rejected("QUOTE_NOT_FOUND_IN_FRAGMENT")
    quote_end = quote_start + len(normalized_quote)

    relative_value_start = normalized_quote.find(value)
    if relative_value_start < 0:
        return _rejected("VALUE_NOT_FOUND_IN_QUOTE")
    value_start = quote_start + relative_value_start
    value_end = value_start + len(value)

    if unit and normalized_quote.find(unit) < 0:
        return _rejected("UNIT_NOT_FOUND_IN_QUOTE")

    return VerificationResult(
        passed=True,
        method="EXACT_SUBSTRING",
        rejection_reason=None,
        quote_start=quote_start,
        quote_end=quote_end,
        value_start=value_start,
        value_end=value_end,
    )


def _rejected(reason: str) -> VerificationResult:
    return VerificationResult(
        passed=False,
        method="EXACT_SUBSTRING",
        rejection_reason=reason,
        quote_start=None,
        quote_end=None,
        value_start=None,
        value_end=None,
    )
