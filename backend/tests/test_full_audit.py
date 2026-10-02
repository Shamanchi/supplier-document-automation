"""Small unit tests for the portable full-audit report helpers."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.full_audit import (
    audit_markdown,
    column_letter,
    inspect_candidate,
    parse_label_value,
    recipe_columns,
    write_rule_prompt_artifacts,
)


def test_parse_label_value_accepts_exactly_one_separator():
    assert parse_label_value("external=/corpus", "--source") == ("external", "/corpus")

    with pytest.raises(ValueError, match="LABEL=VALUE"):
        parse_label_value("missing", "--source")

    with pytest.raises(ValueError, match="не может быть пустым"):
        parse_label_value("=/corpus", "--source")


def test_recipe_columns_keeps_sheet_and_price_metadata():
    recipe = {
        "sheets": [
            {
                "match": {"name_pattern": "Прайс", "or_index": 0},
                "columns": [
                    {"index": 1, "role": "name", "header": "Наименование"},
                    {
                        "index": 3,
                        "role": "price",
                        "header": "Цена опт",
                        "price_descriptor": {"kind": "wholesale", "vat_mode": "included"},
                    },
                ],
            }
        ]
    }

    assert recipe_columns(recipe) == [
        {
            "sheet": "Прайс",
            "column_index": 1,
            "column_ref": "B",
            "header": "Наименование",
            "role": "name",
            "price_kind": None,
            "vat_mode": None,
        },
        {
            "sheet": "Прайс",
            "column_index": 3,
            "column_ref": "D",
            "header": "Цена опт",
            "role": "price",
            "price_kind": "wholesale",
            "vat_mode": "included",
        },
    ]
    assert column_letter(0) == "A"
    assert column_letter(27) == "AB"


def test_audit_prepares_rule_prompt_for_image_column_pdf():
    target = (
        Path(__file__).parents[4]
        / "external_price_corpus"
        / "pdf"
        / "008_PB-01_Москва_10.pdf"
    )

    candidate = inspect_candidate(target)

    assert candidate["state"] == "complex"
    generation = candidate["rule_generation"]
    assert generation["model"] == "gpt-4o"
    assert generation["prompt_id"]
    assert "pdf_layout_v1" in generation["prompt"]


def test_audit_routes_securpb_text_pdf_to_complex_not_ocr():
    target = (
        Path(__file__).parents[4]
        / "external_price_corpus"
        / "pdf"
        / "023_SecurPB.pdf"
    )

    candidate = inspect_candidate(target)

    assert candidate["loader"] == "pdf_text"
    assert candidate["state"] == "complex"
    assert candidate["rule_generation"]["model"] == "gpt-4o"


def test_audit_reports_builtin_layout_profile_as_verified_candidate():
    target = (
        Path(__file__).parents[4]
        / "original"
        / "datasets"
        / "прайсы"
        / "КП опт (3).pdf"
    )

    candidate = inspect_candidate(target)

    assert candidate["state"] == "candidate_layout"
    assert candidate["recipe"]["format"] == "pdf_layout_v1"
    assert candidate["validation"]["passed"] is True
    assert candidate["columns"]
    assert "rule_generation" not in candidate


def test_audit_reports_verified_workbook_layout_without_filename_match():
    target = (
        Path(__file__).parents[4]
        / "original"
        / "datasets"
        / "прайсы"
        / "price_list_sample.xlsx"
    )

    candidate = inspect_candidate(target)

    assert candidate["state"] == "candidate_auto"
    assert candidate["validation"]["passed"] is True
    assert candidate["recipe"].get("format") != "workbook_layout_v1"
    assert candidate["recipe"].get("profile") is None


def test_audit_does_not_prepare_model_prompt_for_ocr_pdf():
    target = (
        Path(__file__).parents[4]
        / "external_price_corpus"
        / "pdf"
        / "285_ScannedDoc_79.pdf"
    )

    candidate = inspect_candidate(target)

    assert candidate["state"] == "needs_ocr"
    assert "rule_generation" not in candidate


def test_audit_markdown_distinguishes_complex_text_pdf_from_ocr():
    markdown = audit_markdown(
        [
            {
                "source": "external",
                "relative_path": "pdf/layout.pdf",
                "filename": "layout.pdf",
                "extension": ".pdf",
                "size_bytes": 1,
                "sha256": "hash",
                "run_file": {"status": "complex", "loader": "pdf_text"},
                "price_version": {},
                "mapping_source": "not_available",
                "effective_columns": [],
                "referenced_parse_rule": None,
                "candidate": {
                    "state": "complex",
                    "message": "Текстовый fallback",
                    "rule_generation": {"prompt_id": "complex123", "model": "ultra"},
                },
                "reconciliation": None,
            }
        ]
    )

    assert "очередь complex" in markdown
    assert "OCR, ручной маппинг" not in markdown


def test_rule_prompt_artifact_is_written_separately(tmp_path: Path):
    audits = [
        {
            "candidate": {
                "rule_generation": {
                    "prompt_id": "abc123",
                    "model": "ultra",
                    "prompt": "prompt body",
                }
            }
        }
    ]

    write_rule_prompt_artifacts(tmp_path, audits)

    artifact = tmp_path / "rule_prompts" / "abc123.md"
    assert artifact.read_text(encoding="utf-8") == (
        "# Prompt генерации правила\n\n"
        "- Модель: `ultra`\n"
        "- Prompt ID: `abc123`\n\n"
        "```text\n"
        "prompt body\n"
        "```\n"
    )
