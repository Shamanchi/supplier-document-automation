"""Оркестрация распознавания одного PDF-документа."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

from .extract import (
    DOC_CERTIFICATE,
    DOC_OTHER,
    DOC_PASSPORT,
    CertificateMention,
    classify,
    find_certificate_mentions,
    find_product_name,
)
from .pages import Source, read_pdf_pages


@dataclass(frozen=True, slots=True)
class RecognizeResult:
    """Результат распознавания одного PDF.

    `product_name` заполняется для паспортов (первая-вторая страницы);
    `certificates` — сведения о сертификатах: у паспорта с предпоследней и
    последней страниц, у файла сертификата — по всему тексту.
    """

    file: str = ""
    folder_kind: str = ""
    supplier: str = ""
    doc_class: str = DOC_OTHER
    page_count: int = 0
    has_text_layer: bool = False
    product_name: str | None = None
    name_pages: tuple[int, int] = (1, 2)
    tail_pages: int = 2
    registration_number: str = ""
    certificates: tuple[CertificateMention, ...] = ()
    text_source: str = "none"
    error: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["name_pages"] = list(self.name_pages)
        data["certificates"] = [asdict(item) for item in self.certificates]
        return data


def _with_base(base: RecognizeResult, **overrides: object) -> RecognizeResult:
    values = {
        "file": base.file,
        "folder_kind": base.folder_kind,
        "supplier": base.supplier,
        "doc_class": base.doc_class,
        "page_count": base.page_count,
        "has_text_layer": base.has_text_layer,
        "product_name": base.product_name,
        "name_pages": base.name_pages,
        "tail_pages": base.tail_pages,
        "registration_number": base.registration_number,
        "certificates": base.certificates,
        "text_source": base.text_source,
        "error": base.error,
    }
    values.update(overrides)
    return RecognizeResult(**values)  # type: ignore[arg-type]


def recognize_pdf(
    source: Source,
    *,
    file: str = "",
    folder_kind: str = "",
    supplier: str = "",
    name_pages: int = 2,
    tail_pages: int = 2,
) -> RecognizeResult:
    """Прочитать PDF и распознать паспорт/сертификат.

    Читаются первые `name_pages` страниц для наименования и последние
    `tail_pages` страниц для сведений о сертификате (у файла сертификата
    номер ищется по всему тексту).
    """
    base = RecognizeResult(
        file=file,
        folder_kind=folder_kind,
        supplier=supplier,
        name_pages=(1, min(name_pages, 2)),
        tail_pages=tail_pages,
    )
    document = read_pdf_pages(source)
    if document.error:
        return _with_base(
            base, error=f"не удалось открыть PDF: {document.error}"
        )
    if document.page_count == 0:
        return _with_base(base, error="PDF без страниц")

    if not document.has_text_layer:
        return _with_base(
            base,
            page_count=document.page_count,
            has_text_layer=False,
            text_source="none",
            error="no_text_layer: скан или PDF без текстового слоя — нужен OCR",
        )

    head = "\n".join(document.first(2))
    whole = "\n".join(document.texts)
    doc_class = classify(head, pages=document.first(2))
    if doc_class == DOC_OTHER:
        doc_class = classify(whole)
    # Папка «Сертификаты» — подсказка: даже без маркера на титуле.
    if doc_class == DOC_OTHER and "СЕРТИФИКА" in whole.upper():
        doc_class = DOC_CERTIFICATE

    product_name: str | None = None
    if doc_class in (DOC_PASSPORT, DOC_OTHER):
        product_name = find_product_name(document.texts)

    certificates: Iterable[CertificateMention]
    if doc_class == DOC_CERTIFICATE:
        certificates = find_certificate_mentions(
            document.texts, whole_document=True
        )
    else:
        certificates = find_certificate_mentions(
            document.texts, tail_pages=tail_pages
        )

    mentions = tuple(certificates)
    registration_number = ""
    if doc_class == DOC_CERTIFICATE:
        for mention in mentions:
            if mention.number:
                registration_number = mention.number
                break

    return RecognizeResult(
        file=file,
        folder_kind=folder_kind,
        supplier=supplier,
        doc_class=doc_class,
        page_count=document.page_count,
        has_text_layer=True,
        product_name=product_name,
        name_pages=(1, min(name_pages, 2)),
        tail_pages=tail_pages,
        registration_number=registration_number,
        certificates=mentions,
        text_source="text_layer",
        error="",
    )