"""OCR cascade and LLM gap-filling orchestration."""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.extraction.parsers import (
    MAX_FRAGMENT_CHARS,
    ParameterCandidate,
    ParsedDocument,
    ParsedFragment,
    ParsedPage,
    normalize_text,
)
from app.modules.extraction.providers import (
    LlmExtractionProvider,
    LlmFragment,
    OcrOptions,
    OcrProvider,
    PdfPageRenderer,
)


@dataclass(frozen=True)
class OcrStats:
    attempted_pages: int = 0
    recognized_pages: int = 0
    provider: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class LlmStats:
    supplied_fragments: int = 0
    candidates: int = 0
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None


async def apply_ocr_fallback(
    pdf: bytes,
    document: ParsedDocument,
    *,
    renderer: PdfPageRenderer,
    provider: OcrProvider,
    dpi: int,
    language: str,
    timeout_seconds: int,
    min_confidence: float,
) -> tuple[ParsedDocument, OcrStats]:
    pages: list[ParsedPage] = []
    attempted = 0
    recognized = 0
    provider_name: str | None = None
    model: str | None = None
    for page in document.pages:
        if not page.ocr_needed:
            pages.append(page)
            continue
        attempted += 1
        image = await renderer.render(pdf, page.page_no, dpi=dpi)
        result = await provider.recognize(
            image,
            OcrOptions(language=language, timeout_seconds=timeout_seconds),
        )
        provider_name = result.provider
        model = result.model
        ocr_fragments = tuple(
            ParsedFragment(
                text=normalize_text(line.text)[:MAX_FRAGMENT_CHARS],
                method="OCR",
                locator_kind="PDF",
                locator={
                    "page": page.page_no,
                    "line": index,
                    "bbox": line.bbox,
                },
                parser_version=result.model,
                confidence=line.confidence,
            )
            for index, line in enumerate(result.lines, start=1)
            if normalize_text(line.text) and line.confidence >= min_confidence
        )
        if not ocr_fragments:
            pages.append(page)
            continue
        recognized += 1
        quality = sum(
            fragment.confidence or 0 for fragment in ocr_fragments
        ) / len(ocr_fragments)
        pages.append(
            ParsedPage(
                page_no=page.page_no,
                fragments=page.fragments + ocr_fragments,
                quality_score=quality,
                ocr_needed=False,
                document_type="MIXED" if page.fragments else "SCANNED",
                active_layer="OCR",
                dpi=dpi,
                width_px=result.width_px,
                height_px=result.height_px,
                parser_version=result.model,
            )
        )
    return (
        ParsedDocument(tuple(pages)),
        OcrStats(attempted, recognized, provider_name, model),
    )


async def apply_llm_fallback(
    document: ParsedDocument,
    deterministic: list[ParameterCandidate],
    *,
    provider: LlmExtractionProvider,
    correlation_id: str,
    max_fragments: int,
    guidance: str | None = None,
    preferred_table_id: str | None = None,
) -> tuple[list[ParameterCandidate], LlmStats]:
    handled = {candidate.fragment_index for candidate in deterministic}
    mapping: dict[str, tuple[int, int]] = {}
    supplied: list[LlmFragment] = []
    for page_index, page in enumerate(document.pages):
        for fragment_index, fragment in enumerate(page.fragments):
            index = (page_index, fragment_index)
            if index in handled:
                continue
            if page.active_layer == "OCR" and fragment.method != "OCR":
                continue
            if len(fragment.text.strip()) < 4:
                continue
            fragment_id = _llm_fragment_id(page_index, fragment_index, fragment.locator)
            mapping[fragment_id] = index
            supplied.append(LlmFragment(fragment_id, fragment.text))
            if len(supplied) >= max_fragments:
                break
        if len(supplied) >= max_fragments:
            break

    if preferred_table_id:
        supplied.sort(
            key=lambda fragment: (
                0 if fragment.fragment_id.startswith(f"{preferred_table_id}:") else 1,
                fragment.fragment_id,
            )
        )

    if not supplied:
        return deterministic, LlmStats()
    try:
        extracted = await provider.extract(
            supplied,
            correlation_id=correlation_id,
            guidance=guidance,
        )
    except TypeError:
        # Older test fakes implement the previous port without guidance.
        extracted = await provider.extract(supplied, correlation_id=correlation_id)
    allowed = {fragment.fragment_id: fragment.text for fragment in supplied}
    merged = list(deterministic)
    seen = {
        (
            candidate.name.casefold(),
            candidate.value.casefold(),
            candidate.unit.casefold() if candidate.unit else None,
            candidate.fragment_index,
        )
        for candidate in deterministic
    }
    accepted = 0
    for candidate in extracted:
        if candidate.fragment_id not in mapping:
            continue
        source = allowed[candidate.fragment_id]
        if candidate.quote not in source:
            continue
        index = mapping[candidate.fragment_id]
        key = (
            candidate.name.casefold(),
            candidate.value.casefold(),
            candidate.unit.casefold() if candidate.unit else None,
            index,
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(
            ParameterCandidate(
                fragment_index=index,
                name=candidate.name,
                value=candidate.value,
                unit=candidate.unit,
                quote=candidate.quote,
                method="LLM",
                confidence=candidate.confidence,
            )
        )
        accepted += 1
    return (
        merged,
        LlmStats(
            supplied_fragments=len(supplied),
            candidates=accepted,
            provider=provider.provider,
            model=provider.model,
            prompt_version=provider.prompt_version,
        ),
    )


def _llm_fragment_id(page_index: int, fragment_index: int, locator: dict) -> str:
    if "table" in locator:
        return f"table:{locator['table']}:{page_index}:{fragment_index}"
    if "sheet" in locator:
        return f"sheet:{locator['sheet']}:{page_index}:{fragment_index}"
    return f"{page_index}:{fragment_index}"
