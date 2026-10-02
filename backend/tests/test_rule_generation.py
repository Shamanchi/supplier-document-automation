"""Tests for the model-assisted unique-price rule prompt."""
from __future__ import annotations

from pathlib import Path

from app.parsing.rule_generation import (
    build_pdf_source_context,
    build_rule_generation_prompt,
)


TARGET_PDF = (
    Path(__file__).parents[4]
    / "external_price_corpus"
    / "pdf"
    / "008_PB-01_Москва_10.pdf"
)


def test_pdf_context_keeps_price_text_and_layout_coordinates() -> None:
    context = build_pdf_source_context(
        TARGET_PDF.read_bytes(), TARGET_PDF.name, max_pages=1, max_chars=8_000
    )

    assert context["loader"] == "pdf_text"
    assert "Наименование" in context["text"]
    assert "Стоимость" in context["text"]
    assert any(
        word["text"] == "920,00" and word["page_no"] == 1
        for word in context["words"]
    )
    price_word = next(word for word in context["words"] if word["text"] == "920,00")
    assert price_word["x0"] > 300


def test_rule_prompt_requires_spatial_rule_and_strict_json() -> None:
    prompt = build_rule_generation_prompt(
        filename=TARGET_PDF.name,
        loader="pdf_text",
        failure="таблица содержит name/photo, но не содержит price",
        source_context={
            "text": "Наименование Изображение Стоимость\nСкамья СК001 23 920,00 ₽",
            "words": [
                {"page_no": 1, "text": "Скамья СК001", "x0": 51, "top": 96},
                {"page_no": 1, "text": "23 920,00", "x0": 316, "top": 96},
            ],
        },
        observed_columns=[
            {"header": "Наименование", "role": "name", "confidence": 0.9},
            {"header": "Изображение", "role": "photo", "confidence": 0.9},
        ],
    )

    assert "Модель: gpt-4o" in prompt
    assert "таблица содержит name/photo" in prompt
    assert "pdf_layout_v1" in prompt
    assert "Не выдумывай" in prompt
    assert '"requires_manual_review"' in prompt
    assert "23 920,00" in prompt
