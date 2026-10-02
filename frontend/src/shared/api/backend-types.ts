export type BackendRole = "viewer" | "manager" | "admin" | "sysadmin";

export interface BackendUser {
  id: string;
  email: string;
  display_name: string;
  initials: string;
  roles: BackendRole[];
  role: BackendRole;
  permissions: Record<string, string[]>;
}

export interface AuthSession {
  accessToken: string;
  expiresIn: number;
}

export interface ProjectDto {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  is_default: boolean;
  is_archived: boolean;
  price_files_count: number;
  items_count: number;
  global_categories_count: number;
}

export interface SupplierActiveVersionDto {
  id: string;
  version_no: number;
  item_count: number;
  activated_at: string | null;
}

export interface SupplierDto {
  id: string;
  code: string;
  name: string;
  active_version: SupplierActiveVersionDto | null;
  versions_count: number;
}

export type PriceImportStatusName =
  | "queued"
  | "parsing"
  | "generating_rule"
  | "compiling_rule"
  | "reparsing"
  | "quality_check"
  | "completed"
  | "review_required"
  | "needs_ocr"
  | "failed";

export const TERMINAL_IMPORT_STATUSES = new Set<PriceImportStatusName>([
  "completed",
  "review_required",
  "needs_ocr",
  "failed",
]);

export interface PriceImportStatusDto {
  id: string;
  status: PriceImportStatusName;
  stage: PriceImportStatusName;
  progress_percent: number;
  supplier_id: string;
  supplier_external_id: string | null;
  supplier_name: string;
  price_version_id: string;
  filename: string;
  item_count: number;
  warnings_count: number;
  errors_count: number;
  error_code: string | null;
  error_message: string | null;
  vat_mode: string | null;
  completeness: Record<string, unknown> | null;
  telemetry: Record<string, unknown> | null;
  webhook: Record<string, unknown> | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  status_url: string;
  result_url: string;
}

export interface PriceImportListPageDto {
  total: number;
  items: PriceImportStatusDto[];
}

export interface PriceImportAcceptedDto {
  import_id: string;
  status: PriceImportStatusName;
  status_url: string;
  result_url: string;
  replayed: boolean;
}

export interface CreatePriceImportInput {
  file: File;
  supplierExternalId: string;
  supplierName?: string;
  currencyHint?: string;
  vatRatePercent?: string;
  provider?: string;
  model?: string;
  idempotencyKey: string;
}

export interface LlmProfileDto {
  provider: string;
  label: string;
  enabled: boolean;
  models: string[];
  default_model: string;
  supports_thinking: boolean;
}

export interface LlmProfilesDto {
  providers: LlmProfileDto[];
  defaults: {
    provider: string;
  };
}

export interface ImportItemPriceDto {
  kind: string;
  amount: string | number | null;
  qualifier: "exact" | "range" | "from" | "on_request" | "unparsed";
  amount_from: string | number | null;
  amount_to: string | number | null;
  currency: string | null;
  currency_symbol: string | null;
  currency_source: string;
  min_quantity: string | number | null;
  max_quantity: string | number | null;
  basis_quantity: string | number | null;
  basis_unit: string | null;
  raw_value: string | null;
  source_column_index: number | null;
  vat_mode: string;
  vat_rate_percent: string | number | null;
  price_without_vat: string | number | null;
  price_with_vat: string | number | null;
  vat_amount: string | number | null;
  is_on_request: boolean;
  source_label: string | null;
}

export interface ImportItemAttributeDto {
  id: string;
  code: string;
  name: string;
  value_kind:
    | "text"
    | "integer"
    | "decimal"
    | "boolean"
    | "date"
    | "datetime"
    | "option";
  value: string | number | boolean | null;
  raw_value: string;
  unit: string | null;
  parse_status: string;
}

export interface ImportItemCategoryDto {
  source_path: string | null;
  supplier_category_id: string | null;
  supplier_normalized_path: string | null;
  global_category_id: string | null;
  global_path: string | null;
  mapping_status: "mapped" | "unmapped" | "manual";
  matching_identity: string;
  matching_scope: string | null;
}

export interface ImportItemDto {
  id: string;
  source_name: string;
  normalized_name: string;
  article: string | null;
  unit: string | null;
  category_path: string | null;
  category: ImportItemCategoryDto;
  dimensions: string | null;
  description: string | null;
  attributes: ImportItemAttributeDto[];
  source_sheet: string;
  source_row: number;
  source_section_id: string | null;
  source_panel_index: number | null;
  source_column_start: number | null;
  source_column_end: number | null;
  provenance: Record<string, unknown> | null;
  source_evidence: Record<string, unknown> | null;
  prices: ImportItemPriceDto[];
}

export interface ImportedItemDto extends ImportItemDto {
  supplier_id: string;
  supplier_name: string;
  price_version_id: string;
  version_no: number;
  version_status: string;
  filename: string;
  imported_at: string;
}

export interface ImportedItemsPageDto {
  total: number;
  items: ImportedItemDto[];
}

export interface PriceImportItemsPageDto {
  import_id: string;
  status: PriceImportStatusName;
  total: number;
  items: ImportItemDto[];
}

export interface ImportIssueDto {
  id: string;
  severity: "error" | "warning";
  code: string;
  sheet: string | null;
  row_no: number | null;
  column_ref: string | null;
  source_value: string | null;
  message: string;
}

export interface PriceImportIssuesPageDto {
  import_id: string;
  status: PriceImportStatusName;
  total: number;
  issues: ImportIssueDto[];
}

/**
 * Назначение ценового документа, загруженного со страницы заявки.
 *
 * `PRICE_LIST` — общий каталог поставщика: в подбор попадает только после
 * активации администратором. `SUPPLIER_QUOTE` — разовый счёт/КП: действует
 * исключительно в своей заявке, активировать его нельзя.
 */
export type OrderPriceDocumentType = "PRICE_LIST" | "SUPPLIER_QUOTE";

export interface UploadOrderPriceDocumentInput {
  file: File;
  /** Код поставщика (`Supplier.external_id` в контуре Ф2). Не UUID. */
  supplierExternalId: string;
  supplierName?: string;
  documentType: OrderPriceDocumentType;
  currencyHint?: string;
  idempotencyKey?: string;
}

export interface OrderPriceDocumentAcceptedDto {
  import_id: string;
  price_version_id: string;
  document_type: OrderPriceDocumentType;
  check_batch_id: string | null;
  status: PriceImportStatusName;
  status_url: string;
  result_url: string;
  replayed: boolean;
}

