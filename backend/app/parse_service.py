"""Version parsing orchestration shared by the celery task and corpus runs.

Flow for a version without a user mapping:
  detect loader -> (needs_ocr / unsupported_format terminal statuses)
  -> fingerprint -> rule detection (exact / partial / none)
  -> parse -> persist -> auto-draft rule when a clean auto-parse validated.

A version with a stored user mapping re-parses by it and, on success, the
mapping is upserted as the supplier's active rule (user_confirmed).
"""
from __future__ import annotations

import logging
from decimal import Decimal
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ParseRule,
    ParseRuleProvenance,
    ParseRuleStatus,
    PriceFile,
    PriceImport,
    PriceParseIssue,
    PriceVersion,
    RuleMatch,
    SupplierPriceItem,
    SupplierPriceItemAttributeValue,
    SupplierPriceValue,
    VersionStatus,
)
from app.attribute_service import AttributePersistenceContext, persist_attribute_value
from app.category_service import bind_supplier_categories, normalize_category_path
from app.column_aliases import AliasDictionary
from app.currencies import currency_directory_map
from app.parsing import loaders
from app.parsing.loaders import DocumentConversionError, PdfNoTablesError, UnknownFormatError
from app.parsing.mapping import MappingDecision, build_auto_mapping, stored_mapping_to_tables
from app.parsing.pdf_layout import find_builtin_pdf_layout_recipe, parse_pdf_layout
from app.parsing.pipeline import ParseResult, parse_grids
from app.parsing.rule_generation import prepare_rule_generation
from app.parsing.matrix_unfold import (
    MatrixSchemaChangedError,
    build_matrix_section_contexts,
)
from app.parsing.workbook_layout import (
    is_workbook_layout_recipe,
    parse_workbook_layout,
)
from app.parsing.rules import (
    compute_fingerprint,
    compute_header_hash,
    recipe_from_decision,
    recipe_mapping_gaps,
    recipe_prefill_mapping,
    recipe_to_decision,
    validation_metrics,
)
from app import rules_service

logger = logging.getLogger(__name__)


async def apply_parse_rule_to_version(
    session: AsyncSession,
    *,
    version: PriceVersion,
    price_file: PriceFile,
    data: bytes,
    rule: ParseRule,
) -> ParseResult:
    """Apply one reviewed rule to its source version and persist the result."""

    if rule.project_id != price_file.project_id:
        raise ValueError("Правило относится к другому проекту")
    if rule.supplier_id != version.supplier_id:
        raise ValueError("Правило относится к другому поставщику")
    loader = loaders.detect_loader(data, price_file.original_filename)
    if loader != rule.loader:
        raise ValueError("Загрузчик правила не совпадает с исходным прайсом")
    grids = loaders.load_grids_for(loader, data, price_file.original_filename)
    if is_workbook_layout_recipe(rule.recipe):
        result = parse_workbook_layout(grids, rule.recipe)
    else:
        decision = recipe_to_decision(rule.recipe, grids)
        if not decision.auto_ok:
            raise ValueError("Правило не применилось к исходному прайсу")
        result = parse_grids(grids, decision)
    quality = validation_metrics(result)
    if not quality["passed"]:
        raise ValueError("Правило не прошло quality gate при применении")
    await _persist_result(session, version, result)
    version.parse_rule_id = rule.id
    version.rule_match = RuleMatch.exact.value
    return result


async def _queue_complex_pdf(
    session: AsyncSession,
    version: PriceVersion,
    price_file: PriceFile,
    data: bytes,
    *,
    grids,
    failure: str,
    result: ParseResult | None = None,
) -> str:
    """Put a text-bearing PDF into the bounded rule-review queue.

    A `complex` version is deliberately distinct from `needs_ocr`: it has
    enough source text for a deterministic or model-proposed layout rule, but
    no verified rule has produced safe price rows yet.  Persist only its
    diagnostics, never unvalidated tentative items.
    """
    if result is not None:
        await _persist_result(
            session,
            version,
            ParseResult(needs_mapping=True, issues=list(result.issues)),
        )
    else:
        await _persist_result(session, version, ParseResult(needs_mapping=True))

    version.status = VersionStatus.complex.value
    version.rule_generation = prepare_rule_generation(
        data=data,
        filename=price_file.original_filename,
        loader=version.loader or loaders.LOADER_PDF_TEXT,
        grids=grids,
        failure=failure,
    )
    version.error_message = (
        "Текстовый PDF не удалось безопасно разобрать готовыми правилами; "
        "файл направлен в complex. Подготовлен ограниченный prompt для "
        f"черновика layout-правила (id: {version.rule_generation['prompt_id']})."
    )
    return version.status


async def parse_and_persist(
    session: AsyncSession,
    version: PriceVersion,
    price_file: PriceFile,
    data: bytes,
    *,
    allow_rule_mutations: bool = True,
    vat_rate_percent: Decimal | None = None,
) -> str:
    """Parse the file into this version's items. Returns the final version
    status. The caller owns commit/rollback and top-level crash handling.

    ``allow_rule_mutations=False`` keeps a maintenance reparse from creating,
    deprecating, or updating shared parse rules as a side effect.
    """
    dictionary = await AliasDictionary.load(
        session,
        supplier_id=price_file.supplier_id,
        price_file_id=price_file.id,
    )
    with dictionary.active():
        return await _parse_and_persist(
            session,
            version,
            price_file,
            data,
            allow_rule_mutations=allow_rule_mutations,
            vat_rate_percent=vat_rate_percent,
        )


async def _parse_and_persist(
    session: AsyncSession,
    version: PriceVersion,
    price_file: PriceFile,
    data: bytes,
    *,
    allow_rule_mutations: bool = True,
    vat_rate_percent: Decimal | None = None,
) -> str:
    version.error_message = None
    version.rule_generation = None
    version.parse_rule_id = None
    version.rule_match = None

    try:
        loader = loaders.detect_loader(data, price_file.original_filename)
    except UnknownFormatError as exc:
        version.loader = None
        version.status = VersionStatus.unsupported_format.value
        version.error_message = str(exc)
        return version.status
    version.loader = loader

    if loader == loaders.LOADER_PDF_SCAN:
        version.status = VersionStatus.needs_ocr.value
        version.error_message = (
            "Требуется OCR: в PDF нет пригодного текстового слоя. "
            "Модель не вызывается до появления OCR-текста, чтобы не расходовать лимит."
        )
        return version.status

    if version.mapping and version.mapping.get("tables"):
        # A user-confirmed mapping is always the highest-priority instruction.
        # A text-layout profile is only a fallback when the saved mapping cannot
        # even obtain a grid from this PDF.
        try:
            mapping_grids = loaders.load_grids_for(loader, data, price_file.original_filename)
        except (DocumentConversionError, PdfNoTablesError) as exc:
            if loader != loaders.LOADER_PDF_TEXT or not isinstance(exc, PdfNoTablesError):
                version.status = VersionStatus.validation_failed.value
                version.error_message = str(exc)
                return version.status
            logger.info(
                "Подтверждённый маппинг версии %s не получил PDF-grid; проверяем layout-правила",
                version.id,
            )
        else:
            decision = stored_mapping_to_tables(version.mapping, mapping_grids)
            result = parse_grids(mapping_grids, decision)
            await _persist_result(session, version, result)
            if (
                allow_rule_mutations
                and version.status == VersionStatus.validated.value
                and decision.tables
            ):
                await _upsert_user_rule(
                    session, version, price_file, loader, mapping_grids, decision, result
                )
            return version.status

    if loader == loaders.LOADER_PDF_TEXT:
        saved_layout_rule = await rules_service.find_pdf_layout_rule(
            session,
            data,
            loader,
            version.supplier_id,
            project_id=price_file.project_id,
        )
        if saved_layout_rule is not None:
            try:
                layout_result = parse_pdf_layout(data, saved_layout_rule.recipe)
            except ValueError:
                logger.warning(
                    "Layout-правило %s совпало по evidence, но не применилось",
                    saved_layout_rule.id,
                )
            else:
                validation = validation_metrics(layout_result)
                if validation["passed"]:
                    await _persist_result(session, version, layout_result)
                    version.parse_rule_id = saved_layout_rule.id
                    version.rule_match = RuleMatch.exact.value
                    return version.status
                logger.warning(
                    "Layout-правило %s совпало, но не прошло quality gate: %s",
                    saved_layout_rule.id,
                    validation["metrics"],
                )
        builtin_recipe = find_builtin_pdf_layout_recipe(data, price_file.original_filename)
        if builtin_recipe is not None:
            try:
                layout_result = parse_pdf_layout(data, builtin_recipe)
            except ValueError as exc:
                return await _queue_complex_pdf(
                    session,
                    version,
                    price_file,
                    data,
                    grids=[],
                    failure=f"встроенный pdf_layout_v1-профиль не применился: {exc}",
                )
            validation = validation_metrics(layout_result)
            if validation["passed"]:
                await _persist_result(session, version, layout_result)
                version.rule_match = RuleMatch.exact.value
                return version.status
            return await _queue_complex_pdf(
                session,
                version,
                price_file,
                data,
                grids=[],
                result=layout_result,
                failure="встроенное layout-правило не прошло проверку качества",
            )

    try:
        grids = loaders.load_grids_for(loader, data, price_file.original_filename)
    except (DocumentConversionError, PdfNoTablesError) as exc:
        if loader == loaders.LOADER_PDF_TEXT:
            return await _queue_complex_pdf(
                session,
                version,
                price_file,
                data,
                grids=[],
                failure=f"табличный extractor не дал безопасной структуры: {exc}",
            )
        version.status = VersionStatus.validation_failed.value
        version.error_message = str(exc)
        return version.status

    fingerprint = compute_fingerprint(loader, grids)
    header_hash = compute_header_hash(fingerprint)

    # A workbook layout profile describes panel and matrix structure that a
    # generic saved column rule cannot represent, so it is matched before the
    # generic header-hash lookup: otherwise an older user-confirmed rule with
    # the same header hash intercepts a reparse as "stale" and the profile
    # never runs. Such profiles live in `parse_rules` like every other rule.
    if loader in {loaders.LOADER_XLS, loaders.LOADER_XLSX}:
        saved_workbook_rule = await rules_service.find_workbook_layout_rule(
            session,
            grids,
            loader,
            version.supplier_id,
            project_id=price_file.project_id,
        )
        workbook_recipe = (
            saved_workbook_rule.recipe if saved_workbook_rule is not None else None
        )
        if workbook_recipe is not None:
            try:
                workbook_result = parse_workbook_layout(grids, workbook_recipe)
            except MatrixSchemaChangedError as exc:
                logger.warning("Сохранённая схема матрицы изменилась: %s", exc)
                version.status = VersionStatus.needs_mapping.value
                version.parse_rule_id = saved_workbook_rule.id if saved_workbook_rule else None
                version.rule_match = RuleMatch.partial.value
                version.error_message = str(exc)
                return version.status
            except ValueError as exc:
                logger.warning("workbook_layout_v1-профиль не применился: %s", exc)
            else:
                validation = validation_metrics(workbook_result)
                if validation["passed"]:
                    await _persist_result(session, version, workbook_result)
                    version.rule_match = RuleMatch.exact.value
                    if saved_workbook_rule is not None:
                        version.parse_rule_id = saved_workbook_rule.id
                    elif allow_rule_mutations:
                        draft = await rules_service.create_draft_rule(
                            session,
                            project_id=price_file.project_id,
                            supplier_id=version.supplier_id,
                            name=f"Авторазбор: {price_file.original_filename}",
                            loader=loader,
                            header_hash=header_hash,
                            fingerprint=fingerprint,
                            recipe=workbook_recipe,
                            created_from_version_id=version.id,
                            validation=_validation_payload(validation, version),
                        )
                        if draft is not None:
                            version.parse_rule_id = draft.id
                    return version.status
                logger.warning(
                    "Встроенный workbook_layout_v1-профиль не прошёл quality gate: %s",
                    validation["metrics"],
                )

        # A matrix must never fall through to the flat auto-mapper. The
        # profiler uses only workbook geometry and bounded cell types; the
        # actual axis semantics are proposed later by the selected LLM and
        # source-compiled into a declarative generated_matrix_v1 recipe.
        matrix_contexts = build_matrix_section_contexts(grids)
        if saved_workbook_rule is None and matrix_contexts:
            await _persist_result(session, version, ParseResult(needs_mapping=True))
            version.rule_match = RuleMatch.none.value
            version.rule_generation = {
                "status": "prepared",
                "mode": "section_proposals",
                "matrix_sections_count": len(matrix_contexts),
                "requires_manual_review": True,
            }
            version.error_message = (
                "Обнаружена матричная структура. Сгенерируйте правило из прайса: "
                "модель определит смысл осей, backend проверит координаты и dry-run."
            )
            return version.status

    # A stale auto-rule must not immediately reappear as its own 100%-similar
    # partial candidate.  That would turn a maintenance reparse into
    # ``needs_mapping`` even though the current deterministic mapper knows how
    # to retain the new columns.  The exclusion is local to this invocation;
    # the caller decides separately whether a shared rule may be deprecated.
    excluded_partial_rule_ids: set = set()
    partial_auto_candidate = None

    rule = await rules_service.find_exact_rule(
        session,
        header_hash,
        loader,
        version.supplier_id,
        project_id=price_file.project_id,
    )
    if rule is not None:
        gaps = recipe_mapping_gaps(rule.recipe, grids)
        if gaps:
            gap_labels = ", ".join(
                f"{gap['sheet']}:{gap['header']} ({gap['actual_role']} → {gap['expected_role']})"
                for gap in gaps
            )
            logger.warning("Правило %s устарело: %s", rule.id, gap_labels)
            if rule.provenance == ParseRuleProvenance.auto.value:
                excluded_partial_rule_ids.add(rule.id)
                if allow_rule_mutations:
                    rule.status = ParseRuleStatus.deprecated.value
                    rule.updated_at = datetime.now(timezone.utc)
            else:
                version.mapping = recipe_prefill_mapping(rule.recipe, grids)
                version.parse_rule_id = rule.id
                version.rule_match = RuleMatch.partial.value
                version.status = VersionStatus.needs_mapping.value
                version.item_count = 0
                version.rows_accepted = 0
                version.error_message = (
                    "Правило распознало новую колонку, которую оно не использует "
                    f"({gap_labels}); проверьте предзаполненный маппинг."
                )
                return version.status
        else:
            decision = recipe_to_decision(rule.recipe, grids)
            if decision.auto_ok:
                result = parse_grids(grids, decision)
                validation = validation_metrics(result)
                if validation["passed"]:
                    await _persist_result(session, version, result)
                    version.parse_rule_id = rule.id
                    version.rule_match = RuleMatch.exact.value
                    return version.status
                logger.warning(
                    "Правило %s совпало по hash, но не прошло quality gate: %s",
                    rule.id,
                    validation["metrics"],
                )
                if rule.provenance == ParseRuleProvenance.auto.value:
                    excluded_partial_rule_ids.add(rule.id)
                    if allow_rule_mutations:
                        rule.status = ParseRuleStatus.deprecated.value
                        rule.updated_at = datetime.now(timezone.utc)
                else:
                    version.mapping = recipe_prefill_mapping(rule.recipe, grids)
                    version.parse_rule_id = rule.id
                    version.rule_match = RuleMatch.partial.value
                    version.status = VersionStatus.needs_mapping.value
                    version.item_count = 0
                    version.rows_accepted = 0
                    version.error_message = (
                        "Совпавшее правило не прошло проверку качества; "
                        "проверьте предзаполненный маппинг."
                    )
                    return version.status
            else:
                logger.warning("Правило %s совпало по hash, но рецепт не применился — авторазбор", rule.id)

    partial = await rules_service.find_partial_rule(
        session,
        fingerprint,
        loader,
        version.supplier_id,
        project_id=price_file.project_id,
        excluded_rule_ids=excluded_partial_rule_ids,
    )
    if partial is not None:
        cand, score = partial
        if (
            cand.provenance == ParseRuleProvenance.auto.value
            and not allow_rule_mutations
        ):
            # An auto-rule is a historical parser suggestion, not an operator
            # instruction.  Try the current auto mapper before requesting
            # manual work.  If its quality is insufficient below, we still
            # return the old recipe as a review prefill.
            logger.info(
                "Авто-правило %s частично совпало (%.2f); проверяем свежий авторазбор",
                cand.id,
                score,
            )
            partial_auto_candidate = (cand, score)
        else:
            version.mapping = recipe_prefill_mapping(cand.recipe, grids)
            version.parse_rule_id = cand.id
            version.rule_match = RuleMatch.partial.value
            version.status = VersionStatus.needs_mapping.value
            version.item_count = 0
            version.rows_accepted = 0
            version.error_message = (
                f"Структура похожа на правило «{cand.name}» (сходство {score:.2f}) — "
                "маппинг предзаполнен, проверьте изменения"
            )
            return version.status

    # No rule matched: full auto-analysis.
    decision = build_auto_mapping(grids)
    result = parse_grids(grids, decision)
    if partial_auto_candidate is not None:
        validation = validation_metrics(result)
        if not validation["passed"]:
            cand, score = partial_auto_candidate
            version.mapping = recipe_prefill_mapping(cand.recipe, grids)
            version.parse_rule_id = cand.id
            version.rule_match = RuleMatch.partial.value
            version.status = VersionStatus.needs_mapping.value
            version.item_count = 0
            version.rows_accepted = 0
            version.error_message = (
                f"Свежий авторазбор после правила «{cand.name}» не прошёл quality gate "
                f"({score:.2f}); проверьте предзаполненный маппинг."
            )
            return version.status
    if result.needs_mapping and loader == loaders.LOADER_PDF_TEXT:
        return await _queue_complex_pdf(
            session,
            version,
            price_file,
            data,
            grids=grids,
            result=result,
            failure="детерминированный маппер не нашёл одновременно обязательные роли name и price",
        )
    # A text PDF with a header-looking line but no verifiable item rows is not
    # a successful tabular parse. Keep it in the bounded layout-review queue
    # rather than persisting a misleading empty ``validated`` version.
    if loader == loaders.LOADER_PDF_TEXT and not validation_metrics(result)["passed"]:
        return await _queue_complex_pdf(
            session,
            version,
            price_file,
            data,
            grids=grids,
            result=result,
            failure="автоматический маппинг PDF не прошёл проверку качества",
        )
    await _persist_result(session, version, result)
    version.rule_match = RuleMatch.none.value
    if result.needs_mapping:
        version.rule_generation = prepare_rule_generation(
            data=data,
            filename=price_file.original_filename,
            loader=loader,
            grids=grids,
            failure="детерминированный маппер не нашёл одновременно обязательные роли name и price",
        )
        version.error_message = (
            "Маппинг не найден; подготовлен prompt для генерации правила "
            f"(id: {version.rule_generation['prompt_id']})."
        )
    if version.status == VersionStatus.validated.value and decision.tables:
        validation = validation_metrics(result)
        if validation["passed"] and allow_rule_mutations:
            draft = await rules_service.create_draft_rule(
                session,
                project_id=price_file.project_id,
                supplier_id=version.supplier_id,
                name=f"Авторазбор: {price_file.original_filename}",
                loader=loader,
                header_hash=header_hash,
                fingerprint=fingerprint,
                recipe=recipe_from_decision(decision, grids),
                created_from_version_id=version.id,
                validation=_validation_payload(validation, version),
            )
            if draft is not None:
                version.parse_rule_id = draft.id
    return version.status


def _validation_payload(validation: dict, version: PriceVersion) -> dict:
    from datetime import datetime, timezone

    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "price_version_id": str(version.id),
        "metrics": validation["metrics"],
        "passed": validation["passed"],
    }


async def _upsert_user_rule(
    session: AsyncSession,
    version: PriceVersion,
    price_file: PriceFile,
    loader: str,
    grids,
    decision: MappingDecision,
    result: ParseResult,
) -> None:
    fingerprint = compute_fingerprint(loader, grids)
    rule = await rules_service.upsert_user_rule(
        session,
        project_id=price_file.project_id,
        supplier_id=version.supplier_id,
        name=f"Подтверждено пользователем: {price_file.original_filename}",
        loader=loader,
        header_hash=compute_header_hash(fingerprint),
        fingerprint=fingerprint,
        recipe=recipe_from_decision(decision, grids),
        created_from_version_id=version.id,
        validation=_validation_payload(validation_metrics(result), version),
    )
    version.parse_rule_id = rule.id
    version.rule_match = RuleMatch.exact.value


async def _delete_persisted_items(
    session: AsyncSession,
    *,
    version_id,
    source_section_id: str | None = None,
) -> None:
    conditions = [SupplierPriceItem.price_version_id == version_id]
    if source_section_id is not None:
        conditions.append(SupplierPriceItem.source_section_id == source_section_id)
    old_item_ids = select(SupplierPriceItem.id).where(*conditions)
    await session.execute(
        delete(SupplierPriceItemAttributeValue).where(
            SupplierPriceItemAttributeValue.item_id.in_(old_item_ids)
        )
    )
    await session.execute(
        delete(SupplierPriceValue).where(SupplierPriceValue.item_id.in_(old_item_ids))
    )
    await session.execute(
        delete(SupplierPriceItem).where(*conditions)
    )


async def _persist_items(
    session: AsyncSession,
    *,
    version: PriceVersion,
    items,
    vat_rate_percent: Decimal | None = None,
) -> list[tuple[int, object]]:
    """Persist parsed items through the existing category/attribute pipeline."""

    # Ставка импорта — запасной вариант для значений, у которых её нет в
    # источнике. Вызовов у этой функции много, и почти ни один не держит
    # PriceImport под рукой, поэтому ставка читается по версии: связь
    # PriceImport.price_version_id уникальна.
    if vat_rate_percent is None:
        vat_rate_percent = await session.scalar(
            select(PriceImport.vat_rate_percent).where(
                PriceImport.price_version_id == version.id
            )
        )

    parsed_items = list(items)
    attribute_context = AttributePersistenceContext()
    attribute_warnings: list[tuple[int, object]] = []
    currency_map = await currency_directory_map(session)
    project_id = await session.scalar(
        select(PriceFile.project_id).where(PriceFile.id == version.price_file_id)
    )
    category_bindings = await bind_supplier_categories(
        session,
        supplier_id=version.supplier_id,
        version_id=version.id,
        project_id=project_id,
        source_paths=(parsed.category_path for parsed in parsed_items),
    )
    for parsed in parsed_items:
        supplier_category = (
            category_bindings.get(normalize_category_path(parsed.category_path))
            if parsed.category_path
            else None
        )
        item = SupplierPriceItem(
            price_version_id=version.id,
            source_name=parsed.source_name,
            normalized_name=parsed.normalized_name,
            source_article=parsed.source_article,
            normalized_article=parsed.normalized_article,
            unit_code=parsed.unit_code,
            category_path=parsed.category_path,
            supplier_category_id=(supplier_category.id if supplier_category else None),
            dimensions=parsed.dimensions,
            description=parsed.description,
            source_sheet=parsed.source_sheet,
            source_row=parsed.source_row,
            source_section_id=parsed.source_section_id,
            source_panel_index=parsed.source_panel_index,
            source_column_start=parsed.source_column_start,
            source_column_end=parsed.source_column_end,
            source_header_cells=parsed.source_header_cells,
            source_data_cells=parsed.source_data_cells,
            source_category_cells=parsed.source_category_cells,
            glued_source_rows=parsed.glued_source_rows or None,
        )
        session.add(item)
        for parsed_attribute in parsed.attribute_values:
            warning = await persist_attribute_value(
                session,
                item=item,
                supplier_id=version.supplier_id,
                parsed=parsed_attribute,
                cache=attribute_context,
            )
            if warning is not None:
                attribute_warnings.append((parsed.source_row, warning))
        for value in parsed.values:
            currency_ref = (
                currency_map.get(value.currency.upper()) if value.currency else None
            )
            session.add(
                SupplierPriceValue(
                    item=item,
                    price_kind=value.kind,
                    amount=value.amount if value.qualifier == "exact" else None,
                    qualifier=value.qualifier,
                    amount_from=value.amount_from,
                    amount_to=value.amount_to,
                    raw_value=value.raw_value,
                    currency=value.currency,
                    currency_id=currency_ref.id if currency_ref else None,
                    currency_source=value.currency_source,
                    basis_quantity=value.basis_quantity,
                    basis_unit=value.basis_unit,
                    source_column_index=value.source_column_index,
                    vat_mode=value.vat_mode,
                    vat_rate_percent=value.vat_rate_percent if value.vat_rate_percent is not None else vat_rate_percent,
                    min_quantity=value.min_quantity,
                    max_quantity=value.max_quantity,
                    is_on_request=value.is_on_request,
                    source_label=value.source_label,
                )
            )
    return attribute_warnings


async def persist_partial_section_result(
    session: AsyncSession,
    *,
    version: PriceVersion,
    section_id: str,
    result: ParseResult,
) -> dict[str, int]:
    """Atomically replace one verified section without validating the version."""

    if result.needs_mapping or result.errors_count:
        raise ValueError("Partial section result не прошёл локальную проверку")
    if any(item.source_section_id != section_id for item in result.items):
        raise ValueError("Partial section result содержит данные другой section")
    await _delete_persisted_items(
        session, version_id=version.id, source_section_id=section_id
    )
    attribute_warnings = await _persist_items(
        session, version=version, items=result.items
    )
    await session.flush()
    version.item_count = int(
        await session.scalar(
            select(func.count(SupplierPriceItem.id)).where(
                SupplierPriceItem.price_version_id == version.id
            )
        )
        or 0
    )
    # This is deliberately only a visibility counter. Final rows/quality and
    # validated status remain owned by the existing whole-version quality gate.
    version.rows_accepted = version.item_count
    version.quality_score = None
    return {
        "items": len(result.items),
        "prices": sum(len(item.values) for item in result.items),
        "attribute_warnings": len(attribute_warnings),
    }


async def finalize_materialized_version_with_rule(
    session: AsyncSession,
    *,
    version: PriceVersion,
    price_file: PriceFile,
    rule: ParseRule,
) -> None:
    """Bind a fully dry-run-verified rule without rebuilding section rows."""

    if rule.project_id != price_file.project_id:
        raise ValueError("Правило относится к другому проекту")
    if rule.supplier_id != version.supplier_id:
        raise ValueError("Правило относится к другому поставщику")
    validation = rule.validation if isinstance(rule.validation, dict) else {}
    metrics = validation.get("metrics")
    if validation.get("passed") is not True or not isinstance(metrics, dict):
        raise ValueError("Итоговое правило не прошло полный compiler dry-run")
    expected_items = int(validation.get("item_count") or 0)
    expected_priced_items = int(metrics.get("items_with_price") or 0)
    materialized_items = int(
        await session.scalar(
            select(func.count(SupplierPriceItem.id)).where(
                SupplierPriceItem.price_version_id == version.id
            )
        )
        or 0
    )
    materialized_priced_items = int(
        await session.scalar(
            select(func.count(func.distinct(SupplierPriceValue.item_id)))
            .join(SupplierPriceItem)
            .where(SupplierPriceItem.price_version_id == version.id)
        )
        or 0
    )
    if (
        materialized_items != expected_items
        or materialized_priced_items != expected_priced_items
    ):
        raise ValueError(
            "Частично материализованные данные не совпадают с итоговым dry-run"
        )

    version.loader = rule.loader
    version.parse_rule_id = rule.id
    version.rule_match = RuleMatch.exact.value
    version.item_count = materialized_items
    version.rows_accepted = int(metrics.get("rows_accepted") or materialized_items)
    version.warnings_count = int(metrics.get("warnings_count") or 0)
    version.errors_count = int(metrics.get("errors_count") or 0)
    version.quality_score = metrics.get("quality_score")
    version.status = VersionStatus.validated.value


async def _persist_result(session: AsyncSession, version: PriceVersion, result: ParseResult) -> None:
    # Re-parse replaces previous items/values/issues of this version.
    await _delete_persisted_items(session, version_id=version.id)
    await session.execute(
        delete(PriceParseIssue).where(PriceParseIssue.price_version_id == version.id)
    )

    for issue in result.issues:
        session.add(
            PriceParseIssue(
                price_version_id=version.id,
                severity=issue.severity,
                code=issue.code,
                sheet=issue.sheet,
                row_no=issue.row_no,
                column_ref=issue.column_ref,
                source_value=issue.source_value,
                message=issue.message,
            )
        )

    if result.needs_mapping:
        version.status = VersionStatus.needs_mapping.value
        version.item_count = 0
        version.rows_accepted = 0
        version.warnings_count = result.warnings_count
        version.errors_count = result.errors_count
        version.quality_score = None
        return

    attribute_warnings = await _persist_items(
        session, version=version, items=result.items
    )

    for source_row, warning in attribute_warnings:
        session.add(
            PriceParseIssue(
                price_version_id=version.id,
                severity="warning",
                code="ATTRIBUTE_VALUE_PARSE_ERROR",
                row_no=source_row,
                column_ref=str(warning.source_column_index + 1),
                source_value=warning.source_value,
                message=warning.message,
            )
        )

    version.item_count = len(result.items)
    version.rows_accepted = result.rows_accepted
    version.warnings_count = result.warnings_count + len(attribute_warnings)
    version.errors_count = result.errors_count
    version.quality_score = result.quality_score

    if result.rows_accepted == 0:
        version.status = VersionStatus.validation_failed.value
    else:
        version.status = VersionStatus.validated.value
