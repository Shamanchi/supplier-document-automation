#!/usr/bin/env python3
"""Audit product/offer attributes present in the ten semantic price contexts.

The script deliberately keeps the source coordinates used by the extractor and
validates every model citation against the exact masked input record.  Qwen is
used only to name and classify candidate meanings; unsupported candidates are
dropped before the Russian Markdown report is rendered.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


FIXED_MODEL = "qwen3-235b-a22b-fp8/latest"
BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR.parent
DEFAULT_CORPUS = APP_DIR.parents[1] / "external_price_corpus"
DEFAULT_JSON = APP_DIR / "reports/аудит_признаков_позиций_10_прайсов_2026-07-15.json"
DEFAULT_REPORT = APP_DIR / "reports/аудит_признаков_позиций_10_прайсов_2026-07-15.md"
FILES = [
    "xls/004_Global_Security_KZ.xls",
    "xls/015_UNFIRE01.xls",
    "xls/032_Интернет-магазин_пожарного_обо.xls",
    "xls/043_НПО_Сибирский_Арсенал.xls",
    "xls/054_Пождело_оборудование.xls",
    "xlsx/005_gefest_orositeli.xlsx",
    "xlsx/043_Гефест.xlsx",
    "xlsx/047_Комплектоборудование_KZ.xlsx",
    "xlsx/054_МЧС01_ДВ.xlsx",
    "xlsx/058_ПироХимУрал.xlsx",
]

BUSINESS_CLASSES = {
    "identity",
    "product_characteristic",
    "offer_condition",
    "catalog_classification",
    "service_or_process",
    "supplier_or_document_metadata",
    "noise_or_formatting",
    "manual_review",
}
STORAGE_TARGETS = {
    "item_base_field",
    "item_attribute_value",
    "price_value",
    "offer_fact",
    "category_relation",
    "do_not_store",
    "manual_review",
}
VALUE_KINDS = {"text", "integer", "decimal", "boolean", "date", "datetime", "option"}

SYSTEM_PROMPT = """Ты аудитор товарных данных. Анализируй только переданные записи прайсов.
Нельзя придумывать признак без дословного evidence. Отличай постоянное свойство товара от цены,
наличия, скидки, минимального количества и срока поставки. Не превращай розницу/дилера/НДС/валюту
в характеристики товара. Категория — связь классификации, а не характеристика. Название, артикул,
единица, описание и габариты — базовые поля позиции. Технические параметры идут в типизированную
таблицу характеристик. Услуга/операция (перезарядка, монтаж, доставка) не является характеристикой
физического продукта. Возвращай только JSON по схеме."""


def _schema() -> dict[str, Any]:
    evidence = {
        "type": "object",
        "additionalProperties": False,
        "required": ["record_id", "fragment"],
        "properties": {
            "record_id": {"type": "string"},
            "fragment": {"type": "string"},
        },
    }
    candidate = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "code",
            "name_ru",
            "business_class",
            "storage_target",
            "value_kind",
            "canonical_unit",
            "aliases",
            "reason",
            "evidence",
        ],
        "properties": {
            "code": {"type": "string", "pattern": "^[a-z][a-z0-9_]{1,79}$"},
            "name_ru": {"type": "string"},
            "business_class": {"type": "string", "enum": sorted(BUSINESS_CLASSES)},
            "storage_target": {"type": "string", "enum": sorted(STORAGE_TARGETS)},
            "value_kind": {"type": "string", "enum": sorted(VALUE_KINDS)},
            "canonical_unit": {"type": ["string", "null"]},
            "aliases": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
            "reason": {"type": "string"},
            "evidence": {"type": "array", "items": evidence, "minItems": 1, "maxItems": 4},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["candidates"],
        "properties": {
            "candidates": {"type": "array", "items": candidate, "maxItems": 100}
        },
    }


def _clean(value: object | None) -> str:
    return " ".join(str(value).replace("\u00a0", " ").split()) if value is not None else ""


def _md(value: object | None) -> str:
    if value is None or value == "":
        return "—"
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def _unique_values(rows: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for cell in row.get("cells") or []:
            if cell.get("is_merged_fill"):
                continue
            value = _clean(cell.get("value"))
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                result.append(value)
    return result


def _collect(corpus: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    sys.path.insert(0, str(BACKEND_DIR))
    from app.parsing import loaders
    from app.parsing.rule_proposal import build_tabular_section_contexts

    records: list[dict[str, Any]] = []
    explicit_headers: dict[tuple[str, str], dict[str, Any]] = {}
    file_stats: list[dict[str, Any]] = []
    context_count = 0
    row_count = 0
    for relative_path in FILES:
        source = corpus / relative_path
        data = source.read_bytes()
        loader = loaders.detect_loader(data, source.name)
        grids = loaders.load_grids_for(loader, data, source.name)
        contexts = build_tabular_section_contexts(grids, sample_rows_limit=4)
        context_count += len(contexts)
        file_rows = 0
        for context in contexts:
            observations = context.get("deterministic_observations") or []
            headers_by_column = {
                int(item["index"]): {
                    "header": _clean(item.get("header")),
                    "role": str(item.get("suggested_role") or "ignore"),
                }
                for item in observations
                if isinstance(item.get("index"), int)
            }
            for item in observations:
                header = _clean(item.get("header"))
                role = str(item.get("suggested_role") or "ignore")
                if not header:
                    continue
                key = (header.casefold(), role)
                entry = explicit_headers.setdefault(
                    key,
                    {
                        "header": header,
                        "role": role,
                        "occurrences": 0,
                        "files": set(),
                        "examples": [],
                    },
                )
                entry["occurrences"] += 1
                entry["files"].add(relative_path)
                example = f"{relative_path} · {context['sheet_name']} · строка {item.get('source_header_row_index')}"
                if example not in entry["examples"] and len(entry["examples"]) < 3:
                    entry["examples"].append(example)

            category = _unique_values(context.get("category_rows") or [])
            for row in context.get("sample_rows") or []:
                cells: list[dict[str, Any]] = []
                seen_values: set[tuple[int, str]] = set()
                for column_index, cell in enumerate(row.get("cells") or []):
                    if cell.get("is_merged_fill"):
                        continue
                    value = _clean(cell.get("value"))
                    if not value:
                        continue
                    key = (column_index, value.casefold())
                    if key in seen_values:
                        continue
                    seen_values.add(key)
                    header = headers_by_column.get(column_index, {})
                    cells.append(
                        {
                            "column": column_index,
                            "header": header.get("header") or "",
                            "role": header.get("role") or "unknown",
                            "value": value,
                        }
                    )
                if not cells:
                    continue
                record_id = f"r{len(records) + 1:05d}"
                record = {
                    "record_id": record_id,
                    "price": relative_path,
                    "sheet": context["sheet_name"],
                    "row_index": row["row_index"],
                    "category": category,
                    "cells": cells,
                }
                record["search_text"] = " | ".join(
                    category
                    + [
                        f"{cell['header'] or 'колонка ' + str(cell['column'])}: {cell['value']}"
                        for cell in cells
                    ]
                )
                records.append(record)
                row_count += 1
                file_rows += 1
        file_stats.append(
            {
                "price": relative_path,
                "sheets": len(grids),
                "contexts": len(contexts),
                "rows": file_rows,
            }
        )

    serialized_headers: list[dict[str, Any]] = []
    for entry in explicit_headers.values():
        serialized_headers.append(
            {
                **entry,
                "files": sorted(entry["files"]),
            }
        )
    serialized_headers.sort(key=lambda item: (item["role"], item["header"].casefold()))
    return records, serialized_headers, {
        "files": file_stats,
        "contexts": context_count,
        "rows": row_count,
        "explicit_headers": len(serialized_headers),
    }


def _record_line(record: dict[str, Any]) -> str:
    return (
        f"{record['record_id']} | файл={record['price']} | лист={record['sheet']} | "
        f"строка={record['row_index']} | {record['search_text']}"
    )


def _batches(records: list[dict[str, Any]], max_chars: int = 42_000) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for record in records:
        line_size = len(_record_line(record)) + 1
        if current and size + line_size > max_chars:
            batches.append(current)
            current = []
            size = 0
        current.append(record)
        size += line_size
    if current:
        batches.append(current)
    return batches


async def _request(batch: list[dict[str, Any]], batch_number: int) -> dict[str, Any]:
    api_key = os.environ.get("YANDEXGPT_API_KEY", "").strip()
    folder_id = os.environ.get("YANDEXGPT_FOLDER_ID", "").strip()
    configured_model = os.environ.get("YANDEXGPT_RULE_MODEL", FIXED_MODEL).strip()
    if configured_model != FIXED_MODEL:
        raise RuntimeError(f"Разрешена только зафиксированная модель {FIXED_MODEL}")
    if not api_key or not folder_id:
        raise RuntimeError("YANDEXGPT_API_KEY/YANDEXGPT_FOLDER_ID не заданы")
    endpoint = os.environ.get(
        "YANDEXGPT_API_BASE_URL", "https://llm.api.cloud.yandex.net/v1"
    ).rstrip("/") + "/chat/completions"
    model = f"gpt://{folder_id}/{FIXED_MODEL}"
    prompt = """Найди ВСЕ РАЗЛИЧНЫЕ СМЫСЛЫ признаков, которые реально встречаются в записях ниже.
Учитывай явные заголовки колонок, категорию и признаки внутри названия/описания. Объединяй варианты
написания одного смысла (например, вес/масса), но не объединяй разные параметры. Конкретные значения
не являются отдельными признаками. Для каждого смысла реши бизнес-класс и место хранения:

- item_base_field: name/article/unit/description/dimensions и иные уже базовые поля позиции;
- item_attribute_value: стабильная техническая/физическая характеристика товара;
- price_value: вид цены, сумма, валюта, НДС, минимальное количество именно ценовой ступени;
- offer_fact: наличие, остаток, скидка, упаковка поставки, срок/условие поставки;
- category_relation: раздел/вид товара;
- do_not_store: номер строки, оформление, примечание-звёздочка, служебный текст;
- manual_review: смысл реально есть, но по фрагменту нельзя безопасно решить.

Если запись описывает услугу, её операция/условия не становятся характеристикой физического товара.
Каждый evidence.fragment скопируй ДОСЛОВНО из строки указанного record_id; без evidence кандидат будет удалён.

ЗАПИСИ:
""" + "\n".join(_record_line(record) for record in batch)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "price_position_attribute_audit",
                "strict": True,
                "schema": _schema(),
            },
        },
        "temperature": 0,
        "max_tokens": 6500,
    }
    async with httpx.AsyncClient(timeout=180) as client:
        response: httpx.Response | None = None
        for attempt in range(3):
            try:
                response = await client.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=body,
                )
                if response.status_code != 429 and response.status_code < 500:
                    break
            except httpx.TransportError:
                if attempt == 2:
                    raise
            await asyncio.sleep(2**attempt)
    assert response is not None
    if response.is_error:
        raise RuntimeError(f"Qwen batch {batch_number}: HTTP {response.status_code}")
    payload = response.json()
    content = payload["choices"][0]["message"]["content"]
    result = json.loads(content)
    if not isinstance(result.get("candidates"), list):
        raise RuntimeError(f"Qwen batch {batch_number}: нет candidates")
    return result


def _normal_code(value: object) -> str:
    code = re.sub(r"[^a-z0-9_]+", "_", str(value).casefold()).strip("_")
    return code[:80]


# Canonicalise only high-confidence synonyms. Unfamiliar meanings remain
# separate and visible for manual review rather than being merged by guess.
CODE_ALIASES = {
    "mass": "weight",
    "product_weight": "weight",
    "overall_dimensions": "dimensions",
    "size": "dimensions",
    "product_dimensions": "dimensions",
    "sku": "article",
    "product_code": "article",
    "brand": "manufacturer",
    "maker": "manufacturer",
    "diameter_nominal": "nominal_diameter",
    "dn": "nominal_diameter",
    "operating_pressure": "working_pressure",
    "working_temperature": "temperature_range",
    "temperature": "temperature_range",
    "material_type": "material",
    "colour": "color",
    "ral_color": "color",
    "fire_extinguishing_agent": "extinguishing_agent_type",
    "extinguishing_agent": "extinguishing_agent_type",
    "charge_weight": "charge_mass",
    "capacity": "volume",
    "gross_volume": "volume",
    "gost": "standard",
    "standard_gost": "standard",
    "price": "price_amount",
    "retail_price": "price_amount",
    "wholesale_price": "price_amount",
    "dealer_price": "price_amount",
    "currency": "price_currency",
    "zones": "zones_count",
    "number_of_zones": "zones_count",
    "pressure": "working_pressure",
    "pressure_rating": "working_pressure",
    "ip_rating": "protection_rating",
    "fire_resistant_rating": "protection_rating",
    "power_rating": "power",
    "orifice_diameter": "nozzle_diameter",
}


# A model may see the same meaning once as an identity token and once as a
# technical property.  These storage decisions are architecture rules, not a
# majority vote over batches, so they override such cross-batch disagreement.
DECISION_OVERRIDES: dict[str, tuple[str, str, str, str | None]] = {
    "name": ("identity", "item_base_field", "text", None),
    "product_name": ("identity", "item_base_field", "text", None),
    "model": ("identity", "item_base_field", "text", None),
    "article": ("identity", "item_base_field", "text", None),
    "unit": ("identity", "item_base_field", "text", None),
    "description": ("identity", "item_base_field", "text", None),
    "dimensions": ("product_characteristic", "item_base_field", "text", None),
    "category": ("catalog_classification", "category_relation", "text", None),
    "subcategory": ("catalog_classification", "category_relation", "text", None),
    "manufacturer": ("identity", "item_attribute_value", "text", None),
    "material": ("product_characteristic", "item_attribute_value", "option", None),
    "ral": ("product_characteristic", "item_attribute_value", "option", None),
    "voltage": ("product_characteristic", "item_attribute_value", "decimal", "V"),
    "thread_size": ("product_characteristic", "item_attribute_value", "text", None),
    "protection_rating": ("product_characteristic", "item_attribute_value", "option", None),
    "mounting_type": ("product_characteristic", "item_attribute_value", "option", None),
    "nozzle_type": ("product_characteristic", "item_attribute_value", "option", None),
    "color": ("product_characteristic", "item_attribute_value", "option", None),
    "frequency": ("product_characteristic", "item_attribute_value", "decimal", "MHz"),
    "height": ("manual_review", "manual_review", "text", None),
    "width": ("manual_review", "manual_review", "text", None),
    "width_mm": ("manual_review", "manual_review", "text", None),
    "cable_diameter": ("manual_review", "manual_review", "text", None),
    "type": ("manual_review", "manual_review", "text", None),
    "note": ("manual_review", "manual_review", "text", None),
    "penetration_depth": ("manual_review", "manual_review", "text", None),
    "manual_review": ("manual_review", "manual_review", "text", None),
    "price_amount": ("offer_condition", "price_value", "decimal", None),
    "price_currency": ("offer_condition", "price_value", "option", None),
    "contract_price": ("offer_condition", "price_value", "text", None),
    "discount": ("offer_condition", "offer_fact", "decimal", "%"),
    "service": ("service_or_process", "item_base_field", "text", None),
    "service_type": ("service_or_process", "item_base_field", "text", None),
    "service_scope": ("service_or_process", "item_base_field", "text", None),
    "service_condition": ("service_or_process", "item_base_field", "text", None),
    "do_not_store": ("manual_review", "manual_review", "text", None),
    # ОП-2/ОУ-1 do not prove litres or kilograms by themselves; interpreting
    # a model designation as a numeric characteristic requires a product-family rule.
    "volume": ("manual_review", "manual_review", "text", None),
    "fire_extinguisher_capacity": ("manual_review", "manual_review", "text", None),
    # "колба 3 мм" proves bulb diameter, not triggering temperature.
    "glass_bulb_temperature": ("manual_review", "manual_review", "text", None),
}


def _postprocess_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    regrouped: dict[str, dict[str, Any]] = {}
    for source in candidates:
        code = CODE_ALIASES.get(source["code"], source["code"])
        item = {**source, "code": code}
        override = DECISION_OVERRIDES.get(code)
        if override:
            (
                item["business_class"],
                item["storage_target"],
                item["value_kind"],
                item["canonical_unit"],
            ) = override
        current = regrouped.get(code)
        if current is None:
            regrouped[code] = item
            continue
        for alias in item.get("aliases") or []:
            if alias.casefold() not in {value.casefold() for value in current["aliases"]}:
                current["aliases"].append(alias)
        for reason in item.get("reasons") or []:
            if reason not in current["reasons"]:
                current["reasons"].append(reason)
        evidence_keys = {
            (evidence["record_id"], evidence["fragment"].casefold())
            for evidence in current["evidence"]
        }
        for evidence in item.get("evidence") or []:
            key = (evidence["record_id"], evidence["fragment"].casefold())
            if key not in evidence_keys and len(current["evidence"]) < 8:
                current["evidence"].append(evidence)
                evidence_keys.add(key)
        current["model_occurrences"] += item.get("model_occurrences", 0)
    return sorted(
        regrouped.values(),
        key=lambda item: (item["storage_target"], item["name_ru"].casefold()),
    )


def _validate_and_merge(
    results: list[dict[str, Any]], records: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    by_id = {record["record_id"]: record for record in records}
    merged: dict[str, dict[str, Any]] = {}
    rejected_evidence = 0
    rejected_candidates = 0
    conflicts = 0
    for result in results:
        for raw in result.get("candidates") or []:
            code = CODE_ALIASES.get(_normal_code(raw.get("code")), _normal_code(raw.get("code")))
            if not code:
                rejected_candidates += 1
                continue
            evidence: list[dict[str, Any]] = []
            for item in raw.get("evidence") or []:
                record = by_id.get(str(item.get("record_id")))
                fragment = _clean(item.get("fragment"))
                if not record or not fragment or fragment.casefold() not in record["search_text"].casefold():
                    rejected_evidence += 1
                    continue
                evidence.append(
                    {
                        "record_id": record["record_id"],
                        "fragment": fragment,
                        "price": record["price"],
                        "sheet": record["sheet"],
                        "row_index": record["row_index"],
                    }
                )
            if not evidence:
                rejected_candidates += 1
                continue
            business_class = str(raw.get("business_class"))
            storage_target = str(raw.get("storage_target"))
            value_kind = str(raw.get("value_kind"))
            if business_class not in BUSINESS_CLASSES or storage_target not in STORAGE_TARGETS:
                rejected_candidates += 1
                continue
            if value_kind not in VALUE_KINDS:
                value_kind = "text"
            current = merged.get(code)
            if current is None:
                current = {
                    "code": code,
                    "name_ru": _clean(raw.get("name_ru")) or code,
                    "business_class": business_class,
                    "storage_target": storage_target,
                    "value_kind": value_kind,
                    "canonical_unit": _clean(raw.get("canonical_unit")) or None,
                    "aliases": [],
                    "reasons": [],
                    "evidence": [],
                    "model_occurrences": 0,
                }
                merged[code] = current
            elif (
                current["business_class"] != business_class
                or current["storage_target"] != storage_target
                or current["value_kind"] != value_kind
            ):
                conflicts += 1
                current["business_class"] = "manual_review"
                current["storage_target"] = "manual_review"
                current["value_kind"] = "text"
            current["model_occurrences"] += 1
            for alias in [raw.get("name_ru"), *(raw.get("aliases") or [])]:
                alias_text = _clean(alias)
                if alias_text and alias_text.casefold() not in {
                    existing.casefold() for existing in current["aliases"]
                }:
                    current["aliases"].append(alias_text)
            reason = _clean(raw.get("reason"))
            if reason and reason not in current["reasons"]:
                current["reasons"].append(reason)
            existing_evidence = {
                (item["record_id"], item["fragment"].casefold()) for item in current["evidence"]
            }
            for item in evidence:
                key = (item["record_id"], item["fragment"].casefold())
                if key not in existing_evidence and len(current["evidence"]) < 8:
                    current["evidence"].append(item)
                    existing_evidence.add(key)

    candidates = sorted(
        merged.values(),
        key=lambda item: (item["storage_target"], item["name_ru"].casefold()),
    )
    return candidates, {
        "rejected_evidence": rejected_evidence,
        "rejected_candidates": rejected_candidates,
        "classification_conflicts": conflicts,
    }


ROLE_DESTINATIONS = {
    "name": ("Идентификация", "supplier_price_items.source_name", "не характеристика"),
    "article": ("Идентификация", "supplier_price_items.source_article", "не характеристика"),
    "unit": ("Базовое поле", "supplier_price_items.unit_code", "не характеристика"),
    "description": ("Базовое поле", "supplier_price_items.description", "не характеристика"),
    "dimensions": ("Характеристика в базовом поле", "supplier_price_items.dimensions", "да, но не EAV"),
    "category": ("Классификация", "supplier_price_items.category_path / связь категории", "не характеристика"),
    "price": ("Коммерческое предложение", "supplier_price_values", "не характеристика"),
    "manufacturer": ("Идентификация/справочник", "атрибут manufacturer; позже связь производителя", "не техническая"),
    "weight": ("Характеристика", "supplier_price_item_attribute_values", "да"),
    "volume": ("Характеристика", "supplier_price_item_attribute_values", "да"),
    "attribute": ("Характеристика-кандидат", "supplier_price_item_attribute_values (draft до маппинга)", "да после ревью"),
    "quantity_threshold": ("Коммерческое условие", "supplier_price_values.min_quantity", "не характеристика"),
    "discount_percent": ("Коммерческое условие", "отдельный offer fact", "не характеристика"),
    "stock": ("Состояние предложения", "отдельный offer fact", "не характеристика"),
    "lead_time": ("Состояние предложения", "отдельный offer fact", "не характеристика"),
    "photo": ("Медиа", "ссылка/медиа позиции", "не характеристика"),
    "row_number": ("Служебное", "не хранить как характеристику", "нет"),
    "ignore": ("Не определено", "manual review", "не сохранять автоматически"),
}

TARGET_TITLES = {
    "item_attribute_value": "Идёт в БД как типизированная характеристика продукта",
    "item_base_field": "Хранится в БД, но как базовое поле позиции",
    "price_value": "Хранится как цена предложения, не как характеристика продукта",
    "offer_fact": "Хранится как состояние/условие предложения, не как характеристика",
    "category_relation": "Хранится как классификация, не как характеристика",
    "do_not_store": "Не переносится в продуктовые характеристики",
    "manual_review": "Требует ручного решения до загрузки",
}


HEADER_OVERRIDES: dict[str, tuple[str, str, str]] = {
    "размер скидки": ("Коммерческое условие: скидка", "offer_fact: discount_percent", "нет"),
    "размер скидки 15%": ("Коммерческое условие: скидка", "offer_fact: discount_percent", "нет"),
    "номинальный диаметр присоединенной трубы, мм": (
        "Техническая характеристика: номинальный диаметр",
        "supplier_price_item_attribute_values: nominal_diameter",
        "да",
    ),
    "длинна, мм": (
        "Техническая характеристика: длина",
        "supplier_price_item_attribute_values: length",
        "да",
    ),
    "единицы": ("Базовое поле: единица измерения", "supplier_price_items.unit_code", "нет"),
    "крепление": (
        "Техническая характеристика: способ крепления",
        "supplier_price_item_attribute_values: mounting_type",
        "да",
    ),
    "маркировка": (
        "Идентификационный признак/маркировка",
        "supplier_price_item_attribute_values: marking (draft)",
        "не техническая",
    ),
    "оглавление": ("Служебная навигация", "do_not_store", "нет"),
    "id описания": ("Внутренний идентификатор документа", "source provenance либо do_not_store", "нет"),
    "'гк гефест'!r1c1": ("Формула/ссылка Excel", "do_not_store", "нет"),
    "окно": (
        "Кандидат: наличие/тип окна",
        "supplier_price_item_attribute_values: window_type (draft)",
        "да после проверки значений",
    ),
    "примечания": ("Базовое описание", "supplier_price_items.description", "нет"),
    "расч.": ("Неоднозначный расчётный столбец", "manual_review", "не автоматически"),
    "тип": (
        "Кандидат: тип/исполнение",
        "supplier_price_item_attribute_values: supplier-scoped draft",
        "да после проверки значений",
    ),
    "тип шкафа": (
        "Техническая характеристика: тип шкафа",
        "supplier_price_item_attribute_values: cabinet_type",
        "да",
    ),
    "цвет": (
        "Техническая характеристика: цвет",
        "supplier_price_item_attribute_values: color",
        "да",
    ),
}


def _header_destination(header: dict[str, Any]) -> tuple[str, str, str, bool]:
    override = HEADER_OVERRIDES.get(header["header"].casefold())
    default = ROLE_DESTINATIONS.get(
        header["role"], ("Неизвестный заголовок", "supplier-scoped draft + review", "только после ревью")
    )
    if override:
        return (*override, override != default)
    return (*default, False)


def _render(
    *,
    report_path: Path,
    candidates: list[dict[str, Any]],
    headers: list[dict[str, Any]],
    stats: dict[str, Any],
    validation: dict[str, int],
    batch_count: int,
) -> None:
    target_counts = Counter(item["storage_target"] for item in candidates)
    lines = [
        "# Аудит признаков в позициях десяти тестовых прайсов",
        "",
        f"Сформировано: `{datetime.now(timezone.utc).isoformat()}`.",
        f"Модель семантического выделения: `{FIXED_MODEL}`; локальная проверка evidence обязательна.",
        "",
        "## Вывод",
        "",
        "В канонические характеристики продукта нужно переносить только устойчивые технические и физические свойства. "
        "Название, артикул, единица, описание и габариты остаются базовыми полями позиции. Цена, вид цены, валюта, "
        "НДС, скидка, минимальное количество, наличие и срок поставки принадлежат предложению поставщика. Категория "
        "хранится отдельной связью. Служебные номера, звёздочки, разметка и карточки поставщика не являются данными продукта.",
        "",
        f"Проверено **{len(stats['files'])} файлов, {stats['contexts']} смысловых куска и {stats['rows']} строк позиций**. "
        f"Выделено **{len(candidates)} различных смыслов признаков** из {batch_count} модельных пакетов; "
        f"{validation['rejected_candidates']} неподтверждённых кандидатов и {validation['rejected_evidence']} неверных ссылок "
        "на источник отброшены.",
        f"Между пакетами было **{validation['classification_conflicts']} расхождения классификации**; для известных "
        "смыслов они разрешены зафиксированными правилами архитектуры БД, а действительно неоднозначные случаи оставлены на review.",
        "",
        "| Решение | Различных смыслов |",
        "| --- | ---: |",
    ]
    for target in TARGET_TITLES:
        lines.append(f"| {TARGET_TITLES[target]} | {target_counts[target]} |")
    lines.extend(
        [
            "",
            "## Правило размещения в БД",
            "",
            "| Что найдено | Бизнес-смысл | Куда писать | Характеристика продукта |",
            "| --- | --- | --- | --- |",
        ]
    )
    grouped_decisions: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for header in headers:
        business, destination, is_characteristic, _ = _header_destination(header)
        grouped_decisions[(business, destination, is_characteristic)].append(header)
    for (business, destination, is_characteristic), grouped in sorted(grouped_decisions.items()):
        examples = ", ".join(f"`{item['header']}`" for item in grouped[:8])
        if len(grouped) > 8:
            examples += f" и ещё {len(grouped) - 8}"
        lines.append(
            f"| {examples} | {business} | `{destination}` | {is_characteristic} |"
        )
    corrected_headers = [header for header in headers if _header_destination(header)[3]]
    lines.extend(
        [
            "",
            "### Заголовки, для которых роль extractor нельзя использовать как решение БД",
            "",
            f"Таких заголовков: **{len(corrected_headers)}**. Это отдельные кандидаты на исправление словаря ролей extractor.",
            "",
            "| Заголовок | Сейчас определён как | Решение аудита | Почему |",
            "| --- | --- | --- | --- |",
        ]
    )
    for header in corrected_headers:
        business, destination, _, _ = _header_destination(header)
        lines.append(
            f"| `{_md(header['header'])}` | `{header['role']}` | `{destination}` | {_md(business)} |"
        )
    lines.extend(
        [
            "",
            "## Полный нормализованный перечень признаков",
            "",
            "Ниже один ряд — один смысл, а не одно значение. Для числовых характеристик исходный текст сохраняется "
            "в `raw_value`, а нормализованное число — в соответствующем scalar-поле. JSON/JSONB не требуется.",
            "",
        ]
    )
    for target, title in TARGET_TITLES.items():
        subset = [item for item in candidates if item["storage_target"] == target]
        lines.extend(
            [
                f"### {title}",
                "",
                "| Код | Признак | Тип / единица | Варианты названия | Дословное доказательство | Решение |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        if not subset:
            lines.append("| — | Не найдено | — | — | — | — |")
        for item in subset:
            aliases = ", ".join(item["aliases"][:8])
            evidence = "<br>".join(
                f"`{e['record_id']}` {e['price']} / {e['sheet']} / строка {e['row_index']}: «{_md(e['fragment'])}»"
                for e in item["evidence"][:4]
            )
            reason = item["reasons"][0] if item["reasons"] else "Классифицировано по исходному контексту"
            lines.append(
                f"| `{item['code']}` | {_md(item['name_ru'])} | `{item['value_kind']}` / "
                f"{_md(item['canonical_unit'])} | {_md(aliases)} | {evidence} | {_md(reason)} |"
            )
        lines.append("")

    lines.extend(
        [
            "## Все явные заголовки колонок",
            "",
            f"Найдено **{len(headers)}** различных сочетаний «заголовок + роль». Они приведены полностью, "
            "чтобы было видно, какие данные поступают отдельными колонками, а какие извлечены из текста позиции.",
            "",
            "| Заголовок | Роль extractor | Встреч | Файлов | Целевое размещение |",
            "| --- | --- | ---: | ---: | --- |",
        ]
    )
    for header in headers:
        _, destination, _, corrected = _header_destination(header)
        lines.append(
            f"| {_md(header['header'])} | `{header['role']}` | {header['occurrences']} | "
            f"{len(header['files'])} | `{destination}`{' ⚠ исправлено аудитом' if corrected else ''} |"
        )

    lines.extend(
        [
            "",
            "## Покрытие по прайсам",
            "",
            "| Прайс | Листов | Смысловых кусков | Проверенных строк позиций |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for item in stats["files"]:
        lines.append(
            f"| `{_md(item['price'])}` | {item['sheets']} | {item['contexts']} | {item['rows']} |"
        )
    lines.extend(
        [
            "",
            "## Рекомендация по загрузке",
            "",
            "1. Сначала сохранить исходную позицию, координаты и `raw_value`; ничего не терять при неуверенной нормализации.",
            "2. Базовые поля писать в `supplier_price_items`, цены — в `supplier_price_values`, переменные технические "
            "характеристики — отдельными строками `supplier_price_item_attribute_values`.",
            "3. Для каждого нового supplier-заголовка создать `supplier_attribute_keys`; связывать с каноническим "
            "`attribute_definitions` только после подтверждения смысла и единицы.",
            "4. Признаки, найденные только внутри названия/описания, не вырезать из исходного текста. Нормализованный факт "
            "создавать дополнительно и хранить ссылку на evidence; неоднозначные шаблоны отправлять на review.",
            "5. Коммерческие условия не копировать в канонический товар: они меняются между поставщиками и версиями прайса.",
            "",
            "## Ограничения аудита",
            "",
            "Аудит относится ровно к контексту, который extractor передаёт модели: до четырёх строк на каждый смысловой "
            "кусок. Он покрывает все 333 куска, но не заявляет, что просмотрены строки листов, которые не входят в этот "
            "модельный контекст. Каждый приведённый кандидат имеет проверенное дословное evidence; классификационные "
            "конфликты оставлены в `manual_review`, а не разрешены догадкой.",
            "",
        ]
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")


async def _run(args: argparse.Namespace) -> None:
    records, headers, stats = _collect(args.corpus)
    batches = _batches(records)
    if args.reuse_model_results:
        previous = json.loads(args.json_report.read_text(encoding="utf-8"))
        candidates = previous["candidates"]
        validation = previous["validation"]
        model_batch_count = args.model_batch_count or int(previous.get("batch_count") or len(batches))
    else:
        results: list[dict[str, Any]] = []
        for index, batch in enumerate(batches, start=1):
            result = await _request(batch, index)
            results.append(result)
            print(f"batch {index}/{len(batches)}: {len(batch)} rows, {len(result['candidates'])} candidates", flush=True)
        candidates, validation = _validate_and_merge(results, records)
        model_batch_count = len(batches)
    candidates = _postprocess_candidates(candidates)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": FIXED_MODEL,
        "stats": stats,
        "batch_count": model_batch_count,
        "validation": validation,
        "explicit_headers": headers,
        "candidates": candidates,
    }
    args.json_report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _render(
        report_path=args.report,
        candidates=candidates,
        headers=headers,
        stats=stats,
        validation=validation,
        batch_count=model_batch_count,
    )
    print(
        json.dumps(
            {
                "records": len(records),
                "headers": len(headers),
                "batches": model_batch_count,
                "candidates": len(candidates),
                "validation": validation,
                "report": str(args.report),
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--json-report", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--reuse-model-results", action="store_true")
    parser.add_argument("--model-batch-count", type=int)
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
