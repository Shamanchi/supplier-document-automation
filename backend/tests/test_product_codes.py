# -*- coding: utf-8 -*-
"""Тесты разбора обозначений и сопоставления документов с канонами."""

from __future__ import annotations

from app.matching.product_codes import (
    extract_codes,
    extract_designations,
    extract_product_codes,
    match_canonical_titles,
)


def test_designations_from_ggpt_title_with_typographic_hyphen():
    title = "ГЕНЕРАТОРЫ ГАЗОВОГО ПОЖАРОТУШЕНИЯ ГГПТ‐0,25; ГГПТ‐0,5"
    assert extract_designations(title) == {"ГГПТ-0.25", "ГГПТ-0.5"}
    assert extract_codes(title) == {"ГГПТ0.25", "ГГПТ0.5"}


def test_goa_designation_with_version_prefix():
    title = "ГЕНЕРАТОР ОГНЕТУШАЩЕГО АЭРОЗОЛЯ ГОА‐II‐1,10‐020‐010"
    assert extract_designations(title) == {"ГОА-II-1.10-020-010"}
    codes = extract_product_codes(title)
    assert len(codes) == 1
    assert codes[0].family == "ГОА"
    assert codes[0].number == "1.10"
    assert codes[0].pre is None


def test_module_code_captures_variants_before_and_after_number():
    title = "МОДУЛЬ ПОРОШКОВОГО ПОЖАРОТУШЕНИЯ МПП(Н-Взр)-10(ст)-И-ГЭ-У2"
    codes = extract_product_codes(title)
    assert len(codes) == 1
    code = codes[0]
    assert (code.family, code.number, code.pre, code.post) == (
        "МПП",
        "10",
        "Н-ВЗР",
        "СТ",
    )
    assert code.key == "МПП10"


def test_usp_space_family_normalizes():
    title = "УСТРОЙСТВО СИГНАЛЬНО-ПУСКОВОЕ ... УСП АА-1 Модификация v5"
    codes = extract_product_codes(title)
    assert any(code.key == "УСПАА1" for code in codes)


def test_passport_without_post_binds_to_po_mark_canonical_only():
    document = "МОДУЛЬ ПОРОШКОВОГО ПОЖАРОТУШЕНИЯ МПП(Н-РП)-2-И-ГЭ-У2"
    canonicals = [
        "МПП(Н-РП)-2(п)-И-ГЭ-У2 (огнетушащая способность …)",
        "МПП(Н)-2(п)-И-ГЭ-У2 (огнетушащая способность …)",
        "МПП-2 (переносной огнетушитель)",
        "Кронштейн настенный для МПП(Н-РП)-2",
    ]
    assert match_canonical_titles(document, canonicals) == [
        ("МПП(Н-РП)-2(п)-И-ГЭ-У2 (огнетушащая способность …)", "codes")
    ]


def test_goa_passport_does_not_bind_transport_temperature_variants():
    document = "ГЕНЕРАТОР ОГНЕТУШАЩЕГО АЭРОЗОЛЯ ГОА-II-0,35-020-020"
    canonicals = [
        "ГОА-II-0,35-020-020 (защищаемый объем …)",
        "ГОА-II-0,35-020-020(А) (транспортные …)",
        "ГОА(Т1)-II-0,35-020-020 (защищаемый объем …)",
        "ГОА(Т)-II-0,35-020-020 (защищаемый объем …)",
    ]
    matched = match_canonical_titles(document, canonicals)
    titles = [title for title, _ in matched]
    assert "ГОА-II-0,35-020-020 (защищаемый объем …)" in titles
    assert "ГОА-II-0,35-020-020(А) (транспортные …)" in titles
    assert not any("ГОА(Т1)" in title or "ГОА(Т)-" in title for title in titles)


def test_accessory_canonicals_are_not_matched():
    document = "ГЕНЕРАТОРЫ ГАЗОВОГО ПОЖАРОТУШЕНИЯ ГГПТ‐1,0"
    canonicals = [
        "ГГПТ-1,0 (Генератор газового пожаротушения …)",
        "Кронштейн настенный для ГГПТ-1,0",
        "Картиридж для ГГПТ-1,0; ГГПТ-3,0; ГГПТ-7,0, 1 шт.",
    ]
    assert match_canonical_titles(document, canonicals) == [
        ("ГГПТ-1,0 (Генератор газового пожаротушения …)", "designation")
    ]


def test_explosion_proof_module_does_not_bind_plain_module():
    document = "МОДУЛЬ ПОЖАРОТУШЕНИЯ ПЕНОЙ ВЫСОКОЙ КРАТНОСТИ МППВК(Взр)-17-ГЗ"
    canonicals = [
        "МППВК(Взр)-17-ГЗ (защ. площадь …)",
        "МППВК-17-ГЗ (защ. площадь …)",
    ]
    assert match_canonical_titles(document, canonicals) == [
        ("МППВК(Взр)-17-ГЗ (защ. площадь …)", "designation")
    ]


def test_muptv_multi_temperature_canonical_matches_by_codes():
    document = "МУПТВ(Взр)-13,5-ГЗ-ВД (tºC = -30;-50) МУПТВ-13"
    canonicals = [
        "МУПТВ(Взр)-13,5-ГЗ-ВД (tºC=-30) (Hуст от 2,5 до 6 м.)",
        "МУПТВ-13,5-ГЗ-ВД (tºC=-30) (Hуст от 2,5 до 6 м.)",
    ]
    matched = match_canonical_titles(document, canonicals)
    assert matched == [
        ("МУПТВ(Взр)-13,5-ГЗ-ВД (tºC=-30) (Hуст от 2,5 до 6 м.)", "codes")
    ]
