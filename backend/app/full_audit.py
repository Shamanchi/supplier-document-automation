"""Export and audit an imported price corpus without modifying parsed data.

The command joins one or more completed ``corpus_runs`` with the actual
PostgreSQL schema, exports all normalized entities, reconciles stored counters,
and emits a Russian Markdown audit.  It may inspect source files to create a
*candidate* mapping only when the imported version has no saved rule/mapping;
candidate mappings are deliberately never written back to the database.

Example (inside the compose network)::

    python -m app.full_audit \
      --run external=<uuid> --source external=/audit/external \
      --run original=<uuid> --source original=/audit/original \
      --output-dir /reports/full_price_audit
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import enum
import hashlib
import json
import sys
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select, text

from app.corpus_service import collect_files
from app.db import make_task_engine
from app.models import (
    AttributeDefinition,
    AttributeOption,
    CorpusRun,
    CorpusRunFile,
    ParseRule,
    PriceFile,
    PriceParseIssue,
    PriceVersion,
    Supplier,
    SupplierAttributeKey,
    SupplierPriceItem,
    SupplierPriceItemAttributeValue,
    SupplierPriceValue,
)
from app.parsing import loaders
from app.parsing.headers import detect_header, profile_price_columns
from app.parsing.mapping import ROLES, build_auto_mapping
from app.parsing.pipeline import parse_grids
from app.parsing.pdf_layout import find_builtin_pdf_layout_recipe, parse_pdf_layout
from app.parsing.rule_generation import (
    DEFAULT_RULE_MODEL,
    RULE_PROMPT_VERSION,
    prepare_rule_generation,
)
from app.parsing.rules import (
    compute_fingerprint,
    compute_header_hash,
    recipe_from_decision,
    validation_metrics,
)
from app.parsing.workbook_layout import (
    parse_workbook_layout,
)


TABLE_PURPOSES = {
    "suppliers": "Справочник поставщиков. Код выводится из имени файла в текущем corpus-importer.",
    "price_files": "Оригинальный файл в объектном хранилище и его технический паспорт (SHA-256, размер, MIME).",
    "price_versions": "Версия импорта, итоговый статус, показатели качества, применённая карта/правило, загрузчик и review-only prompt для уникальной структуры.",
    "supplier_price_items": "Нормализованные товарные строки версии с трассировкой до листа и строки источника.",
    "supplier_price_values": "Ценовые значения строк: тип цены, сумма, НДС, признак «по запросу» и подпись колонки.",
    "attribute_definitions": "Справочник смысла и типа характеристики; новая характеристика добавляется строкой, не миграцией БД.",
    "supplier_attribute_keys": "Сопоставление исходного заголовка конкретного поставщика с характеристикой из справочника.",
    "supplier_price_item_attribute_values": "Один атомарный факт характеристики позиции: raw_value и ровно одно типизированное скалярное поле.",
    "attribute_options": "Допустимые динамические варианты характеристик типа option; это не PostgreSQL ENUM.",
    "price_parse_issues": "Ошибки и предупреждения разбора с координатами исходной ячейки/строки.",
    "parse_rules": "Версионируемые правила: отпечаток структуры, рецепт маппинга, состояние и валидация.",
    "corpus_runs": "Запуск массового импорта и его агрегированные показатели.",
    "corpus_run_files": "Результат обработки каждого файла в конкретном массовом запуске.",
    "alembic_version": "Техническая версия миграций базы данных.",
}

ROLE_DESCRIPTIONS = {
    "name": "наименование товара (обязательно для товарной строки)",
    "article": "артикул/код товара",
    "manufacturer": "производитель — сохраняется как именованный технический атрибут",
    "description": "описание — сохраняется отдельным полем позиции",
    "attribute": "вариативная техническая характеристика — сохраняется с подписью исходной колонки",
    "unit": "единица измерения",
    "price": "цена; может иметь kind и vat_mode",
    "quantity_threshold": "порог количества — сохраняется как атрибут; пока не связывается с price tier",
    "discount_percent": "скидка — сохраняется как атрибут",
    "weight": "вес — сохраняется как атрибут",
    "volume": "объём — сохраняется как атрибут",
    "dimensions": "габариты — сохраняются в поле dimensions",
    "stock": "остаток — сохраняется как атрибут",
    "lead_time": "срок поставки — сохраняется как атрибут",
    "category": "категория/раздел",
    "photo": "ссылка на фото — сохраняется как атрибут",
    "row_number": "технический номер строки",
    "ignore": "столбец игнорируется",
}


def as_json(value: Any) -> Any:
    """Convert SQLAlchemy/standard scalar values into JSON-safe values."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): as_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [as_json(item) for item in value]
    return value


def model_dict(model: Any) -> dict[str, Any]:
    return {column.name: as_json(getattr(model, column.name)) for column in model.__table__.columns}


def parse_label_value(value: str, option: str) -> tuple[str, str]:
    """Parse one CLI assignment, keeping paths containing spaces intact."""
    label, separator, raw_value = value.partition("=")
    label = label.strip()
    raw_value = raw_value.strip()
    if not separator:
        raise ValueError(f"{option} должен иметь формат LABEL=VALUE")
    if not label or not raw_value:
        raise ValueError(f"{option}: label и значение не может быть пустым")
    return label, raw_value


def parse_assignments(values: list[str], option: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_value in values:
        label, value = parse_label_value(raw_value, option)
        if label in result:
            raise ValueError(f"{option}: label «{label}» указан дважды")
        result[label] = value
    return result


def column_letter(index: int) -> str:
    """Return an Excel-like column reference for a zero-based index."""
    if index < 0:
        return "?"
    result = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def recipe_columns(recipe: dict | None) -> list[dict[str, Any]]:
    """Flatten a rule recipe into readable source-column mappings."""
    rows: list[dict[str, Any]] = []
    if not isinstance(recipe, dict):
        return rows
    if recipe.get("format") == "pdf_layout_v1":
        for region_index, region in enumerate(recipe.get("regions", []), start=1):
            if not isinstance(region, dict):
                continue
            region_name = str(region.get("name") or f"region-{region_index}")
            for column_index, column in enumerate(region.get("columns", [])):
                if not isinstance(column, dict):
                    continue
                descriptor = column.get("price_descriptor")
                descriptor = descriptor if isinstance(descriptor, dict) else {}
                x_range = column.get("x_range")
                if isinstance(x_range, (list, tuple)) and len(x_range) == 2:
                    column_ref = f"X {x_range[0]}–{x_range[1]}"
                else:
                    column_ref = "X —"
                rows.append(
                    {
                        "sheet": f"layout:{region_name}",
                        "column_index": column_index,
                        "column_ref": column_ref,
                        "header": column.get("header") or column.get("role") or "ignore",
                        "role": column.get("role") or "ignore",
                        "price_kind": descriptor.get("kind"),
                        "vat_mode": descriptor.get("vat_mode"),
                    }
                )
        return rows
    if recipe.get("format") == "workbook_layout_v1":
        layout_name = str(recipe.get("name") or "workbook_layout_v1")
        return [
            {
                "sheet": f"layout:{layout_name}",
                "column_index": 0,
                "column_ref": "панели/матрицы",
                "header": "Изолированные name → price области",
                "role": "name + price",
                "price_kind": "other",
                "vat_mode": "unknown",
            }
        ]
    for sheet in recipe.get("sheets", []):
        if not isinstance(sheet, dict) or sheet.get("skip"):
            continue
        match = sheet.get("match") if isinstance(sheet.get("match"), dict) else {}
        sheet_name = str(match.get("name_pattern") or f"#лист-{match.get('or_index', 0)}")
        for column in sheet.get("columns", []):
            if not isinstance(column, dict):
                continue
            try:
                index = int(column.get("index"))
            except (TypeError, ValueError):
                continue
            descriptor = column.get("price_descriptor")
            descriptor = descriptor if isinstance(descriptor, dict) else {}
            rows.append(
                {
                    "sheet": sheet_name,
                    "column_index": index,
                    "column_ref": column_letter(index),
                    "header": column.get("header") or None,
                    "role": column.get("role") or "ignore",
                    "price_kind": descriptor.get("kind"),
                    "vat_mode": descriptor.get("vat_mode"),
                }
            )
    return sorted(rows, key=lambda row: (row["sheet"], row["column_index"]))


def mapping_as_recipe(mapping: dict | None) -> dict | None:
    """Make a version.mapping payload readable through ``recipe_columns``."""
    if not isinstance(mapping, dict):
        return None
    sheets: list[dict[str, Any]] = []
    for index, table in enumerate(mapping.get("tables", [])):
        if not isinstance(table, dict):
            continue
        sheets.append(
            {
                "match": {"name_pattern": table.get("sheet_name", ""), "or_index": index},
                "header_row": table.get("header_row_index"),
                "columns": table.get("columns", []),
            }
        )
    return {"sheets": sheets, "options": None}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(sources: dict[str, str]) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    for label, raw_path in sorted(sources.items()):
        root = Path(raw_path)
        if not root.is_dir():
            raise ValueError(f"Источник «{label}» не является каталогом: {root}")
        files = collect_files(root)
        print(f"Источник {label}: отобрано файлов прайсов: {len(files)}", flush=True)
        for path in files:
            manifest.append(
                {
                    "source": label,
                    "relative_path": str(path.relative_to(root)),
                    "filename": path.name,
                    "extension": path.suffix.lower(),
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                    "_path": path,
                }
            )
    return sorted(manifest, key=lambda row: (row["source"], row["relative_path"]))


async def load_snapshot(run_labels: dict[str, uuid.UUID]) -> dict[str, Any]:
    """Load a stable, read-only database snapshot for the requested runs."""
    engine, session_factory = make_task_engine()
    run_ids = list(run_labels.values())
    label_by_id = {str(run_id): label for label, run_id in run_labels.items()}
    try:
        async with session_factory() as session:
            schema_rows = [
                dict(row._mapping)
                for row in (
                    await session.execute(
                        text(
                            "SELECT table_name, column_name, data_type, udt_name, "
                            "is_nullable, column_default, ordinal_position "
                            "FROM information_schema.columns "
                            "WHERE table_schema = 'public' "
                            "ORDER BY table_name, ordinal_position"
                        )
                    )
                ).all()
            ]
            runs = (
                await session.execute(select(CorpusRun).where(CorpusRun.id.in_(run_ids)))
            ).scalars().all()
            found_ids = {run.id for run in runs}
            missing = [str(run_id) for run_id in run_ids if run_id not in found_ids]
            if missing:
                raise ValueError(f"В БД не найдены corpus_run: {', '.join(missing)}")

            joined_rows = (
                await session.execute(
                    select(CorpusRunFile, PriceVersion, PriceFile, Supplier)
                    .outerjoin(PriceVersion, CorpusRunFile.price_version_id == PriceVersion.id)
                    .outerjoin(PriceFile, PriceVersion.price_file_id == PriceFile.id)
                    .outerjoin(Supplier, PriceVersion.supplier_id == Supplier.id)
                    .where(CorpusRunFile.corpus_run_id.in_(run_ids))
                    .order_by(CorpusRunFile.corpus_run_id, CorpusRunFile.filename, CorpusRunFile.created_at)
                )
            ).all()

            all_rules = (
                await session.execute(select(ParseRule).order_by(ParseRule.created_at, ParseRule.id))
            ).scalars().all()

            versions_by_id: dict[str, PriceVersion] = {}
            files_by_id: dict[str, PriceFile] = {}
            suppliers_by_id: dict[str, Supplier] = {}
            run_records: list[dict[str, Any]] = []
            for run_file, version, price_file, supplier in joined_rows:
                if version is not None:
                    versions_by_id[str(version.id)] = version
                if price_file is not None:
                    files_by_id[str(price_file.id)] = price_file
                if supplier is not None:
                    suppliers_by_id[str(supplier.id)] = supplier
                run_records.append(
                    {
                        "run_label": label_by_id[str(run_file.corpus_run_id)],
                        "run_file": model_dict(run_file),
                        "price_version": model_dict(version) if version is not None else None,
                        "price_file": model_dict(price_file) if price_file is not None else None,
                        "supplier": model_dict(supplier) if supplier is not None else None,
                    }
                )

            version_ids = [version.id for version in versions_by_id.values()]
            items: list[SupplierPriceItem] = []
            values: list[tuple[SupplierPriceValue, uuid.UUID]] = []
            attribute_values: list[tuple[SupplierPriceItemAttributeValue, uuid.UUID]] = []
            issues: list[PriceParseIssue] = []
            definitions: list[AttributeDefinition] = []
            source_keys: list[SupplierAttributeKey] = []
            attribute_options: list[AttributeOption] = []
            if version_ids:
                items = (
                    await session.execute(
                        select(SupplierPriceItem)
                        .where(SupplierPriceItem.price_version_id.in_(version_ids))
                        .order_by(SupplierPriceItem.price_version_id, SupplierPriceItem.source_sheet, SupplierPriceItem.source_row)
                    )
                ).scalars().all()
                values = (
                    await session.execute(
                        select(SupplierPriceValue, SupplierPriceItem.price_version_id)
                        .join(SupplierPriceItem, SupplierPriceValue.item_id == SupplierPriceItem.id)
                        .where(SupplierPriceItem.price_version_id.in_(version_ids))
                        .order_by(SupplierPriceItem.price_version_id, SupplierPriceValue.item_id)
                    )
                ).all()
                attribute_values = (
                    await session.execute(
                        select(
                            SupplierPriceItemAttributeValue,
                            SupplierPriceItem.price_version_id,
                        )
                        .join(
                            SupplierPriceItem,
                            SupplierPriceItemAttributeValue.item_id == SupplierPriceItem.id,
                        )
                        .where(SupplierPriceItem.price_version_id.in_(version_ids))
                        .order_by(
                            SupplierPriceItem.price_version_id,
                            SupplierPriceItemAttributeValue.item_id,
                            SupplierPriceItemAttributeValue.source_column_index,
                            SupplierPriceItemAttributeValue.ordinal,
                        )
                    )
                ).all()
                definition_ids = {
                    value.attribute_definition_id for value, _version_id in attribute_values
                }
                source_key_ids = {value.source_attribute_key_id for value, _version_id in attribute_values}
                option_ids = {
                    value.option_id
                    for value, _version_id in attribute_values
                    if value.option_id is not None
                }
                if definition_ids:
                    definitions = (
                        await session.execute(
                            select(AttributeDefinition)
                            .where(AttributeDefinition.id.in_(definition_ids))
                            .order_by(AttributeDefinition.code)
                        )
                    ).scalars().all()
                if source_key_ids:
                    source_keys = (
                        await session.execute(
                            select(SupplierAttributeKey)
                            .where(SupplierAttributeKey.id.in_(source_key_ids))
                            .order_by(SupplierAttributeKey.supplier_id, SupplierAttributeKey.normalized_header)
                        )
                    ).scalars().all()
                if option_ids:
                    attribute_options = (
                        await session.execute(
                            select(AttributeOption)
                            .where(AttributeOption.id.in_(option_ids))
                            .order_by(AttributeOption.attribute_definition_id, AttributeOption.code)
                        )
                    ).scalars().all()
                issues = (
                    await session.execute(
                        select(PriceParseIssue)
                        .where(PriceParseIssue.price_version_id.in_(version_ids))
                        .order_by(PriceParseIssue.price_version_id, PriceParseIssue.severity, PriceParseIssue.row_no)
                    )
                ).scalars().all()

            return {
                "schema": [as_json(row) for row in schema_rows],
                "runs": [
                    {"run_label": label_by_id[str(run.id)], **model_dict(run)}
                    for run in sorted(runs, key=lambda item: label_by_id[str(item.id)])
                ],
                "run_records": run_records,
                "versions": [model_dict(item) for item in versions_by_id.values()],
                "price_files": [model_dict(item) for item in files_by_id.values()],
                "suppliers": [model_dict(item) for item in suppliers_by_id.values()],
                "items": [model_dict(item) for item in items],
                "values": [
                    {**model_dict(item), "price_version_id": str(version_id)}
                    for item, version_id in values
                ],
                "attribute_values": [
                    {**model_dict(item), "price_version_id": str(version_id)}
                    for item, version_id in attribute_values
                ],
                "attribute_definitions": [model_dict(item) for item in definitions],
                "supplier_attribute_keys": [model_dict(item) for item in source_keys],
                "attribute_options": [model_dict(item) for item in attribute_options],
                "issues": [model_dict(item) for item in issues],
                "all_rules": [model_dict(item) for item in all_rules],
            }
    finally:
        await engine.dispose()


def inspect_candidate(path: Path) -> dict[str, Any]:
    """Produce a non-persistent candidate rule for a file without one."""
    try:
        data = path.read_bytes()
        loader = loaders.detect_loader(data, path.name)
        if loader == loaders.LOADER_PDF_SCAN:
            return {
                "state": "needs_ocr",
                "loader": loader,
                "message": (
                    "Файл распознан как сканированный PDF; до OCR правило и "
                    "model prompt не строятся."
                ),
            }
        if loader == loaders.LOADER_PDF_TEXT:
            builtin_recipe = find_builtin_pdf_layout_recipe(data, path.name)
            if builtin_recipe is not None:
                result = parse_pdf_layout(data, builtin_recipe)
                validation = validation_metrics(result)
                if validation["passed"]:
                    return {
                        "state": "candidate_layout",
                        "loader": loader,
                        "message": (
                            "Найдено встроенное layout-правило; dry-run прошёл, "
                            "правило не записывалось в БД аудитом."
                        ),
                        "fingerprint": {
                            "loader": loader,
                            "layout_required_text": builtin_recipe.get("match", {}).get(
                                "required_text", []
                            ),
                        },
                        "recipe": builtin_recipe,
                        "columns": recipe_columns(builtin_recipe),
                        "validation": validation,
                    }
        try:
            grids = loaders.load_grids_for(loader, data, path.name)
        except loaders.PdfNoTablesError as exc:
            if loader != loaders.LOADER_PDF_TEXT:
                raise
            generation = prepare_rule_generation(
                data=data,
                filename=path.name,
                loader=loader,
                grids=[],
                failure=f"табличный extractor не дал структуры: {exc}",
            )
            return {
                "state": "complex",
                "loader": loader,
                "message": (
                    "Текстовый PDF не дал безопасной таблицы; направлен в complex, "
                    "подготовлен bounded prompt для layout-правила."
                ),
                "rule_generation": generation,
            }
        if loader in {loaders.LOADER_XLS, loaders.LOADER_XLSX}:
            builtin_recipe = None  # встроенных профилей нет: правила живут в parse_rules
            if builtin_recipe is not None:
                result = parse_workbook_layout(grids, builtin_recipe)
                validation = validation_metrics(result)
                if validation["passed"]:
                    return {
                        "state": "candidate_layout",
                        "loader": loader,
                        "message": (
                            "Найдено встроенное workbook-layout-правило; dry-run прошёл, "
                            "правило не записывалось в БД аудитом."
                        ),
                        "fingerprint": {
                            "loader": loader,
                            "layout_profile": builtin_recipe.get("name"),
                        },
                        "recipe": builtin_recipe,
                        "columns": recipe_columns(builtin_recipe),
                        "validation": validation,
                    }
        decision = build_auto_mapping(grids)
        candidate_result = parse_grids(grids, decision)
        candidate_validation = validation_metrics(candidate_result)
        fingerprint = compute_fingerprint(loader, grids)
        recipe = recipe_from_decision(decision, grids) if decision.tables else None
        observed_columns: list[dict[str, Any]] = []
        for grid in grids:
            detection = detect_header(grid)
            if detection.header_row_index is None:
                continue
            profile_price_columns(grid, detection)
            for suggestion in detection.columns:
                if suggestion.role == "ignore":
                    continue
                descriptor = suggestion.price_descriptor or {}
                observed_columns.append(
                    {
                        "sheet": grid.name,
                        "header_row": detection.header_row_index,
                        "column_index": suggestion.index,
                        "column_ref": column_letter(suggestion.index),
                        "header": suggestion.header,
                        "suggested_role": suggestion.role,
                        "confidence": suggestion.confidence,
                        "price_kind": descriptor.get("kind"),
                        "vat_mode": descriptor.get("vat_mode"),
                    }
                )
        auto_candidate_is_safe = decision.auto_ok and candidate_validation["passed"]
        is_complex_pdf = loader == loaders.LOADER_PDF_TEXT and not auto_candidate_is_safe
        candidate = {
            "state": "candidate_auto" if auto_candidate_is_safe else (
                "complex" if is_complex_pdf else "needs_manual_mapping"
            ),
            "loader": loader,
            "message": (
                "Кандидат построен автоматически и не сохранён в БД."
                if auto_candidate_is_safe
                else (
                    "Текстовый PDF не дал безопасного разбора; направлен в complex, "
                    "подготовлен bounded prompt для layout-правила."
                    if is_complex_pdf
                    else "Автоматический разбор не прошёл проверку качества; подготовлен prompt для генерации правила."
                )
            ),
            "fingerprint": fingerprint,
            "header_hash": compute_header_hash(fingerprint),
            "recipe": recipe,
            "columns": recipe_columns(recipe),
            "observed_columns": observed_columns,
            "non_price_sheets": decision.non_price_sheets,
            "validation": candidate_validation,
        }
        if not auto_candidate_is_safe:
            candidate["rule_generation"] = prepare_rule_generation(
                data=data,
                filename=path.name,
                loader=loader,
                grids=grids,
                failure=(
                    "детерминированный маппер не дал проверяемый набор товарных строк"
                ),
                observed_columns=observed_columns,
            )
        return candidate
    except (loaders.UnknownFormatError, loaders.PdfNoTablesError, loaders.DocumentConversionError) as exc:
        return {"state": "not_available", "message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - audit must continue across a bad source file
        return {"state": "not_available", "message": f"{type(exc).__name__}: {exc}"}


def resolve_persisted_mapping(
    record: dict[str, Any] | None, rules_by_id: dict[str, dict[str, Any]]
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    """Return source label, effective recipe, and the referenced full rule."""
    if record is None:
        return "not_available", None, None
    rule_ids: list[str] = []
    run_rule_id = (record.get("run_file") or {}).get("parse_rule_id")
    version_rule_id = (record.get("price_version") or {}).get("parse_rule_id")
    for rule_id in (run_rule_id, version_rule_id):
        if rule_id and rule_id not in rule_ids:
            rule_ids.append(rule_id)
    for rule_id in rule_ids:
        rule = rules_by_id.get(rule_id)
        if rule is not None:
            return "persisted_rule", rule.get("recipe"), rule
    mapping = (record.get("price_version") or {}).get("mapping")
    recipe = mapping_as_recipe(mapping)
    if recipe and recipe.get("sheets"):
        return "persisted_mapping", recipe, None
    return "not_available", None, None


def build_reconciliation(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    item_counts = Counter(item["price_version_id"] for item in snapshot["items"])
    value_counts = Counter(value["price_version_id"] for value in snapshot["values"])
    attribute_value_counts = Counter(
        value["price_version_id"] for value in snapshot["attribute_values"]
    )
    issue_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for issue in snapshot["issues"]:
        issue_counts[issue["price_version_id"]][issue["severity"]] += 1
    rows: list[dict[str, Any]] = []
    for version in sorted(snapshot["versions"], key=lambda item: item["id"]):
        version_id = version["id"]
        persisted_items = item_counts[version_id]
        persisted_errors = issue_counts[version_id]["error"]
        persisted_warnings = issue_counts[version_id]["warning"]
        rows.append(
            {
                "price_version_id": version_id,
                "status": version["status"],
                "declared_item_count": version["item_count"],
                "persisted_item_count": persisted_items,
                "item_count_matches": version["item_count"] == persisted_items,
                "declared_rows_accepted": version["rows_accepted"],
                "rows_accepted_matches_item_count": version["rows_accepted"] == persisted_items,
                "persisted_price_values": value_counts[version_id],
                "persisted_attribute_values": attribute_value_counts[version_id],
                "declared_errors_count": version["errors_count"],
                "persisted_errors_count": persisted_errors,
                "errors_count_matches": version["errors_count"] == persisted_errors,
                "declared_warnings_count": version["warnings_count"],
                "persisted_warnings_count": persisted_warnings,
                "warnings_count_matches": version["warnings_count"] == persisted_warnings,
            }
        )
    return rows


def build_field_coverage(snapshot: dict[str, Any]) -> dict[str, Any]:
    datasets = {
        "suppliers": snapshot["suppliers"],
        "price_files": snapshot["price_files"],
        "price_versions": snapshot["versions"],
        "supplier_price_items": snapshot["items"],
        "supplier_price_values": snapshot["values"],
        "attribute_definitions": snapshot["attribute_definitions"],
        "supplier_attribute_keys": snapshot["supplier_attribute_keys"],
        "supplier_price_item_attribute_values": snapshot["attribute_values"],
        "attribute_options": snapshot["attribute_options"],
        "price_parse_issues": snapshot["issues"],
        "parse_rules": snapshot["all_rules"],
        "corpus_runs": snapshot["runs"],
        "corpus_run_files": [record["run_file"] for record in snapshot["run_records"]],
    }
    result: dict[str, Any] = {}
    for table, rows in datasets.items():
        fields = sorted({field for row in rows for field in row})
        result[table] = {
            "rows": len(rows),
            "non_null_by_field": {
                field: sum(1 for row in rows if row.get(field) is not None) for field in fields
            },
        }
    return result


def build_audits(
    manifest: list[dict[str, Any]], snapshot: dict[str, Any], reconciliation: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    records_by_source_sha: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in snapshot["run_records"]:
        sha256 = record["run_file"].get("sha256")
        if sha256:
            records_by_source_sha[(record["run_label"], sha256)].append(record)
    reconciliation_by_version = {row["price_version_id"]: row for row in reconciliation}
    rules_by_id = {rule["id"]: rule for rule in snapshot["all_rules"]}
    audits: list[dict[str, Any]] = []
    for position, source_file in enumerate(manifest, start=1):
        matches = records_by_source_sha.get((source_file["source"], source_file["sha256"]), [])
        record = matches[0] if matches else None
        mapping_source, recipe, rule = resolve_persisted_mapping(record, rules_by_id)
        if recipe is not None:
            candidate = {
                "state": "not_run",
                "message": "Не строился: для версии уже есть сохранённое правило или маппинг.",
            }
        else:
            print(
                f"Кандидат правила {position}/{len(manifest)}: {source_file['source']}/{source_file['relative_path']}",
                flush=True,
            )
            candidate = inspect_candidate(source_file["_path"])
            if candidate.get("recipe"):
                recipe = candidate["recipe"]
                mapping_source = (
                    "candidate_layout"
                    if candidate.get("state") == "candidate_layout"
                    else "candidate_auto"
                )
        version = record.get("price_version") if record else None
        version_id = version.get("id") if version else None
        audits.append(
            {
                "source": source_file["source"],
                "relative_path": source_file["relative_path"],
                "filename": source_file["filename"],
                "extension": source_file["extension"],
                "size_bytes": source_file["size_bytes"],
                "sha256": source_file["sha256"],
                "import_record_found": record is not None,
                "run_file": record.get("run_file") if record else None,
                "supplier": record.get("supplier") if record else None,
                "price_file": record.get("price_file") if record else None,
                "price_version": version,
                "mapping_source": mapping_source,
                "effective_recipe": recipe,
                "effective_columns": recipe_columns(recipe),
                "referenced_parse_rule": rule,
                "candidate": candidate,
                "reconciliation": reconciliation_by_version.get(version_id) if version_id else None,
            }
        )
    return audits


def csv_cell(value: Any) -> str:
    value = as_json(value)
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(as_json(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = sorted({field for row in rows for field in row})
    with path.open("w", encoding="utf-8-sig", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_cell(row.get(field)) for field in fieldnames})


def write_rule_prompt_artifacts(output_dir: Path, audits: list[dict[str, Any]]) -> None:
    """Write prepared model prompts as reviewable, non-persistent artifacts."""
    prompts: dict[str, dict[str, Any]] = {}
    for audit in audits:
        generation = (audit.get("candidate") or {}).get("rule_generation") or {}
        prompt_key = generation.get("prompt_id")
        prompt = generation.get("prompt")
        if prompt_key and prompt:
            prompts[str(prompt_key)] = generation
    if not prompts:
        return

    prompt_dir = output_dir / "rule_prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    for prompt_key, generation in prompts.items():
        prompt_dir.joinpath(f"{prompt_key}.md").write_text(
            "# Prompt генерации правила\n\n"
            f"- Модель: `{generation.get('model', DEFAULT_RULE_MODEL)}`\n"
            f"- Prompt ID: `{prompt_key}`\n\n"
            "```text\n"
            f"{str(generation['prompt']).rstrip()}\n"
            "```\n",
            encoding="utf-8",
        )


def md_cell(value: Any) -> str:
    if value is None or value == "":
        return "—"
    text_value = str(value).replace("|", "\\|").replace("\n", "<br>")
    return text_value


def md_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    if not rows:
        return ["_Нет строк._", ""]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(md_cell(value) for value in row) + " |")
    lines.append("")
    return lines


def audit_markdown(audits: list[dict[str, Any]]) -> str:
    lines = [
        "# Аудит маппинга по каждому прайсу",
        "",
        "Этот файл содержит итог импорта и эффективный маппинг каждого отобранного прайса.",
        "`candidate_auto` — предложение анализатора; оно **не записано в БД и не активно**.",
        "",
    ]
    for index, audit in enumerate(audits, start=1):
        run_file = audit.get("run_file") or {}
        version = audit.get("price_version") or {}
        rule = audit.get("referenced_parse_rule") or {}
        candidate = audit.get("candidate") or {}
        lines.extend(
            [
                f"## {index}. {audit['source']}/{audit['relative_path']}",
                "",
                f"- SHA-256: `{audit['sha256']}`",
                f"- Размер: {audit['size_bytes']} байт; расширение: `{audit['extension'] or 'без расширения'}`",
                f"- Импорт: `{run_file.get('status', 'нет записи')}`; загрузчик: `{run_file.get('loader') or version.get('loader') or '—'}`",
                f"- Версия БД: `{version.get('id') or '—'}`; товаров: {version.get('item_count', '—')}; качество: {version.get('quality_score', '—')}",
                f"- Источник маппинга: `{audit['mapping_source']}`",
            ]
        )
        if rule:
            lines.append(
                f"- Правило: `{rule.get('id')}` — {rule.get('name')}; статус `{rule.get('status')}`, revision {rule.get('revision')}"
            )
        if candidate.get("state") not in (None, "not_run"):
            lines.append(f"- Кандидат: `{candidate.get('state')}` — {candidate.get('message', '—')}")
        reconciliation = audit.get("reconciliation")
        if reconciliation:
            lines.append(
                "- Сверка выгрузки: "
                f"товары {reconciliation['persisted_item_count']}/{reconciliation['declared_item_count']}; "
                f"значения цен {reconciliation['persisted_price_values']}; "
                f"ошибки {reconciliation['persisted_errors_count']}/{reconciliation['declared_errors_count']}; "
                f"предупреждения {reconciliation['persisted_warnings_count']}/{reconciliation['declared_warnings_count']}."
            )
        lines.append("")
        columns = audit["effective_columns"]
        if columns:
            lines.append("### Поля источника → роль правила")
            lines.append("")
            lines.extend(
                md_table(
                    ["Лист", "Колонка", "Заголовок", "Роль", "Тип цены", "НДС"],
                    [
                        [
                            column["sheet"],
                            f"{column['column_ref']} ({column['column_index']})",
                            column["header"],
                            column["role"],
                            column["price_kind"],
                            column["vat_mode"],
                        ]
                        for column in columns
                    ],
                )
            )
        else:
            observed_columns = candidate.get("observed_columns") or []
            candidate_state = candidate.get("state")
            if candidate_state == "complex":
                no_columns_message = (
                    "_Текстовый слой найден, но безопасного правила нет: файл "
                    "направлен в очередь complex. Используйте подготовленный "
                    "bounded prompt и проверьте полученный рецепт dry-run._"
                )
            elif candidate_state == "needs_ocr":
                no_columns_message = (
                    "_Текстовый слой отсутствует: файл направлен в need_ocr; "
                    "правило и model prompt до OCR не строятся._"
                )
            else:
                no_columns_message = (
                    "_Нет применимого табличного правила. Требуется ручной "
                    "маппинг или исправление формата._"
                )
            lines.extend(
                [
                    "### Поля источника → роль правила",
                    "",
                    no_columns_message,
                    "",
                ]
            )
            if observed_columns:
                lines.append("### Найденная шапка для ручного маппинга")
                lines.append("")
                lines.extend(
                    md_table(
                        ["Лист", "Строка шапки", "Колонка", "Заголовок", "Предложенная роль", "Уверенность"],
                        [
                            [
                                column["sheet"],
                                column["header_row"],
                                f"{column['column_ref']} ({column['column_index']})",
                                column["header"],
                                column["suggested_role"],
                                column["confidence"],
                            ]
                            for column in observed_columns
                        ],
                    )
                )
            generation = candidate.get("rule_generation") or {}
            if generation.get("prompt_id"):
                lines.extend(
                    [
                        "### Fallback генерации правила",
                        "",
                        f"Подготовлен bounded prompt для модели `{generation.get('model', DEFAULT_RULE_MODEL)}`; "
                        "ответ должен пройти схему и dry-run, автоматической активации нет.",
                        "",
                        f"Полный prompt: [rule_prompts/{generation['prompt_id']}.md](rule_prompts/{generation['prompt_id']}.md)",
                        "",
                    ]
                )
    return "\n".join(lines) + "\n"


def rule_guide_markdown() -> str:
    roles = "\n".join(f"- `{role}` — {description}" for role, description in ROLE_DESCRIPTIONS.items())
    return f'''# Формат и сопровождение правил прайсов

## Два связанных формата

`price_versions.mapping` — разовый подтверждённый маппинг конкретной версии. Он имеет короткую форму:

```json
{{
  "tables": [
    {{
      "sheet_name": "Прайс",
      "columns": [
        {{"index": 0, "role": "article"}},
        {{"index": 1, "role": "name"}},
        {{"index": 2, "role": "unit"}},
        {{"index": 3, "role": "price", "price_descriptor": {{"kind": "wholesale", "vat_mode": "included"}}}}
      ]
    }}
  ]
}}
```

После успешного разбора подтверждённый маппинг становится записью `parse_rules` с рецептом. Правило — переносимый формат, по которому система ищет аналогичную структуру в следующем прайсе:

```json
{{
  "name": "ООО Пример — прайс XLSX",
  "supplier_id": "UUID поставщика или null для глобального правила",
  "loader": "xlsx",
  "fingerprint": {{
    "loader": "xlsx",
    "sheet_signatures": [
      {{
        "sheet_name": "прайс",
        "header_row": 0,
        "headers": ["артикул", "наименование", "ед изм", "цена опт"]
      }}
    ]
  }},
  "header_hash": "SHA-256 отпечатка; сервер может вычислить сам",
  "recipe": {{
    "sheets": [
      {{
        "match": {{"name_pattern": "Прайс", "or_index": 0}},
        "skip": false,
        "header_row": 0,
        "columns": [
          {{"index": 0, "role": "article", "header": "артикул"}},
          {{"index": 1, "role": "name", "header": "наименование"}},
          {{"index": 2, "role": "unit", "header": "ед изм"}},
          {{
            "index": 3,
            "role": "price",
            "header": "цена опт",
            "price_descriptor": {{"kind": "wholesale", "vat_mode": "included"}}
          }}
        ]
      }}
    ],
    "options": {{
      "repeat_headers": "skip",
      "category_rows": "context",
      "default_unit": null,
      "currency": "RUB"
    }}
  }}
}}
```

Все `index` нулевые: `0` — колонка A, `1` — B. `header` — нормализованный текст заголовка, он помогает переориентировать частично похожее правило. `name_pattern` на текущей реализации сравнивается с нормализованным именем листа **точно**, это не шаблон с `*`.

## Допустимые роли колонок

{roles}

## Жизненный цикл правила

1. Импорт без совпадения запускает автоматический анализ. При качественном результате (есть строки, не менее 95% из них имеют распознанную цену, нет `PRICE_PARSE_ERROR` и `NO_NAME`) система создаёт `draft`-правило с provenance `auto`.
2. Оператор проверяет предварительный просмотр, при необходимости сохраняет маппинг версии через `PUT /api/v1/price-versions/{{version_id}}/mapping` и запускает повторный разбор.
3. Для нового ручного правила используется `POST /api/v1/parse-rules`; сервер создаёт `draft`, а `loader` должен быть одним из `xlsx`, `xls`, `pdf_text`, `docx`, `doc`, `txt`.
4. Перед активацией вызвать `POST /api/v1/parse-rules/{{rule_id}}/validate` с `{{"price_version_id": "..."}}`. Валидация разбирает сохранённый оригинал без перезаписи товарных строк и сохраняет метрики в `validation`.
5. Только после успешной проверки вызвать `POST /api/v1/parse-rules/{{rule_id}}/activate`. Параметр `?force=true` обходит защиту и потому годится лишь для осознанного ручного решения.
6. Ненужное правило выключается через `POST /api/v1/parse-rules/{{rule_id}}/deprecate`.

## Как исправлять существующее правило

- Получить правило: `GET /api/v1/parse-rules/{{rule_id}}`.
- Изменить имя, поставщика или `recipe`: `PUT /api/v1/parse-rules/{{rule_id}}`.
- После изменения сервис увеличивает `revision` и очищает `validation`, поэтому правило надо снова валидировать.
- Важно: `PUT` обновляет **ту же запись**, а не сохраняет отдельный неизменяемый снимок старого рецепта. Перед существенной правкой экспортируйте `all_parse_rules.json` из аудита или создайте новое правило как резервную копию.
- `options` сохраняются в рецепте, но текущий executor использует фактически логику колонок/листов; не рассчитывайте на `currency`, `default_unit`, `repeat_headers` или `category_rows` как на настраиваемые переключатели без доработки кода.

## Как завести правило для нового прайса

1. Сначала импортируйте один типичный файл, не активируя автоматически сгенерированное правило.
2. Откройте `mapping-preview`, найдите строку шапки и назначьте минимум `name` и одну `price`. Добавьте `article`, `unit`, `category` при наличии.
3. Для каждой цены задайте `price_descriptor.kind` (`wholesale`, `retail`, `dealer`, `purchase`, `other`) и `vat_mode` (`included`, `excluded`, `unknown`).
4. Сохраните маппинг версии, дождитесь повторного разбора, проверьте выгруженные `supplier_price_items.csv`, `supplier_price_values.csv` и `price_parse_issues.csv`.
5. Провалидируйте правило минимум на одном, а лучше на двух прайсах того же поставщика. Только затем активируйте его.
6. При изменении структуры (новые столбцы, другое имя листа) ожидайте partial-match или отсутствие совпадения: проверьте новый маппинг, не включайте правило вслепую.
'''


def main_report_markdown(
    snapshot: dict[str, Any], manifest: list[dict[str, Any]], audits: list[dict[str, Any]],
    reconciliation: list[dict[str, Any]], coverage: dict[str, Any], output_dir: Path,
) -> str:
    status_counts = Counter(
        (audit.get("run_file") or {}).get("status", "not_imported") for audit in audits
    )
    mapping_counts = Counter(audit["mapping_source"] for audit in audits)
    mismatches = [
        row for row in reconciliation
        if not all(
            row[key]
            for key in (
                "item_count_matches",
                "rows_accepted_matches_item_count",
                "errors_count_matches",
                "warnings_count_matches",
            )
        )
    ]
    manifest_by_source = Counter(item["source"] for item in manifest)
    schema_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in snapshot["schema"]:
        schema_by_table[row["table_name"]].append(row)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        "# Полный аудит импорта и экспорта прайсов",
        "",
        f"Сформирован: {now}",
        "",
        "## Итог",
        "",
        f"- Отобрано исходных файлов: **{len(manifest)}** (" + ", ".join(
            f"{label}: {count}" for label, count in sorted(manifest_by_source.items())
        ) + ").",
        f"- Запусков корпуса в аудите: **{len(snapshot['runs'])}**; строк запусков: **{len(snapshot['run_records'])}**.",
        f"- Нормализованных товарных строк выгружено: **{len(snapshot['items'])}**; ценовых значений: **{len(snapshot['values'])}**; диагностик: **{len(snapshot['issues'])}**.",
        f"- Несовпадений внутренних счётчиков импорта с выгрузкой: **{len(mismatches)}**.",
        "",
        "Проверка счётчиков доказывает, что материализованные сущности не теряются между итогом версии и экспортом БД. Она не означает, что каждая ячейка исходника уже имеет отдельное поле: ограничения модели перечислены ниже.",
        "",
        "### Результаты импортов",
        "",
    ]
    lines.extend(md_table(["Статус строки запуска", "Файлов"], [[status, count] for status, count in sorted(status_counts.items())]))
    lines.append("### Источник маппинга")
    lines.append("")
    lines.extend(md_table(["Источник", "Файлов"], [[source, count] for source, count in sorted(mapping_counts.items())]))
    lines.extend(
        [
            "## 1. Поля в фактической базе данных",
            "",
            "Ниже — схема, прочитанная из `information_schema` работающей PostgreSQL, а не только из исходного кода. Полный машиночитаемый экспорт: `database_schema.json` и `database_schema.csv`.",
            "",
        ]
    )
    for table_name in sorted(schema_by_table):
        lines.append(f"### `{table_name}`")
        lines.append("")
        if table_name in TABLE_PURPOSES:
            lines.append(TABLE_PURPOSES[table_name])
            lines.append("")
        lines.extend(
            md_table(
                ["Поле", "Тип PostgreSQL", "NULL", "По умолчанию"],
                [
                    [row["column_name"], f"{row['data_type']} ({row['udt_name']})", row["is_nullable"], row["column_default"]]
                    for row in schema_by_table[table_name]
                ],
            )
        )
    lines.extend(
        [
            "### Заполненность выгруженных полей",
            "",
            "Число ненулевых значений по каждому полю находится в `field_coverage.json`. Сводно:",
            "",
        ]
    )
    lines.extend(
        md_table(
            ["Таблица", "Строк в выгрузке", "Заполненные поля"],
            [
                [
                    table,
                    data["rows"],
                    ", ".join(f"{field}: {count}" for field, count in data["non_null_by_field"].items()),
                ]
                for table, data in sorted(coverage.items())
            ],
        )
    )
    lines.extend(
        [
            "## 2. Какие метаданные прайса сохраняются",
            "",
            "- `price_files`: оригинальное имя, SHA-256, размер, MIME, ключ MinIO и поставщик. Исходный относительный путь **не сохраняется** в модели; в этом аудите он восстановлен из SHA-256 и находится в `source_manifest.csv`.",
            "- `price_versions`: порядковый номер у поставщика, статус, loader, map/rule linkage, показатели качества, число строк/товаров/диагностик, текст ошибки, review-only `rule_generation` и время активации.",
            "- `corpus_runs` и `corpus_run_files`: происхождение запуска, агрегаты, длительность, статус конкретного файла, SHA-256 и ссылка на версию/правило.",
            "- `parse_rules`: структура листов и шапок (`fingerprint`), рецепт, revision, status, provenance и результат dry-run валидации.",
            "- `supplier_price_items`: исходное и нормализованное наименование/артикул, единица, категория, габариты, описание, лист и строка происхождения.",
            "- `supplier_price_values`: тип цены, сумма, валюта, НДС, минимум количества, «по запросу» и подпись исходной колонки.",
            "- `attribute_definitions`, `supplier_attribute_keys`, `supplier_price_item_attribute_values`, `attribute_options`: справочник смысла/типа, исходные заголовки поставщика, атомарные значения и допустимые варианты характеристик.",
            "- `price_parse_issues`: код/уровень ошибки, лист, строка, колонка и исходное проблемное значение.",
            "",
            "### Важные границы хранения",
            "",
            "Сейчас executor материализует `name`, `article`, `unit`, `category`, `dimensions`, `description` и ценовые поля. Роли `manufacturer`, `attribute`, `weight`, `volume`, `stock`, `lead_time`, `discount_percent`, `photo` и `quantity_threshold` становятся строками `supplier_price_item_attribute_values`: исходный текст лежит в `raw_value`, а при безопасном приведении заполняется одно скалярное typed-поле. JSON для значений характеристик не используется; `row_number` остаётся технической ролью. `currency` остаётся значением по умолчанию `RUB`, а `min_quantity` пока не связывается с ценовым ярусом. Не сохраняются сырые строки целиком, координаты PDF/страницы, форматирование/формулы Excel и история старых строк при повторном разборе версии — повторный разбор удаляет её прежние items/values/issues.",
            "",
            "## 3. Аудит каждого прайса и правила",
            "",
            "Подробный файл: [audit_by_price.md](audit_by_price.md). В нём для каждого файла приведены SHA-256, статус импорта, версия, применённое правило/маппинг, таблица «колонка источника → роль», счётчики экспорта и безопасный кандидат, если сохранённого маппинга нет.",
            "",
            "Полный JSON всех правил/кандидатов: `rules_for_all_prices.json`; фактические записи `parse_rules` целиком: `all_parse_rules.json`; удобная сводка: `mapping_audit.csv`.",
            "",
            "## 4. Полный формат правил и создание нового парсера",
            "",
            "Подробное руководство со схемой JSON, всеми ролями, жизненным циклом и API: [rule_format_and_authoring_guide_ru.md](rule_format_and_authoring_guide_ru.md). В частности, правило начинается как `draft`, валидируется dry-run и лишь затем активируется. Автокандидаты этого аудита не были записаны или активированы.",
            "",
            "## 5. Что умеет система разбора прайсов",
            "",
            "- Определяет реальный формат по содержимому, а не только по расширению: XLSX/XLSM, XLS, текстовый PDF, сканированный PDF, DOCX, DOC, TXT/CSV.",
            "- Преобразует XLSX/XLS, DOCX/DOC, текст и таблицы текстового PDF в общий набор листов/строк и применяет единый анализ шапки и строк.",
            "- Распознаёт шапку, товарные строки, повторные шапки, категории, наименование, артикул, единицу, несколько цен, НДС и «по запросу»; нормализует наименование, артикул и единицу.",
            "- Пишет качество, предупреждения и ошибки с исходной координатой; умеет exact-match по `header_hash` и partial-match по Jaccard-сходству заголовков (порог 0,6).",
            "- Для PDF-сканов фиксирует `needs_ocr`; для страниц с текстовым слоем, но потерянной из-за изображения ценой, готовит bounded prompt с координатами слов. Для DOC применяется LibreOffice-конвертация в DOCX.",
            "- Лимиты одного грида: до 20 000 строк и 64 колонок. Заголовок ищется в первых 30 строках. Нет LLM- или OCR-fallback; CSV обрабатывается как текстовый loader, хотя отдельного loader `csv` в правилах нет.",
            "",
            "## Экспортные артефакты",
            "",
            f"Все файлы лежат в `{output_dir}`.",
            "",
            "- `source_manifest.csv` — отобранные исходные файлы с относительным путём и SHA-256.",
            "- `corpus_runs.json`, `corpus_run_files.csv` — результаты массовых импортов.",
            "- `suppliers.csv`, `price_files.csv`, `price_versions.csv` — служебные и версионные сущности.",
            "- `supplier_price_items.csv`, `supplier_price_values.csv`, `price_parse_issues.csv` — нормализованные данные и диагностика из БД.",
            "- `data_reconciliation.json` — по-version сверка счётчиков импорта с фактическим экспортом.",
            "- `database_schema.json`, `database_schema.csv`, `field_coverage.json` — схема и заполненность полей.",
            "- `all_parse_rules.json`, `rules_for_all_prices.json`, `mapping_audit.csv`, `audit_by_price.md` — правила и аудит каждого файла.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_artifacts(
    output_dir: Path, snapshot: dict[str, Any], manifest: list[dict[str, Any]], audits: list[dict[str, Any]],
    reconciliation: list[dict[str, Any]], coverage: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_rule_prompt_artifacts(output_dir, audits)
    manifest_export = [{key: value for key, value in row.items() if key != "_path"} for row in manifest]
    run_file_rows = [
        {"run_label": record["run_label"], **record["run_file"]}
        for record in snapshot["run_records"]
    ]
    mapping_rows = []
    for audit in audits:
        run_file = audit.get("run_file") or {}
        version = audit.get("price_version") or {}
        rule = audit.get("referenced_parse_rule") or {}
        candidate = audit.get("candidate") or {}
        mapping_rows.append(
            {
                "source": audit["source"],
                "relative_path": audit["relative_path"],
                "sha256": audit["sha256"],
                "import_status": run_file.get("status"),
                "loader": run_file.get("loader") or version.get("loader") or candidate.get("loader"),
                "price_version_id": version.get("id"),
                "item_count": version.get("item_count"),
                "mapping_source": audit["mapping_source"],
                "parse_rule_id": rule.get("id"),
                "parse_rule_status": rule.get("status"),
                "candidate_state": candidate.get("state"),
                "mapped_columns": len(audit["effective_columns"]),
            }
        )
    write_json(output_dir / "database_schema.json", snapshot["schema"])
    write_csv(output_dir / "database_schema.csv", snapshot["schema"])
    write_csv(output_dir / "source_manifest.csv", manifest_export)
    write_json(output_dir / "corpus_runs.json", snapshot["runs"])
    write_csv(output_dir / "corpus_run_files.csv", run_file_rows)
    write_csv(output_dir / "suppliers.csv", snapshot["suppliers"])
    write_csv(output_dir / "price_files.csv", snapshot["price_files"])
    write_csv(output_dir / "price_versions.csv", snapshot["versions"])
    write_csv(output_dir / "supplier_price_items.csv", snapshot["items"])
    write_csv(output_dir / "supplier_price_values.csv", snapshot["values"])
    write_csv(output_dir / "attribute_definitions.csv", snapshot["attribute_definitions"])
    write_csv(output_dir / "supplier_attribute_keys.csv", snapshot["supplier_attribute_keys"])
    write_csv(output_dir / "supplier_price_item_attribute_values.csv", snapshot["attribute_values"])
    write_csv(output_dir / "attribute_options.csv", snapshot["attribute_options"])
    write_csv(output_dir / "price_parse_issues.csv", snapshot["issues"])
    write_json(output_dir / "all_parse_rules.json", snapshot["all_rules"])
    write_json(
        output_dir / "rules_for_all_prices.json",
        {
            "notice": "candidate_auto — только предложение аудита, оно не записано и не активировано.",
            "allowed_roles": sorted(ROLES),
            "price_files": audits,
        },
    )
    write_json(output_dir / "data_reconciliation.json", reconciliation)
    write_json(output_dir / "field_coverage.json", coverage)
    write_csv(output_dir / "mapping_audit.csv", mapping_rows)
    (output_dir / "audit_by_price.md").write_text(audit_markdown(audits), encoding="utf-8")
    (output_dir / "rule_format_and_authoring_guide_ru.md").write_text(
        rule_guide_markdown(), encoding="utf-8"
    )
    (output_dir / "price_audit_report_ru.md").write_text(
        main_report_markdown(snapshot, manifest, audits, reconciliation, coverage, output_dir),
        encoding="utf-8",
    )


async def run_audit(run_values: list[str], source_values: list[str], output_dir: Path) -> int:
    raw_runs = parse_assignments(run_values, "--run")
    sources = parse_assignments(source_values, "--source")
    if set(raw_runs) != set(sources):
        missing_sources = sorted(set(raw_runs) - set(sources))
        missing_runs = sorted(set(sources) - set(raw_runs))
        messages: list[str] = []
        if missing_sources:
            messages.append("нет --source для: " + ", ".join(missing_sources))
        if missing_runs:
            messages.append("нет --run для: " + ", ".join(missing_runs))
        raise ValueError("Метки --run и --source должны совпадать: " + "; ".join(messages))
    try:
        run_labels = {label: uuid.UUID(value) for label, value in raw_runs.items()}
    except ValueError as exc:
        raise ValueError(f"--run содержит некорректный UUID: {exc}") from exc

    manifest = build_manifest(sources)
    snapshot = await load_snapshot(run_labels)
    reconciliation = build_reconciliation(snapshot)
    coverage = build_field_coverage(snapshot)
    audits = build_audits(manifest, snapshot, reconciliation)
    write_artifacts(output_dir, snapshot, manifest, audits, reconciliation, coverage)
    mismatches = sum(
        not all(
            row[key]
            for key in (
                "item_count_matches",
                "rows_accepted_matches_item_count",
                "errors_count_matches",
                "warnings_count_matches",
            )
        )
        for row in reconciliation
    )
    print(f"Аудит готов: {output_dir}")
    print(f"Исходных файлов: {len(manifest)}; несоответствий счётчиков: {mismatches}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Полный аудит импортированных прайсов и экспорт БД")
    parser.add_argument(
        "--run", action="append", required=True, metavar="LABEL=UUID",
        help="Идентификатор завершённого corpus_run; можно указать несколько раз",
    )
    parser.add_argument(
        "--source", action="append", required=True, metavar="LABEL=PATH",
        help="Каталог-источник для того же LABEL; нужен для SHA и кандидатов правил",
    )
    parser.add_argument("--output-dir", required=True, type=Path, help="Каталог с экспортом и Markdown")
    return parser


if __name__ == "__main__":
    arguments = build_parser().parse_args()
    try:
        sys.exit(asyncio.run(run_audit(arguments.run, arguments.source, arguments.output_dir)))
    except ValueError as exc:
        print(f"Ошибка аргументов/аудита: {exc}", file=sys.stderr)
        sys.exit(2)
