"""Извлечение полей из распознанного текста документа.

Задача первого прохода узкая и намеренно такая: достать регистрационный номер
сертификата. Если реестр доступен, остальные поля приходят из него — чище и с
актуальным статусом, — поэтому нет смысла вытягивать их регулярками из
неидеального OCR-текста.

Прочие поля извлекаются как вспомогательные: они нужны, чтобы показать
расхождение бумаги с реестром, а не чтобы заменить реестр.
"""

from __future__ import annotations

import hashlib
import re

from .fsa.normalize import (
    expand_ranges,
    find_numbers,
    normalize_designation,
    parse_date,
)
from .fsa.schema import DocumentClass, ExtractedDocument, Field, TextSource

_VALIDITY_PATTERN = re.compile(
    r"СРОК\s+ДЕЙСТВИЯ\s*С?\s*(?P<from>\d{2}\.\d{2}\.\d{4})"
    r".{0,40}?ПО\s*(?P<until>\d{2}\.\d{2}\.\d{4})",
    re.IGNORECASE | re.DOTALL,
)
_PRODUCTS_PATTERN = re.compile(
    r"ПРОДУКЦИЯ\s*(?P<body>.+?)(?=КОД\s+ТН\s?ВЭД|СООТВЕТСТВУЕТ\s+ТРЕБОВАНИЯМ|$)",
    re.IGNORECASE | re.DOTALL,
)
_ANNEX_PATTERN = re.compile(
    r"(?:см\.?\s*)?приложени\w*\s*№?\s*(?P<number>[0-9]{4,})", re.IGNORECASE
)
_TU_PATTERN = re.compile(r"ТУ\s?[\d][\d.\-]{6,}", re.IGNORECASE)
_TR_PATTERN = re.compile(r"ТР\s+(?:ЕАЭС|ТС)\s*\d{3}/\d{4}", re.IGNORECASE)
_BLANK_PATTERN = re.compile(r"Серия\s+RU\s*№?\s*(?P<number>\d{6,8})", re.IGNORECASE)
_DESIGNATION_PATTERN = re.compile(
    r"\b[А-ЯA-Z]{2,8}(?:\([^)]{1,12}\))?-[\dА-ЯA-Z][\dА-ЯA-Z,.()\-]{0,24}\b"
)
_MASS_PATTERN = re.compile(
    r"Масс[аы][^\n]{0,80}?кг[^\n]{0,20}?(?P<value>\d+[,.]?\d*)", re.IGNORECASE
)

_CERTIFICATE_MARKERS = ("СЕРТИФИКАТ СООТВЕТСТВИЯ", "ЕВРАЗИЙСКИЙ ЭКОНОМИЧЕСКИЙ СОЮЗ")
_DECLARATION_MARKERS = ("ДЕКЛАРАЦИЯ О СООТВЕТСТВИИ",)
_EXTRACT_MARKERS = ("РЕЕСТР СЕРТИФИКАТОВ СООТВЕТСТВИЯ", "ВЫПИСКА ОТ")
_PASSPORT_MARKERS = ("ПАСПОРТ", "РУКОВОДСТВО ПО ЭКСПЛУАТАЦИИ")


def document_id(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def classify(text: str) -> DocumentClass:
    upper = (text or "").upper()
    if any(marker in upper for marker in _EXTRACT_MARKERS):
        return DocumentClass.REGISTRY_EXTRACT
    if any(marker in upper for marker in _DECLARATION_MARKERS):
        return DocumentClass.DECLARATION
    if any(marker in upper for marker in _CERTIFICATE_MARKERS):
        return DocumentClass.CERTIFICATE
    if any(marker in upper for marker in _PASSPORT_MARKERS):
        return DocumentClass.PASSPORT
    return DocumentClass.OTHER


def extract(
    text: str,
    *,
    doc_id: str,
    text_source: TextSource,
    engine: str = "",
    page_count: int = 0,
    mean_confidence: float | None = None,
    source_path: str = "",
) -> ExtractedDocument:
    doc_class = classify(text)
    confidence = 1.0 if text_source is TextSource.TEXT_LAYER else (
        mean_confidence if mean_confidence is not None else 0.5
    )
    warnings: list[str] = []

    numbers = find_numbers(text)
    number_field: Field[str] = Field(None)
    certificates_field: Field[list[str]] = Field(None)
    if numbers:
        if doc_class is DocumentClass.PASSPORT:
            # В паспорте номер сертификата — ссылка на смежный документ,
            # а не идентификатор самого паспорта.
            certificates_field = Field(numbers, confidence=confidence)
        else:
            number_field = Field(numbers[0], confidence=confidence)
            if len(numbers) > 1:
                certificates_field = Field(numbers, confidence=confidence)
                warnings.append(
                    f"в документе найдено несколько номеров ({len(numbers)}), "
                    "взят первый"
                )

    valid_from: Field[object] = Field(None)
    valid_until: Field[object] = Field(None)
    validity = _VALIDITY_PATTERN.search(text or "")
    if validity:
        valid_from = Field(parse_date(validity["from"]), confidence=confidence)
        valid_until = Field(parse_date(validity["until"]), confidence=confidence)
    elif doc_class in {DocumentClass.CERTIFICATE, DocumentClass.DECLARATION}:
        # Типичный случай: круглая печать органа лежит поверх поля «ПО».
        warnings.append("срок действия не прочитан — вероятно перекрыт печатью")

    products_raw: Field[str] = Field(None)
    designations: Field[list[str]] = Field(None)
    products = _PRODUCTS_PATTERN.search(text or "")
    if products:
        body = re.sub(r"\s+", " ", products["body"]).strip()
        products_raw = Field(body, confidence=confidence)
        designations = Field(_designations_from(body), confidence=confidence)

    annex = _ANNEX_PATTERN.search(products["body"] if products else "")
    annex_field: Field[str] = Field(None)
    if annex:
        annex_field = Field(annex["number"], confidence=confidence)
        warnings.append(
            "перечень изделий вынесен в приложение — с самого бланка его не получить"
        )

    return ExtractedDocument(
        doc_id=doc_id,
        doc_class=doc_class,
        text_source=text_source,
        source_path=source_path,
        engine=engine,
        page_count=page_count,
        mean_confidence=mean_confidence,
        registration_number=number_field,
        blank_number=_first(_BLANK_PATTERN, text, confidence, group="number"),
        valid_from=valid_from,  # type: ignore[arg-type]
        valid_until=valid_until,  # type: ignore[arg-type]
        products_raw=products_raw,
        product_designations=designations,
        technical_regulation=_first(_TR_PATTERN, text, confidence),
        tu_reference=_first(_TU_PATTERN, text, confidence),
        annex_reference=annex_field,
        mass_kg=_mass(text, confidence),
        certificate_numbers=certificates_field,
        warnings=tuple(warnings),
    )


def _designations_from(body: str) -> list[str]:
    found: dict[str, None] = {}
    for match in _DESIGNATION_PATTERN.finditer(body):
        normalized = normalize_designation(match.group(0))
        if len(normalized) >= 4:
            found.setdefault(normalized, None)
    for item in expand_ranges(body):
        found.setdefault(normalize_designation(item), None)
    return list(found)


def _first(
    pattern: re.Pattern[str], text: str, confidence: float, *, group: str | None = None
) -> Field[str]:
    match = pattern.search(text or "")
    if not match:
        return Field(None)
    value = match[group] if group else match.group(0)
    return Field(re.sub(r"\s+", " ", value).strip(), confidence=confidence)


def _mass(text: str, confidence: float) -> Field[float]:
    match = _MASS_PATTERN.search(text or "")
    if not match:
        return Field(None)
    try:
        return Field(float(match["value"].replace(",", ".")), confidence=confidence)
    except ValueError:
        return Field(None)


def from_payload(payload: dict) -> ExtractedDocument:
    """Восстановить `ExtractedDocument` из сохранённого JSON.

    Нужна, чтобы пересверять уже распознанный документ, не гоняя его через
    движок заново: вердикт со временем меняется сам, текст — нет.
    """
    from datetime import date as _date

    from .fsa.schema import DocumentClass as _DocumentClass, TextSource as _TextSource

    def field(name: str) -> Field:
        raw = (payload or {}).get(name) or {}
        value = raw.get("value")
        if name in {"valid_from", "valid_until"} and isinstance(value, str):
            try:
                value = _date.fromisoformat(value)
            except ValueError:
                value = None
        bbox = raw.get("bbox")
        return Field(
            value=value,
            confidence=float(raw.get("confidence", 1.0)),
            page=raw.get("page"),
            bbox=tuple(bbox) if bbox else None,
        )

    names = (
        "registration_number", "blank_number", "valid_from", "valid_until",
        "products_raw", "product_designations", "technical_regulation",
        "tu_reference", "annex_reference", "manufacturer_name",
        "product_designation", "product_name", "mass_kg", "dimensions_mm",
        "service_life", "warranty_period", "certificate_numbers",
    )
    return ExtractedDocument(
        doc_id=(payload or {}).get("doc_id", ""),
        doc_class=_DocumentClass((payload or {}).get("doc_class", "other")),
        text_source=_TextSource((payload or {}).get("text_source", "ocr")),
        source_path=(payload or {}).get("source_path", ""),
        engine=(payload or {}).get("engine", ""),
        page_count=int((payload or {}).get("page_count", 0) or 0),
        mean_confidence=(payload or {}).get("mean_confidence"),
        warnings=tuple((payload or {}).get("warnings", ())),
        **{name: field(name) for name in names},
    )
