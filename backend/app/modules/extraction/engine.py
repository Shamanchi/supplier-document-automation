"""Ядро извлечения, не знающее о владельце документа.

Один и тот же документ обрабатывается одинаково независимо от того, кто его
принёс: сертификат из библиотеки или FACT-документ позиции заявки. Здесь живёт
общая часть — разбор, OCR, LLM-догрузка, проверка evidence и запись
страниц/фрагментов/сырых параметров. Привязка к владельцу (статусы сертификата,
этапы pipeline, лизинг прогона) остаётся в сервисах, которые это ядро вызывают.

Расчётная часть (`DocumentAnalyzer.analyze`) не трогает базу вовсе, а запись
(`persist_analysis`) не трогает агрегат владельца — благодаря этому оба контура
переиспользуют движок без общего состояния.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.modules.documents.signatures import PDF_MEDIA_TYPE
from app.modules.extraction.fallbacks import (
    LlmStats,
    OcrStats,
    apply_llm_fallback,
    apply_ocr_fallback,
)
from app.modules.extraction.models import (
    DocumentPage,
    DocumentTable,
    ExtractionEvidence,
    ExtractionRun,
    RawParameter,
    SourceFragment,
    SourceVerification,
)
from app.modules.extraction.parsers import (
    ParameterCandidate,
    ParsedDocument,
    extract_parameters,
    normalize_text,
    parse_document,
)
from app.modules.extraction.preset_mapping import (
    PresetConfig,
    extract_with_presets,
)
from app.modules.extraction.providers import (
    LlmExtractionProvider,
    OcrProvider,
    OpenAiCompatibleLlmProvider,
    PdfPageRenderer,
    PopplerPdfRenderer,
    ProviderContractError,
    TesseractOcrProvider,
)
from app.modules.extraction.verifier import (
    VERIFIER_VERSION,
    VerificationResult,
    verify_evidence,
)
from app.modules.normalization.service import NormalizationStats, ParameterNormalizer

#: Прогресс между шагами разбора: владелец сам решает, куда его записать.
ProgressHook = Callable[[str, int], Awaitable[None]]


@dataclass(frozen=True)
class DocumentAnalysis:
    """Результат разбора документа до записи в базу."""

    document: ParsedDocument
    candidates: list[ParameterCandidate]
    ocr_stats: OcrStats
    llm_stats: LlmStats
    llm_error: str | None
    preset_code: str | None = None
    preset_name: str | None = None
    preset_match_score: float | None = None
    preset_match_method: str | None = None
    selected_table_id: str | None = None
    preset_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PersistedAnalysis:
    """Что осело в базе после записи разбора.

    `parameters` — подтверждённые верификатором и прошедшие порог уверенности
    сырые параметры. Нормализация вынесена наружу: в pipeline это отдельный этап
    после hard gate `VERIFYING_SOURCES`, и объединять их здесь нельзя.
    """

    pages: int
    fragments: int
    candidates: int
    parameters: list[RawParameter]

    @property
    def verified(self) -> int:
        return len(self.parameters)


class DocumentAnalyzer:
    """Разбор документа: native → OCR → LLM → кандидаты в параметры."""

    def __init__(
        self,
        settings: Settings,
        *,
        renderer: PdfPageRenderer | None = None,
        ocr_provider: OcrProvider | None = None,
        llm_provider: LlmExtractionProvider | None = None,
    ) -> None:
        self._settings = settings
        self._renderer = renderer or PopplerPdfRenderer()
        self._ocr_provider = ocr_provider or TesseractOcrProvider()
        self._llm_provider = llm_provider or configured_llm_provider(settings)

    async def analyze(
        self,
        data: bytes,
        media_type: str,
        *,
        correlation_id: str,
        on_progress: ProgressHook | None = None,
        presets: list[PresetConfig] | None = None,
        preset_code: str | None = None,
        table_id: str | None = None,
        include_all: bool = False,
        use_llm: bool | None = None,
    ) -> DocumentAnalysis:
        """Разбирает документ, не обращаясь к базе.

        Исключения провайдеров и парсера не подавляются: как их трактовать —
        решает владелец, у сертификата и позиции заявки разные последствия.
        """

        async def report(stage: str, percent: int) -> None:
            if on_progress is not None:
                await on_progress(stage, percent)

        document = parse_document(data, media_type)
        await report("NATIVE_PARSED", 30)

        ocr_stats = OcrStats()
        if (
            media_type == PDF_MEDIA_TYPE
            and self._settings.ocr_enabled
            and any(page.ocr_needed for page in document.pages)
        ):
            document, ocr_stats = await apply_ocr_fallback(
                data,
                document,
                renderer=self._renderer,
                provider=self._ocr_provider,
                dpi=self._settings.ocr_dpi,
                language=self._settings.ocr_language,
                timeout_seconds=self._settings.ocr_timeout_seconds,
                min_confidence=self._settings.ocr_min_confidence,
            )
        await report("OCR_COMPLETED", 60)

        preset_result = extract_with_presets(
            document,
            presets or [],
            preset_code=preset_code,
            table_id=table_id,
            include_all=include_all,
        )
        candidates = preset_result.candidates or extract_parameters(document)
        llm_stats = LlmStats()
        llm_error: str | None = None
        llm_requested = (
            self._settings.llm_extraction_enabled if use_llm is None else use_llm
        )
        if llm_requested:
            if self._llm_provider is None:
                raise ProviderContractError("LLM_PROVIDER_NOT_CONFIGURED")
            try:
                candidates, llm_stats = await apply_llm_fallback(
                    document,
                    candidates,
                    provider=self._llm_provider,
                    correlation_id=correlation_id,
                    max_fragments=self._settings.llm_extraction_max_fragments,
                    guidance=_llm_guidance(preset_result.preset),
                    preferred_table_id=preset_result.table_id,
                )
            except ProviderContractError as exc:
                # Невалидный ответ модели отбрасывается целиком: детерминированное
                # извлечение остаётся пригодным, выдуманные параметры не пишутся.
                llm_error = str(exc)
        await report("EXTRACTION_COMPLETED", 75)

        return DocumentAnalysis(
            document=document,
            candidates=candidates,
            ocr_stats=ocr_stats,
            llm_stats=llm_stats,
            llm_error=llm_error,
            preset_code=preset_result.preset.code if preset_result.preset else None,
            preset_name=preset_result.preset.name if preset_result.preset else None,
            preset_match_score=preset_result.score,
            preset_match_method=preset_result.method,
            selected_table_id=preset_result.table_id,
            preset_warnings=tuple(preset_result.warnings),
        )


async def persist_analysis(
    session: AsyncSession,
    *,
    document_version_id: uuid.UUID,
    run: ExtractionRun,
    analysis: DocumentAnalysis,
    min_confidence: float,
    check_item_id: uuid.UUID | None = None,
    role: str = "FACT",
) -> PersistedAnalysis:
    """Записывает страницы, фрагменты, сырые параметры и их evidence.

    `role` различает документ требований и документ факта: дальше по конвейеру
    сравниваются именно роли, и потерять её здесь — значит сравнить ТЗ с ТЗ.

    Повторный прогон по той же версии документа не дублирует страницы и
    фрагменты: они принадлежат неизменяемой версии, а не прогону. Сырые
    параметры, наоборот, принадлежат прогону — у каждой попытки свои.
    """
    existing_pages = {
        page.page_no: page
        for page in (
            await session.scalars(
                select(DocumentPage).where(
                    DocumentPage.document_version_id == document_version_id
                )
            )
        ).all()
    }
    existing_fragments = (
        await session.scalars(
            select(SourceFragment).where(
                SourceFragment.document_version_id == document_version_id,
            )
        )
    ).all()
    fragment_index_by_identity = {
        _fragment_identity(
            fragment.page_id,
            fragment.locator_kind,
            fragment.locator,
            fragment.text_hash,
        ): fragment
        for fragment in existing_fragments
    }
    table_fragment_ids = set(
        (
            await session.scalars(
                select(DocumentTable.source_fragment_id).where(
                    DocumentTable.source_fragment_id.in_(
                        [fragment.id for fragment in existing_fragments]
                    )
                )
            )
        ).all()
    )

    fragments: dict[tuple[int, int], SourceFragment] = {}
    fragment_count = 0
    for page_index, parsed_page in enumerate(analysis.document.pages):
        page = existing_pages.get(parsed_page.page_no)
        if page is None:
            page_text = "\n".join(item.text for item in parsed_page.fragments)
            page = DocumentPage(
                id=uuid.uuid4(),
                document_version_id=document_version_id,
                page_no=parsed_page.page_no,
                text_hash=_sha256(page_text),
                dpi=parsed_page.dpi,
                width_px=parsed_page.width_px,
                height_px=parsed_page.height_px,
                document_type=parsed_page.document_type,
                ocr_needed=parsed_page.ocr_needed,
                quality_score=Decimal(str(round(parsed_page.quality_score, 4))),
                active_layer=parsed_page.active_layer,
                parser_version=parsed_page.parser_version,
            )
            session.add(page)
            await session.flush()
            existing_pages[page.page_no] = page

        for fragment_index, parsed_fragment in enumerate(parsed_page.fragments):
            normalized = normalize_text(parsed_fragment.text)
            text_hash = _sha256(normalized)
            identity = _fragment_identity(
                page.id,
                parsed_fragment.locator_kind,
                parsed_fragment.locator,
                text_hash,
            )
            fragment = fragment_index_by_identity.get(identity)
            if fragment is None:
                fragment = SourceFragment(
                    id=uuid.uuid4(),
                    document_version_id=document_version_id,
                    page_id=page.id,
                    method=parsed_fragment.method,
                    fragment_raw_text=parsed_fragment.text,
                    normalized_text=normalized,
                    text_hash=text_hash,
                    locator_kind=parsed_fragment.locator_kind,
                    locator=parsed_fragment.locator,
                    parser_version=parsed_fragment.parser_version,
                )
                session.add(fragment)
                await session.flush()
                fragment_index_by_identity[identity] = fragment

            fragments[(page_index, fragment_index)] = fragment
            fragment_count += 1
            if (
                parsed_fragment.cells is not None
                and fragment.id not in table_fragment_ids
            ):
                structure: dict[str, object] = {
                    "rows": [list(parsed_fragment.cells)],
                }
                if parsed_fragment.cell_boxes is not None:
                    structure["cells"] = list(parsed_fragment.cell_boxes)
                if parsed_fragment.table_bbox is not None:
                    structure["bbox"] = list(parsed_fragment.table_bbox)
                if parsed_fragment.table_source is not None:
                    structure["source"] = parsed_fragment.table_source
                session.add(
                    DocumentTable(
                        source_fragment_id=fragment.id,
                        structure=structure,
                    )
                )
                table_fragment_ids.add(fragment.id)

    verified: list[RawParameter] = []
    for candidate in analysis.candidates:
        fragment = fragments[candidate.fragment_index]
        verification = verify_evidence(
            source_text=fragment.normalized_text or fragment.fragment_raw_text,
            quote=candidate.quote,
            value_text=candidate.value,
            unit_text=candidate.unit,
        )
        parameter, accepted = await save_candidate(
            session,
            run=run,
            fragment=fragment,
            candidate=candidate,
            verification=verification,
            min_confidence=min_confidence,
            check_item_id=check_item_id,
            role=role,
        )
        if accepted:
            verified.append(parameter)

    return PersistedAnalysis(
        pages=len(analysis.document.pages),
        fragments=fragment_count,
        candidates=len(analysis.candidates),
        parameters=verified,
    )


async def normalize_parameters(
    session: AsyncSession,
    parameters: list[RawParameter],
    *,
    catalog_version_id: uuid.UUID | None = None,
) -> NormalizationStats:
    """Нормализует подтверждённые параметры по каталогу прогона."""
    return await ParameterNormalizer(session).normalize(
        parameters, catalog_version_id=catalog_version_id
    )


async def save_candidate(
    session: AsyncSession,
    *,
    run: ExtractionRun,
    fragment: SourceFragment,
    candidate: ParameterCandidate,
    verification: VerificationResult,
    min_confidence: float,
    check_item_id: uuid.UUID | None = None,
    role: str = "FACT",
) -> tuple[RawParameter, bool]:
    """Сохраняет кандидата вместе с evidence и вердиктом верификатора.

    Неподтверждённое или неуверенное значение всё равно сохраняется, но с
    `excluded=true`: оно нужно ручному разбору, а наружу не отдаётся.
    """
    accepted = verification.passed and candidate.confidence >= min_confidence
    quality_flags: list[str] = []
    if not verification.passed:
        quality_flags.append("UNVERIFIED_EVIDENCE")
    if candidate.confidence < min_confidence:
        quality_flags.append("LOW_EXTRACTION_CONFIDENCE")
    parameter = RawParameter(
        id=uuid.uuid4(),
        check_item_id=check_item_id,
        extraction_run_id=run.id,
        role=role,
        name_text=candidate.name,
        value_text=candidate.value,
        unit_text=candidate.unit,
        extraction_method=candidate.method,
        confidence=Decimal(str(candidate.confidence)),
        quality_flags=quality_flags,
        excluded=not accepted,
        version=1,
    )
    session.add(parameter)
    await session.flush()
    evidence = ExtractionEvidence(
        id=uuid.uuid4(),
        raw_parameter_id=parameter.id,
        source_fragment_id=fragment.id,
        quote=candidate.quote,
        quote_start=verification.quote_start,
        quote_end=verification.quote_end,
        value_start=verification.value_start,
        value_end=verification.value_end,
        verified=verification.passed,
    )
    session.add(evidence)
    await session.flush()
    session.add(
        SourceVerification(
            extraction_evidence_id=evidence.id,
            passed=verification.passed,
            method=verification.method,
            rejection_reason=verification.rejection_reason,
            verifier_version=VERIFIER_VERSION,
        )
    )
    return parameter, accepted


def configured_llm_provider(settings: Settings) -> LlmExtractionProvider | None:
    """Внешний LLM подключается только при полной конфигурации."""
    if (
        not settings.llm_extraction_enabled
        or not settings.llm_extraction_endpoint
        or not settings.llm_extraction_api_key
    ):
        return None
    return OpenAiCompatibleLlmProvider(
        endpoint=settings.llm_extraction_endpoint,
        api_key=settings.llm_extraction_api_key,
        model=settings.llm_extraction_model,
        prompt_version=settings.llm_extraction_prompt_version,
        timeout_seconds=settings.llm_extraction_timeout_seconds,
    )


def _llm_guidance(preset: PresetConfig | None) -> str | None:
    if preset is None:
        return None
    labels = [label.strip() for label in preset.labels if str(label).strip()]
    if not labels:
        return None
    joined = ", ".join(labels[:80])
    return (
        "Prioritize these technical characteristic labels if they appear in the "
        f"fragments, but do not invent absent values: {joined}."
    )


def _fragment_identity(
    page_id: uuid.UUID | None,
    locator_kind: str,
    locator: dict,
    text_hash: str,
) -> tuple[uuid.UUID | None, str, str, str]:
    return (
        page_id,
        locator_kind,
        json.dumps(locator, ensure_ascii=False, sort_keys=True),
        text_hash,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
