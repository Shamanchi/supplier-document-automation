# -*- coding: utf-8 -*-
"""Разбор обозначений изделий противопожарного оборудования.

Используется для автоматической привязки паспортов/сертификатов
(product_documents) к каноническим товарам (canonical_products) по обозначениям
изделий из названия документа и названий канонов. Модуль намеренно не зависит
от БД и фреймворков: его можно переиспользовать и в backend API, и в отдельных
stage-2 скриптах.

Два уровня сопоставления:

1. ``designation`` — точное пересечение обозначений: «ГОА-II-0,35-020-020» и
   «ГОА-II-0,35-020-020 (защищаемый объём…)» считаются одним обозначением.
2. ``codes`` — семейство + число с контролем варианта до числа: паспорт
   «МПП(Н-РП)-2-И-ГЭ-У2» привязывается к канону «МПП(Н-РП)-2(п)-И-ГЭ-У2», но
   не к «МПП(Н)-2(п)-И-ГЭ-У2» и не к переносному огнетушителю «МПП-2».
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# Семейства обозначений. Порядок важен: более длинные альтернативы раньше,
# чтобы «МППВК» не читалось как «МПП», а «УСП АА» (в распознанном тексте бывает
# с пробелом) — как «УСП».
FAMILIES: tuple[str, ...] = (
    "МППВК",
    "МУПТВ",
    "УСПАА",
    "УСП АА",
    "МПП",
    "ГГПТ",
    "ГОА",
    "УСП",
    "ОПУ",
    "ОСП",
    "ОВП",
    "ОП",
    "ОУ",
    "ИСТО",
)

_FAM_ALT = "|".join(FAMILIES)

# Типографские дефисы, встречающиеся в распознанном тексте паспортов.
_HYPHENS = "‐‑‒–—−"

# Каноны аксессуаров и ЗИП не получают паспорта изделий: кронштейн, картридж,
# переходник и т.п. продаются как отдельные позиции, а документ относится
# к самому изделию.
ACCESSORY_MARKERS: tuple[str, ...] = (
    "КРОНШТЕЙН",
    "КАРТРИДЖ",
    "КАРТИРИДЖ",
    "ПЕРЕХОДНИК",
    "КОМПЛЕКТ",
    "СТАПЕЛЬ",
    "ПЛАКАТ",
    "СУМКА",
    "ЧЕХОЛ",
    "ЗПУ",
    "РАСТРУБ",
    "ПОДСТАВКА",
    "ТЕЛЕЖК",
    "КРЕСТ",
)

_DESC_OPEN_RE = re.compile(r"\s+\([^()]*[А-ЯЁ]{4,}[^()]*\)")
_STOP_RE = re.compile(r"\s+(?:МОДИФИКАЦИЯ|МОДИФ\.?|ИЗМ\.?|ТУ\b|ГОСТ\b|ТЕХНИЧЕСКИЕ)")
_FAMILY_RE = re.compile(r"(?<![А-ЯЁA-Z0-9])(?:" + _FAM_ALT + r")")
_KEY_RE = re.compile(
    r"(?<![А-ЯЁA-Z0-9])"
    r"(?P<fam>" + _FAM_ALT + r")"
    r"(?P<pre>\([^()]*\))?"
    r"(?:[\s" + _HYPHENS + r"-]*[IVX]{1,3}[\s" + _HYPHENS + r"-]*)?"
    r"[\s" + _HYPHENS + r"-]*\(?Н?\)?[\s" + _HYPHENS + r"-]*"
    r"(?P<num>\d{1,3}(?:[.,]\d{1,2})?)"
)


def normalize_title(text: str | None) -> str:
    """Верхний регистр, единый дефис, запятая как точка, один пробел."""
    if not text:
        return ""
    value = text.upper()
    for char in _HYPHENS:
        value = value.replace(char, "-")
    value = value.replace(",", ".")
    return re.sub(r"\s+", " ", value).strip()


def _clean_tail(tail: str) -> str:
    tail = _DESC_OPEN_RE.split(tail)[0]
    tail = _STOP_RE.split(tail)[0]
    return tail.rstrip("- ")


def extract_designations(text: str | None) -> set[str]:
    """Обозначения из строки: «ГГПТ‐0,25; ГГПТ‐0,5» → {ГГПТ-0.25, ГГПТ-0.5}."""
    source = normalize_title(text)
    if not source:
        return set()
    found: set[str] = set()
    matches = list(_FAMILY_RE.finditer(source))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        tail = source[match.start() : end]
        designation = _clean_tail(tail).split(";")[0].strip(" -")
        if designation:
            found.add(designation)
    return found


@dataclass(frozen=True, slots=True)
class ProductCode:
    """Семейство, число и варианты одного обозначения."""

    family: str
    number: str
    pre: str | None = None
    post: str | None = None

    @property
    def key(self) -> str:
        """МПП(Н-Взр)-10(ст) → 'МПП10' — сравнение без учёта вариантов."""
        return f"{self.family.replace(' ', '')}{self.number}"


def _parse_post(source: str, end: int) -> str | None:
    marker = re.match(r"\(([^()]*)\)", source[end : end + 12])
    return marker.group(1) if marker else None


def extract_product_codes(text: str | None) -> list[ProductCode]:
    """Все вхождения «семейство + число» с вариантами до и после числа."""
    source = normalize_title(text)
    if not source:
        return []
    codes: list[ProductCode] = []
    for match in _KEY_RE.finditer(source):
        number = match.group("num")
        if number.endswith(".0"):
            number = number[:-2]
        pre = match.group("pre")
        codes.append(
            ProductCode(
                family=match.group("fam"),
                number=number,
                pre=pre.strip("()") if pre else None,
                post=_parse_post(source, match.end()),
            )
        )
    return codes


def extract_codes(text: str | None) -> set[str]:
    """Упрощённый набор ключей «семейство+число» (для совместимости со скриптами)."""
    return {code.key for code in extract_product_codes(text)}


def is_accessory_text(text: str | None) -> bool:
    """Название описывает аксессуар/ЗИП, а не само изделие."""
    source = normalize_title(text)
    return any(marker in source for marker in ACCESSORY_MARKERS)


def _codes_compatible(document: ProductCode, canonical: ProductCode) -> bool:
    if document.key != canonical.key:
        return False
    if document.pre is not None and document.pre != canonical.pre:
        return False
    if document.pre is None and canonical.pre is not None:
        # Паспорт без варианта (ГОА-II-0,35-020-020) не привязываем к канонам
        # с вариантом до числа (ГОА(Т1)-…, ГОА(Т)-…): это другие исполнения.
        return False
    if document.post and canonical.post and document.post != canonical.post:
        return False
    return True


def match_canonical_titles(
    document_text: str | None,
    canonical_titles: Iterable[str],
) -> list[tuple[str, str]]:
    """Какие каноны подходят документу; вернуть (title, stage).

    Сначала точные пересечения обозначений, затем — семейство+число с контролем
    вариантов. Аксессуары и строки без обозначений пропускаются.
    """
    source = document_text or ""
    document_designations = extract_designations(source)
    document_codes = extract_product_codes(source)
    if not document_designations and not document_codes:
        return []

    matched: list[tuple[str, str]] = []
    seen: set[str] = set()
    for title in canonical_titles:
        if is_accessory_text(title):
            continue
        if extract_designations(title) & document_designations:
            if title not in seen:
                seen.add(title)
                matched.append((title, "designation"))
            continue
        for document_code in document_codes:
            if any(
                _codes_compatible(document_code, canonical_code)
                for canonical_code in extract_product_codes(title)
            ):
                if title not in seen:
                    seen.add(title)
                    matched.append((title, "codes"))
                break
    return matched
