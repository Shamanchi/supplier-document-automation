"""Распознавание паспортов и сертификатов из PDF по текстовому слою.

Модуль читает PDF постранично (pdfplumber) и извлекает:

- класс документа (паспорт изделия / сертификат) по маркерам первых страниц;
- наименование изделия из первой и второй страниц паспорта;
- сведения о сертификате с предпоследней и последней страниц паспорта
  (обычно там указывается «сертификат соответствия № …»), а для файла
  сертификата — его регистрационный номер из всего текста.

Зависимости только stdlib + pdfplumber: модуль не импортирует app.*,
поэтому его можно использовать и из скриптов stage-2 вне контейнера.
"""

from __future__ import annotations

from .extract import (
    DOC_CERTIFICATE,
    DOC_OTHER,
    DOC_PASSPORT,
    CertificateMention,
    classify,
    find_certificate_mentions,
    find_product_name,
)
from .pages import PdfPages, read_pdf_pages
from .recognize import RecognizeResult, recognize_pdf

__all__ = [
    "DOC_CERTIFICATE",
    "DOC_OTHER",
    "DOC_PASSPORT",
    "CertificateMention",
    "PdfPages",
    "RecognizeResult",
    "classify",
    "find_certificate_mentions",
    "find_product_name",
    "read_pdf_pages",
    "recognize_pdf",
]