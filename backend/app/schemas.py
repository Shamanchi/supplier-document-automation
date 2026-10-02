"""Pydantic request/response models — the API contract shared with the frontend."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


AttributeValueKind = Literal["text", "integer", "decimal", "boolean", "date", "datetime", "option"]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=2, max_length=96, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    is_archived: bool | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    is_default: bool
    is_archived: bool
    price_files_count: int = 0
    items_count: int = 0
    global_categories_count: int = 0


class SupplierCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)


class ActiveVersionBrief(BaseModel):
    id: uuid.UUID
    version_no: int
    item_count: int
    activated_at: datetime | None


class SupplierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    active_version: ActiveVersionBrief | None = None
    versions_count: int = 0


class UploadAccepted(BaseModel):
    price_version_id: uuid.UUID
    job_id: str


class ServiceClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    requests_per_minute: int = Field(default=60, ge=1, le=10_000)
    max_concurrent_imports: int = Field(default=4, ge=1, le=100)
    max_input_tokens_per_import: int = Field(default=5_000_000, ge=1)
    max_output_tokens_per_import: int = Field(default=1_000_000, ge=1)


class ServiceClientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    key_prefix: str
    is_active: bool
    requests_per_minute: int
    max_concurrent_imports: int
    max_input_tokens_per_import: int
    max_output_tokens_per_import: int
    last_used_at: datetime | None
    created_at: datetime


class ServiceClientSecretOut(BaseModel):
    client: ServiceClientOut
    api_key: str
    webhook_secret: str


class VersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    supplier_id: uuid.UUID
    version_no: int
    status: str
    item_count: int
    rows_accepted: int
    warnings_count: int
    errors_count: int
    quality_score: float | None
    original_filename: str
    created_at: datetime
    activated_at: datetime | None
    error_message: str | None
    loader: str | None = None
    rule_match: str | None = None
    parse_rule_id: uuid.UUID | None = None
    rule_generation: dict | None = None


# --- mapping preview / update ---

class PriceDescriptor(BaseModel):
    kind: str = "other"
    vat_mode: str = "unknown"


class PreviewColumn(BaseModel):
    index: int
    header: str
    suggested_role: str
    confidence: float
    price_descriptor: PriceDescriptor | None = None
    attribute_code: str | None = None
    attribute_value_kind: AttributeValueKind | None = None
    canonical_unit: str | None = None


class PreviewTable(BaseModel):
    sheet_name: str
    header_row_index: int | None
    columns: list[PreviewColumn]
    sample_rows: list[list[str | None]]


class MappingPreview(BaseModel):
    tables: list[PreviewTable]


class MappingColumnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    role: str
    price_descriptor: PriceDescriptor | None = None
    attribute_code: str | None = Field(default=None, max_length=160)
    attribute_value_kind: AttributeValueKind | None = None
    canonical_unit: str | None = Field(default=None, max_length=128)
    quantity_threshold_column_index: int | None = Field(default=None, ge=0)


class MappingTableIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheet_name: str
    # A sheet can contain several vertically stacked price tables.  The row
    # index is their stable local section id; omitted values remain compatible
    # with mappings created by the original one-table UI.
    header_row_index: int | None = Field(default=None, ge=0)
    columns: list[MappingColumnIn]


class MappingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables: list[MappingTableIn]


class JobAccepted(BaseModel):
    job_id: str


# --- currency справочник ---

class CurrencyOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    symbol: str | None = None
    is_default: bool = False


# --- items / issues ---

class ItemPriceOut(BaseModel):
    kind: str
    amount: Decimal | None
    qualifier: str = "exact"
    amount_from: Decimal | None = None
    amount_to: Decimal | None = None
    currency: str | None = None
    currency_symbol: str | None = None
    currency_source: str = "unresolved"
    min_quantity: Decimal | None = None
    max_quantity: Decimal | None = None
    basis_quantity: Decimal | None = None
    basis_unit: str | None = None
    raw_value: str | None = None
    source_column_index: int | None = None
    vat_mode: str
    vat_rate_percent: Decimal | None = None
    price_without_vat: Decimal | None = None
    price_with_vat: Decimal | None = None
    vat_amount: Decimal | None = None
    is_on_request: bool
    source_label: str | None


class ItemAttributeOut(BaseModel):
    """One scalar characteristic fact for the item-list and export contracts."""

    id: uuid.UUID
    code: str
    name: str
    value_kind: AttributeValueKind
    # JSON has no Decimal primitive; decimal/date/datetime values are emitted
    # as precise/ISO strings, while integer and boolean remain native scalars.
    value: str | int | bool | None
    raw_value: str
    unit: str | None
    parse_status: str


class ItemCategoryContextOut(BaseModel):
    """Raw supplier context plus an optional reviewed global mapping."""

    source_path: str | None
    supplier_category_id: uuid.UUID | None
    supplier_normalized_path: str | None
    global_category_id: uuid.UUID | None
    global_path: str | None
    mapping_status: Literal["mapped", "unmapped", "manual"]
    matching_identity: str
    matching_scope: str | None


class ItemCategoryAssignIn(BaseModel):
    """Set (or clear, with null) the operator-assigned category of a position."""

    global_category_id: uuid.UUID | None


class AutoClassifySuggestion(BaseModel):
    item_id: uuid.UUID
    source_name: str
    suggested_category_id: uuid.UUID
    suggested_path: str
    score: float


class AutoClassifyIn(BaseModel):
    price_version_id: uuid.UUID
    apply: bool = False
    min_score: float = Field(default=0.5, ge=0.0, le=1.0)
    limit: int = Field(default=200, ge=1, le=2000)


class AutoClassifyOut(BaseModel):
    considered: int
    suggestions: list[AutoClassifySuggestion]
    applied: int


class SourceEvidenceOut(BaseModel):
    """Raw cells the parser read from the original document for one position.

    Each row is a list of ``[column_index, cell_text]`` pairs (0-based column
    index within the parsed grid). ``categories`` is a list of such rows, one
    per category caption that built the position's category path.
    """

    header: list | None = None
    data: list | None = None
    categories: list | None = None


class ItemOut(BaseModel):
    id: uuid.UUID
    source_name: str
    normalized_name: str
    article: str | None
    unit: str | None
    category_path: str | None
    category: ItemCategoryContextOut
    dimensions: str | None
    description: str | None
    attributes: list[ItemAttributeOut] = Field(default_factory=list)
    source_sheet: str
    source_row: int
    source_section_id: str | None = None
    source_panel_index: int | None = None
    source_column_start: int | None = None
    source_column_end: int | None = None
    provenance: dict | None = None
    source_evidence: SourceEvidenceOut | None = None
    prices: list[ItemPriceOut]


class ItemsPage(BaseModel):
    total: int
    items: list[ItemOut]


class ImportedItemOut(ItemOut):
    """A parsed item together with the price-list context that produced it."""

    supplier_id: uuid.UUID
    supplier_name: str
    price_version_id: uuid.UUID
    version_no: int
    version_status: str
    filename: str
    imported_at: datetime


class ImportedItemsPage(BaseModel):
    total: int
    items: list[ImportedItemOut]


class ImportedPriceVersionOut(BaseModel):
    """A stored price version, including versions created before import jobs."""

    id: uuid.UUID
    supplier_id: uuid.UUID
    supplier_external_id: str | None
    supplier_name: str
    version_no: int
    status: str
    # Назначение документа: общий прайс (`PRICE_LIST`) или разовый счёт/КП
    # заявки (`SUPPLIER_QUOTE`). Активация имеет смысл только для первого.
    document_type: str
    check_batch_id: uuid.UUID | None = None
    loader: str | None = None
    filename: str
    item_count: int
    warnings_count: int
    errors_count: int
    created_at: datetime
    activated_at: datetime | None = None
    import_id: uuid.UUID | None = None


class ImportedPriceVersionsPage(BaseModel):
    total: int
    items: list[ImportedPriceVersionOut]


class GlobalCategoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=512)
    parent_id: uuid.UUID | None = None


class GlobalCategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=512)
    parent_id: uuid.UUID | None = None
    status: Literal["active", "deprecated"] = "active"


class GlobalCategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    normalized_name: str
    path: str
    normalized_path: str
    status: str


class SupplierCategoryAliasOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_path: str
    first_seen_version_id: uuid.UUID | None
    last_seen_version_id: uuid.UUID | None


class SupplierCategoryOut(BaseModel):
    id: uuid.UUID
    supplier_id: uuid.UUID
    supplier_name: str
    parent_id: uuid.UUID | None
    source_name: str
    source_path: str
    normalized_path: str
    aliases: list[SupplierCategoryAliasOut] = Field(default_factory=list)
    global_category: GlobalCategoryOut | None = None
    mapping_source: str | None = None


class SupplierCategoryMappingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    global_category_id: uuid.UUID | None


class SupplierCategoryDirectorySummary(BaseModel):
    supplier_id: uuid.UUID
    supplier_name: str
    supplier_code: str
    categories_count: int
    unmapped_count: int


class CategoryDirectoryStats(BaseModel):
    supplier_categories_count: int
    mapped_count: int
    unmapped_count: int
    global_categories_count: int


class SupplierCategoryPage(BaseModel):
    total: int
    items: list[SupplierCategoryOut]


class GlobalCategorySummaryOut(GlobalCategoryOut):
    depth: int = 0
    children_count: int = 0
    direct_supplier_mappings_count: int = 0
    supplier_mappings_count: int = 0
    direct_product_count: int = 0
    product_count: int = 0


class GlobalCategoryPage(BaseModel):
    total: int
    items: list[GlobalCategorySummaryOut]


class CategoryPriceVersionOut(BaseModel):
    id: uuid.UUID
    version_no: int
    filename: str
    imported_at: datetime


class GlobalCategorySourceAliasOut(BaseModel):
    raw_path: str
    first_seen: CategoryPriceVersionOut | None = None
    last_seen: CategoryPriceVersionOut | None = None


class GlobalCategorySourceItemOut(BaseModel):
    source_name: str
    source_article: str | None = None
    source_sheet: str
    source_row: int
    price: CategoryPriceVersionOut


class GlobalCategorySourceOut(BaseModel):
    supplier_category_id: uuid.UUID
    supplier_id: uuid.UUID
    supplier_code: str
    supplier_name: str
    mapping_source: str
    raw_name: str
    raw_path: str
    normalized_path: str
    product_count: int
    first_seen: CategoryPriceVersionOut | None = None
    last_seen: CategoryPriceVersionOut | None = None
    aliases: list[GlobalCategorySourceAliasOut] = Field(default_factory=list)
    item_samples: list[GlobalCategorySourceItemOut] = Field(default_factory=list)


class GlobalCategorySourcePage(BaseModel):
    global_category: GlobalCategoryOut
    total: int
    items: list[GlobalCategorySourceOut]


class CategoryBootstrapOut(BaseModel):
    created_global_categories: int
    created_mappings: int
    reused_global_categories: int
    blocked_deprecated_paths: int = 0
    remaining_unmapped: int


class GlobalCategoryMergeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_category_id: uuid.UUID


class GlobalCategoryMergeOut(BaseModel):
    source_category_id: uuid.UUID
    target_category: GlobalCategoryOut
    moved_mappings: int


class AttributeDefinitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    display_name: str
    value_kind: AttributeValueKind
    canonical_unit: str | None
    status: str
    is_multivalued: bool


class IssueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    severity: str
    code: str
    sheet: str | None
    row_no: int | None
    column_ref: str | None
    source_value: str | None
    message: str


class IssuesPage(BaseModel):
    total: int
    issues: list[IssueOut]


# --- external price-import service ---


class PriceImportAccepted(BaseModel):
    import_id: uuid.UUID
    status: str
    status_url: str
    result_url: str
    replayed: bool = False


class OrderPriceDocumentAccepted(BaseModel):
    """Ценовой документ, принятый со страницы заявки (мостик Ф2→Ф3).

    Назначение и привязка возвращаются наружу, потому что от них зависит
    дальнейший путь менеджера: разовый счёт/КП появится в подборе своей заявки
    сам после разбора и проверки, а общий прайс — только после активации
    администратором в инженерном интерфейсе.
    """

    import_id: uuid.UUID
    price_version_id: uuid.UUID
    document_type: str
    check_batch_id: uuid.UUID | None
    status: str
    status_url: str
    result_url: str
    replayed: bool = False


class RuleGenerationSummaryOut(BaseModel):
    batch_id: uuid.UUID
    total: int
    queued: int
    running: int
    succeeded: int
    retryable_failed: int
    needs_review: int
    permanent_failed: int
    reused: int
    http_attempts: int
    semantic_retries: int
    next_retry_at: datetime | None
    last_activity_at: datetime | None


class PriceImportStatusOut(BaseModel):
    id: uuid.UUID
    status: str
    stage: str
    progress_percent: int
    supplier_id: uuid.UUID
    supplier_external_id: str | None
    supplier_name: str
    price_version_id: uuid.UUID
    filename: str
    item_count: int
    warnings_count: int
    errors_count: int
    error_code: str | None
    error_message: str | None
    vat_mode: str | None
    completeness: dict | None
    telemetry: dict | None
    webhook: dict | None = None
    rule_generation: RuleGenerationSummaryOut | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    status_url: str
    result_url: str


class RuleSectionOut(BaseModel):
    section_id: str
    sheet_name: str | None
    status: str
    failure_class: str | None
    terminal_reason: str | None
    retry_count: int
    semantic_retry_count: int
    next_retry_at: datetime | None
    artifact_id: uuid.UUID | None
    provenance: str | None = None
    reused: bool
    last_activity_at: datetime | None


class RuleSectionRetryIn(BaseModel):
    section_ids: list[str] | None = None
    retryable_only: bool = True


class RuleSectionRetryItemOut(BaseModel):
    section_id: str
    previous_status: str
    status: str
    result: str


class RuleSectionRetryOut(BaseModel):
    batch_id: uuid.UUID
    queued: int
    results: list[RuleSectionRetryItemOut]


class PriceImportListPage(BaseModel):
    total: int
    items: list[PriceImportStatusOut]


class PriceImportItemsPage(BaseModel):
    import_id: uuid.UUID
    status: str
    total: int
    items: list[ItemOut]


class PriceImportIssuesPage(BaseModel):
    import_id: uuid.UUID
    status: str
    total: int
    issues: list[IssueOut]


class PriceImportResultOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    import_: PriceImportStatusOut = Field(alias="import", serialization_alias="import")
    source: dict
    provenance: dict
    items: list[ItemOut]
    issues: list[IssueOut]


class ReviewStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewDecisionIn(ReviewStrictModel):
    sheet: str = Field(min_length=1, max_length=255)
    row_start: int = Field(ge=1)
    row_end: int = Field(ge=1)
    panel_index: int | None = Field(default=None, ge=0)
    decision: Literal["catalog_without_price", "not_price_section"]
    reason_code: str = Field(min_length=1, max_length=64)
    comment: str = Field(min_length=1, max_length=1000)
    # Явное подтверждение обхода гарда `UNSAFE_NON_PRICE_DECISION`. Требует
    # конкретной причины: решение остаётся в черновике, обход — в журнале.
    override_guard: bool = False

    @model_validator(mode="after")
    def _override_needs_a_concrete_reason(self) -> "ReviewDecisionIn":
        if self.override_guard and self.reason_code == "operator_review":
            raise ValueError(
                "Обход защиты требует конкретной причины: «Проверено оператором» не подходит"
            )
        return self


class ReviewRevisionIn(ReviewStrictModel):
    revision: int = Field(ge=1)


class ReviewMappingUpdateIn(ReviewRevisionIn):
    mapping: MappingIn


class ReviewRuleUpdateIn(ReviewRevisionIn):
    name: str = Field(min_length=1, max_length=512)
    recipe: dict


class ReviewDecisionsUpdateIn(ReviewRevisionIn):
    decisions: list[ReviewDecisionIn] = Field(max_length=2000)


class ReviewCurrencyUpdateIn(ReviewRevisionIn):
    currency: str | None = Field(default=None, min_length=3, max_length=3)


class ReviewRuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    recipe: dict


class ReviewDraftPatchIn(ReviewRevisionIn):
    """Частичное изменение черновика review.

    Четыре прежние ручки (`mapping`, `rule`, `decisions`, `currency`) правили
    один и тот же черновик под одной ревизией и отличались только тем, какое
    поле передано. Здесь они собраны в один запрос.

    «Изменено» определяется по факту присутствия ключа в теле, а не по `None`:
    иначе нельзя было бы снять ручной override валюты, передав `null`. Пустой
    патч отвергается — иначе он молча тратил бы ревизию.
    """

    mapping: MappingIn | None = None
    rule: ReviewRuleIn | None = None
    decisions: list[ReviewDecisionIn] | None = Field(default=None, max_length=2000)
    currency: str | None = Field(default=None, min_length=3, max_length=3)

    @model_validator(mode="after")
    def _at_least_one_change(self) -> "ReviewDraftPatchIn":
        if not ({"mapping", "rule", "decisions", "currency"} & self.model_fields_set):
            raise ValueError("Патч не содержит изменений")
        return self


class ReviewValidateIn(ReviewRevisionIn):
    pass


class ReviewApplyIn(ReviewRevisionIn):
    pass


class ReviewActivateIn(ReviewRevisionIn):
    name: str | None = Field(default=None, min_length=1, max_length=512)


class ReviewAttemptOut(BaseModel):
    id: uuid.UUID
    section_id: str
    status: str
    provider: str
    model: str
    prompt_version: str
    input_context: dict
    proposal: dict | None
    validation: dict | None
    error_message: str | None


class ReviewValidationOut(BaseModel):
    revision: int
    passed: bool
    at: datetime
    metrics: dict
    diff: dict
    state_hash: str


class PriceImportReviewOut(BaseModel):
    import_id: uuid.UUID
    review_id: uuid.UUID
    revision: int
    status: str
    mapping: dict | None
    rule: dict | None
    section_decisions: list[dict]
    currency_override: str | None
    sections: list[dict]
    attempts: list[ReviewAttemptOut]
    validation: dict | None
    actions: dict[str, bool | str]
    updated_by: str
    updated_at: datetime


class ReviewQueueSectionSummary(BaseModel):
    id: uuid.UUID
    import_id: uuid.UUID
    review_id: uuid.UUID
    section_id: str
    supplier_name: str
    filename: str
    sheet_name: str
    section_kind: str
    start_row_index: int | None
    end_row_index: int | None
    status: str
    attempt_status: str | None
    reason_code: str | None
    revision: int
    review_status: str
    decision_count: int
    can_edit: bool
    updated_at: datetime


class ReviewQueuePage(BaseModel):
    total: int
    totals: dict[str, int]
    items: list[ReviewQueueSectionSummary]


class ReviewSourceCell(BaseModel):
    column_index: int
    excel_column: str
    value: str | int | float | bool | None
    is_bold: bool = False
    is_merged_fill: bool = False


class ReviewSourceRow(BaseModel):
    row_index: int
    excel_row: int
    kind: str
    cells: list[ReviewSourceCell]


class PriceVersionPreviewSheet(BaseModel):
    index: int
    name: str
    row_count: int
    column_count: int


class PriceVersionPreview(BaseModel):
    version_id: uuid.UUID
    filename: str
    sheets: list[PriceVersionPreviewSheet]
    selected_sheet_index: int
    selected_sheet_name: str
    total_rows: int
    total_columns: int
    row_offset: int
    row_limit: int
    rows: list[ReviewSourceRow]


def _excel_column(index: int) -> str:
    value = index + 1
    label = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        label = chr(65 + remainder) + label
    return label


class ReviewSectionDetail(BaseModel):
    import_id: uuid.UUID
    review_id: uuid.UUID
    section_id: str
    revision: int
    sheet_name: str
    section_kind: str
    header_row_index: int | None
    header_values: list[str | None]
    rows: list[ReviewSourceRow]
    horizontal_panels: list[dict]
    mapping: dict | None
    decisions: list[dict]
    attempt: dict | None
    validation: dict | None
    actions: dict[str, bool | str]

    @classmethod
    def from_context(
        cls,
        *,
        import_id: uuid.UUID | str,
        review_id: uuid.UUID | str,
        revision: int,
        context: dict,
        mapping: dict | None,
        decisions: list[dict],
        attempt: dict | None,
        validation: dict | None,
        actions: dict[str, bool | str],
    ) -> "ReviewSectionDetail":
        rows_by_index: dict[int, tuple[str, dict]] = {}
        for kind, source_rows in (
            ("header", context.get("header_rows") or []),
            ("source", context.get("sample_rows") or []),
        ):
            for row in source_rows:
                row_index = int(row.get("row_index", 0))
                rows_by_index.setdefault(row_index, (kind, row))
        rows = [
            ReviewSourceRow(
                row_index=row_index,
                excel_row=row_index + 1,
                kind=kind,
                cells=[
                    ReviewSourceCell(
                        column_index=column_index,
                        excel_column=_excel_column(column_index),
                        value=cell.get("value"),
                        is_bold=bool(cell.get("is_bold")),
                        is_merged_fill=bool(cell.get("is_merged_fill")),
                    )
                    for column_index, cell in enumerate(row.get("cells") or [])
                ],
            )
            for row_index, (kind, row) in sorted(rows_by_index.items())
        ]
        return cls(
            import_id=import_id,
            review_id=review_id,
            section_id=str(context.get("section_id")),
            revision=revision,
            sheet_name=str(context.get("sheet_name") or ""),
            section_kind=str(context.get("section_kind") or "unknown"),
            header_row_index=context.get("header_row_index"),
            header_values=[
                None if value is None else str(value)
                for value in (context.get("header_values") or [])
            ],
            rows=rows,
            horizontal_panels=list(context.get("horizontal_panels") or []),
            mapping=mapping,
            decisions=decisions,
            attempt=attempt,
            validation=validation,
            actions=actions,
        )


class ReviewActionAccepted(BaseModel):
    import_id: uuid.UUID
    review_id: uuid.UUID
    revision: int
    status: str
    job_id: str | None = None


class DownloadUrl(BaseModel):
    url: str
    expires_in: int = 900


# --- parse rules ---

class ParseRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    supplier_id: uuid.UUID | None
    name: str
    loader: str
    header_hash: str
    fingerprint: dict
    recipe: dict
    revision: int
    status: str
    provenance: str
    created_from_version_id: uuid.UUID | None
    validation: dict | None
    created_at: datetime
    updated_at: datetime


class ParseRuleUsageOut(BaseModel):
    price_versions: int = 0
    database_items: int = 0
    source_rows: int = 0


class ParseRuleSummaryOut(ParseRuleOut):
    supplier_name: str | None = None
    source_import_id: uuid.UUID | None = None
    source_version: ImportedPriceVersionOut | None = None
    usage: ParseRuleUsageOut = Field(default_factory=ParseRuleUsageOut)


class ParseRulePage(BaseModel):
    total: int
    items: list[ParseRuleSummaryOut]


class ParseRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    supplier_id: uuid.UUID | None = None
    loader: str
    recipe: dict
    fingerprint: dict | None = None
    header_hash: str | None = None


class ParseRuleUpdate(BaseModel):
    name: str | None = None
    recipe: dict | None = None
    supplier_id: uuid.UUID | None = None


class ParseRuleStatusUpdate(BaseModel):
    """Целевой статус правила.

    `draft` недостижим: черновик становится активным или снимается с
    использования, но не возвращается назад — для правок клонируется копия.
    """

    status: Literal["active", "deprecated"]


class RuleValidateIn(BaseModel):
    price_version_id: uuid.UUID


class RuleValidationSampleAttributeOut(BaseModel):
    code: str | None
    name: str
    raw_value: str
    unit: str | None


class RuleValidationSamplePriceOut(BaseModel):
    kind: str
    amount: str | None
    is_on_request: bool
    vat_mode: str
    currency: str | None
    source_column_index: int | None


class RuleValidationSampleOut(BaseModel):
    source_sheet: str
    source_row: int
    name: str
    article: str | None
    unit: str | None
    category: str | None
    attributes: list[RuleValidationSampleAttributeOut]
    prices: list[RuleValidationSamplePriceOut]


class RuleValidationOut(BaseModel):
    at: datetime
    price_version_id: uuid.UUID
    metrics: dict
    passed: bool
    samples: list[RuleValidationSampleOut] = Field(default_factory=list)


class RuleMatrixPreviewIn(BaseModel):
    price_version_id: uuid.UUID
    source_section_id: str = Field(min_length=1, max_length=255)
    recipe: dict


class RuleMatrixRebindIn(BaseModel):
    price_version_id: uuid.UUID
    recipe: dict


class RuleMatrixPreviewCellOut(BaseModel):
    column_index: int
    excel_column: str
    value: str | None
    is_merged_fill: bool


class RuleMatrixPreviewRowOut(BaseModel):
    row_index: int
    excel_row: int
    cells: list[RuleMatrixPreviewCellOut]


class RuleMatrixPreviewOut(BaseModel):
    source_section_id: str
    sheet_index: int
    sheet_name: str
    row_offset: int
    total_rows: int
    total_columns: int
    rows: list[RuleMatrixPreviewRowOut]
    total_items: int
    items: list[RuleValidationSampleOut]


class RuleAutoDraftOut(BaseModel):
    recipe: dict
    metrics: dict
    passed: bool
    sheet_count: int


# --- model-assisted rule proposals ---

class RuleProposalStartIn(BaseModel):
    provider: str | None = Field(default=None, min_length=1, max_length=32)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    temperature: float = Field(default=0, ge=0, le=2)
    top_p: float = Field(default=1, gt=0, le=1)
    max_output_tokens: int | None = Field(default=None, ge=256, le=32768)
    timeout_seconds: int | None = Field(default=None, ge=10, le=300)
    thinking: str = Field(default="disabled", pattern="^(enabled|disabled)$")
    reasoning_effort: str = Field(default="high", pattern="^(high|max)$")


class RuleModelProviderOut(BaseModel):
    provider: str
    label: str
    enabled: bool
    models: list[str]
    default_model: str
    supports_thinking: bool


class RuleModelOptionsOut(BaseModel):
    providers: list[RuleModelProviderOut]
    defaults: dict

class RuleProposalAttemptOut(BaseModel):
    """A redacted, review-only result of one model call for one table section."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    price_version_id: uuid.UUID
    batch_id: uuid.UUID
    section_id: str
    status: str
    provider: str
    model: str
    request_settings: dict
    prompt_version: str
    prompt_id: str
    source_file_sha256: str
    source_context_hash: str
    input_context: dict
    prompt: str
    proposal: dict | None
    validation: dict | None
    error_message: str | None
    parse_rule_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class RuleProposalBatchOut(BaseModel):
    batch_id: uuid.UUID
    attempts: list[RuleProposalAttemptOut]
    job_ids: list[str]
    total_sections: int
    page_size: int
    pages_count: int


class RuleDraftProgressOut(BaseModel):
    status: str
    phase: str
    percent: int = Field(ge=0, le=100)
    completed_sections: int = Field(ge=0)
    total_sections: int = Field(ge=0)
    message: str | None = None
    parse_rule_id: uuid.UUID | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None


class RuleProposalApplyOut(BaseModel):
    rule: ParseRuleOut
    job_id: str


# --- corpus runs ---

class CorpusRunCreate(BaseModel):
    path: str = Field(min_length=1)
    force: bool = False


class CorpusRunAccepted(BaseModel):
    corpus_run_id: uuid.UUID
    job_id: str


class CorpusRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_path: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    total_files: int
    processed: int
    totals: dict | None
    report_storage_key: str | None
    error_message: str | None


class CorpusRunFileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    sha256: str | None
    loader: str | None
    rule_match: str | None
    parse_rule_id: uuid.UUID | None
    price_version_id: uuid.UUID | None
    status: str
    item_count: int
    errors_count: int
    warnings_count: int
    quality_score: float | None
    duration_ms: int | None
    message: str | None


class CorpusRunDetail(BaseModel):
    run: CorpusRunOut
    files_total: int
    files: list[CorpusRunFileOut]
