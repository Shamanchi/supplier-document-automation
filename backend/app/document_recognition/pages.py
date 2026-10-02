"""Постраничное чтение текстового слоя PDF (pdfplumber)."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Union

import pdfplumber

# pdfminer на некоторых PDF пишет шумные предупреждения (FontBBox и т.п.),
# которые не относятся к делу при пакетном распознавании.
logging.getLogger("pdfminer").setLevel(logging.ERROR)

# Ниже плотности текста страницу считаем не имеющей текстового слоя —
# тот же порог, что в контуре FSA (fsa/backend/app/services/documents.py).
MIN_TEXT_CHARS_PER_PAGE = 50

Source = Union[str, Path, bytes, "io.BytesIO"]


@dataclass(frozen=True, slots=True)
class PdfPages:
    """Текст всех страниц PDF + признак наличия текстового слоя."""

    texts: tuple[str, ...]
    has_text_layer: bool
    error: str = ""

    @property
    def page_count(self) -> int:
        return len(self.texts)

    def page(self, number: int) -> str:
        """Текст страницы (нумерация с 1); пусто, если страницы нет."""
        if 1 <= number <= len(self.texts):
            return self.texts[number - 1]
        return ""

    def tail(self, count: int) -> tuple[str, ...]:
        """Последние `count` страниц (не больше, чем есть в PDF)."""
        if count <= 0 or not self.texts:
            return ()
        return self.texts[-count:]

    def first(self, count: int) -> tuple[str, ...]:
        """Первые `count` страниц (не больше, чем есть в PDF)."""
        if count <= 0 or not self.texts:
            return ()
        return self.texts[:count]


def read_pdf_pages(source: Source) -> PdfPages:
    """Открыть PDF и вернуть текст каждой страницы.

    Структурно битый файл не роняет вызывающего: причина попадает в
    `error`, как в контуре FSA. Страница, которая не открылась, считается
    пустой, но остальные страницы продолжают читаться.
    """
    payload: Source = source
    if isinstance(source, bytes):
        payload = io.BytesIO(source)
    try:
        with pdfplumber.open(payload) as pdf:  # type: ignore[arg-type]
            texts: list[str] = []
            for page in pdf.pages:
                try:
                    texts.append(page.extract_text() or "")
                except Exception:  # noqa: BLE001 - одна битая страница
                    texts.append("")
            meaningful = sum(
                1 for item in texts if len(item.strip()) >= MIN_TEXT_CHARS_PER_PAGE
            )
            return PdfPages(
                texts=tuple(texts), has_text_layer=meaningful > 0
            )
    except Exception as error:  # noqa: BLE001
        return PdfPages(texts=(), has_text_layer=False, error=str(error))