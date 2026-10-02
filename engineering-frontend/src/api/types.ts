export type AppRole = 'viewer' | 'manager' | 'admin' | 'sysadmin' | 'user';

export interface Project {
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

export interface AuthUser {
  id: string;
  email: string;
  display_name: string;
  initials: string;
  username: string;
  role: AppRole;
  roles: AppRole[];
  permissions: Record<string, string[]>;
}

// Канон identity фазы 3: сессия — camelCase без пользователя в теле;
// пользователь дотягивается через GET /auth/me (merge-remote, шаг 06b).
export interface AuthSession {
  accessToken: string;
  expiresIn: number;
  user: AuthUser;
}

export type PriceImportStatusName =
  | 'queued'
  | 'parsing'
  | 'generating_rule'
  | 'compiling_rule'
  | 'reparsing'
  | 'quality_check'
  | 'completed'
  | 'review_required'
  | 'needs_ocr'
  | 'failed';

export const TERMINAL_IMPORT_STATUSES = new Set<PriceImportStatusName>([
  'completed',
  'review_required',
  'needs_ocr',
  'failed',
]);

export interface PriceImportStatus {
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
  completeness: Record<string, unknown> | null;
  telemetry: Record<string, unknown> | null;
  webhook: Record<string, unknown> | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  status_url: string;
  result_url: string;
}

export interface PriceImportListPage {
  total: number;
  items: PriceImportStatus[];
}

export interface PriceImportAccepted {
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
  provider?: string;
  model?: string;
  idempotencyKey: string;
}

export type AttributeValueKind =
  | 'text'
  | 'integer'
  | 'decimal'
  | 'boolean'
  | 'date'
  | 'datetime'
  | 'option';

export interface ImportItemAttribute {
  id: string;
  code: string;
  name: string;
  value_kind: AttributeValueKind;
  value: string | number | boolean | null;
  raw_value: string;
  unit: string | null;
  parse_status: 'parsed' | 'invalid';
}

export interface ImportItemPrice {
  kind: string;
  amount: string | number | null;
  qualifier: 'exact' | 'range' | 'from' | 'on_request' | 'unparsed';
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
  is_on_request: boolean;
  source_label: string | null;
}

export interface ImportItemCategoryContext {
  source_path: string | null;
  supplier_category_id: string | null;
  supplier_normalized_path: string | null;
  global_category_id: string | null;
  global_path: string | null;
  mapping_status: 'mapped' | 'unmapped' | 'manual';
  matching_identity: string;
  matching_scope: string | null;
}

export type SourceEvidenceCell = [number, string];

export interface SourceEvidence {
  header: SourceEvidenceCell[] | null;
  data: SourceEvidenceCell[] | null;
  categories: SourceEvidenceCell[][] | null;
}

export interface ImportItem {
  id: string;
  source_name: string;
  normalized_name: string;
  article: string | null;
  unit: string | null;
  category_path: string | null;
  category: ImportItemCategoryContext;
  dimensions: string | null;
  description: string | null;
  attributes: ImportItemAttribute[];
  source_sheet: string;
  source_row: number;
  source_section_id: string | null;
  source_panel_index: number | null;
  source_column_start: number | null;
  source_column_end: number | null;
  provenance: Record<string, unknown> | null;
  source_evidence: SourceEvidence | null;
  prices: ImportItemPrice[];
}

export interface PriceImportItemsPage {
  import_id: string;
  status: PriceImportStatusName;
  total: number;
  items: ImportItem[];
}

export interface ImportedPriceItem extends ImportItem {
  supplier_id: string;
  supplier_name: string;
  price_version_id: string;
  version_no: number;
  version_status: string;
  filename: string;
  imported_at: string;
}

export interface ImportedPriceItemsPage {
  total: number;
  items: ImportedPriceItem[];
}

export interface AutoClassifySuggestion {
  item_id: string;
  source_name: string;
  suggested_category_id: string;
  suggested_path: string;
  score: number;
}

export interface AutoClassifyRequest {
  price_version_id: string;
  apply?: boolean;
  min_score?: number;
  limit?: number;
}

export interface AutoClassifyResult {
  considered: number;
  suggestions: AutoClassifySuggestion[];
  applied: number;
}

export interface ImportedPriceVersion {
  id: string;
  supplier_id: string;
  supplier_external_id: string | null;
  supplier_name: string;
  version_no: number;
  status: string;
  document_type: string;
  check_batch_id: string | null;
  loader: string | null;
  filename: string;
  item_count: number;
  warnings_count: number;
  errors_count: number;
  created_at: string;
  activated_at: string | null;
  import_id: string | null;
}

export interface ImportedPriceVersionsPage {
  total: number;
  items: ImportedPriceVersion[];
}

export interface RuleModelProvider {
  provider: string;
  label: string;
  enabled: boolean;
  models: string[];
  default_model: string;
  supports_thinking: boolean;
}

export interface RuleProposalOptions {
  providers: RuleModelProvider[];
  defaults: Record<string, unknown>;
}

export interface RuleProposalAttempt {
  id: string;
  section_id: string;
  status: string;
  provider: string;
  model: string;
  input_context?: {
    sheet_index?: number;
    sheet_name?: string;
    section_kind?: string;
    section_start_row_index?: number;
    section_end_row_index?: number;
    region_start_row_index?: number;
    region_end_row_index?: number;
    proposal_contract?: string;
    [key: string]: unknown;
  };
  error_message?: string | null;
  validation?: {
    model_usage?: { total_tokens?: number; [key: string]: unknown };
    llm_telemetry?: { duration_ms?: number; [key: string]: unknown };
    llm_telemetry_events?: Array<{ duration_ms?: number; [key: string]: unknown }>;
    [key: string]: unknown;
  } | null;
  created_at?: string;
  updated_at?: string;
}

export interface RuleProposalBatch {
  batch_id: string;
  attempts: RuleProposalAttempt[];
  job_ids: string[];
  total_sections: number;
  page_size: number;
  pages_count: number;
}

export interface RuleDraftProgress {
  status: 'idle' | 'running' | 'completed' | 'failed';
  phase: string;
  percent: number;
  completed_sections: number;
  total_sections: number;
  message: string | null;
  parse_rule_id: string | null;
  error_message: string | null;
  started_at: string | null;
  updated_at: string | null;
}

export interface ImportIssue {
  id: string;
  severity: 'error' | 'warning';
  code: string;
  sheet: string | null;
  row_no: number | null;
  column_ref: string | null;
  source_value: string | null;
  message: string;
}

export interface PriceImportIssuesPage {
  import_id: string;
  status: PriceImportStatusName;
  total: number;
  issues: ImportIssue[];
}

export interface RuleModelProvider {
  provider: string;
  label: string;
  enabled: boolean;
  models: string[];
  default_model: string;
  supports_thinking: boolean;
}

export interface PriceImportOptions {
  providers: RuleModelProvider[];
  defaults: {
    provider?: string;
    [key: string]: unknown;
  };
}

export interface GlobalCategory {
  id: string;
  parent_id: string | null;
  name: string;
  normalized_name: string;
  path: string;
  normalized_path: string;
  status: 'active' | 'deprecated';
}

export interface SupplierCategoryAlias {
  source_path: string;
  first_seen_version_id: string | null;
  last_seen_version_id: string | null;
}

export interface SupplierCategory {
  id: string;
  supplier_id: string;
  supplier_name: string;
  parent_id: string | null;
  source_name: string;
  source_path: string;
  normalized_path: string;
  aliases: SupplierCategoryAlias[];
  global_category: GlobalCategory | null;
  mapping_source: string | null;
}

export interface CategorySupplierSummary {
  supplier_id: string;
  supplier_name: string;
  supplier_code: string;
  categories_count: number;
  unmapped_count: number;
}

export interface CategoryDirectoryStats {
  supplier_categories_count: number;
  mapped_count: number;
  unmapped_count: number;
  global_categories_count: number;
}

export interface SupplierCategoryPage {
  total: number;
  items: SupplierCategory[];
}

export interface GlobalCategorySummary extends GlobalCategory {
  depth: number;
  children_count: number;
  direct_supplier_mappings_count: number;
  supplier_mappings_count: number;
  direct_product_count: number;
  product_count: number;
}

export interface GlobalCategoryPage {
  total: number;
  items: GlobalCategorySummary[];
}

export interface CategoryPriceVersion {
  id: string;
  version_no: number;
  filename: string;
  imported_at: string;
}

export interface PriceVersionPreviewSheet {
  index: number;
  name: string;
  row_count: number;
  column_count: number;
}

export interface PriceVersionPreview {
  version_id: string;
  filename: string;
  sheets: PriceVersionPreviewSheet[];
  selected_sheet_index: number;
  selected_sheet_name: string;
  total_rows: number;
  total_columns: number;
  row_offset: number;
  row_limit: number;
  rows: ReviewSourceRow[];
}

export interface GlobalCategorySourceAlias {
  raw_path: string;
  first_seen: CategoryPriceVersion | null;
  last_seen: CategoryPriceVersion | null;
}

export interface GlobalCategorySourceItem {
  source_name: string;
  source_article: string | null;
  source_sheet: string;
  source_row: number;
  price: CategoryPriceVersion;
}

export interface GlobalCategorySource {
  supplier_category_id: string;
  supplier_id: string;
  supplier_code: string;
  supplier_name: string;
  mapping_source: string;
  raw_name: string;
  raw_path: string;
  normalized_path: string;
  product_count: number;
  first_seen: CategoryPriceVersion | null;
  last_seen: CategoryPriceVersion | null;
  aliases: GlobalCategorySourceAlias[];
  item_samples: GlobalCategorySourceItem[];
}

export interface GlobalCategorySourcePage {
  global_category: GlobalCategory;
  total: number;
  items: GlobalCategorySource[];
}

export interface CategoryBootstrapResult {
  created_global_categories: number;
  created_mappings: number;
  reused_global_categories: number;
  blocked_deprecated_paths: number;
  remaining_unmapped: number;
}

export interface GlobalCategoryMergeResult {
  source_category_id: string;
  target_category: GlobalCategory;
  moved_mappings: number;
}

export interface ParseRule {
  id: string;
  project_id: string;
  supplier_id: string | null;
  supplier_name: string | null;
  source_import_id: string | null;
  name: string;
  loader: string;
  header_hash: string;
  fingerprint: Record<string, unknown>;
  recipe: Record<string, unknown>;
  revision: number;
  status: 'draft' | 'active' | 'deprecated';
  provenance: string;
  created_from_version_id: string | null;
  source_version: ImportedPriceVersion | null;
  validation: Record<string, unknown> | null;
  usage?: {
    price_versions: number;
    database_items: number;
    source_rows: number;
  };
  created_at: string;
  updated_at: string;
}

export interface ParseRulePage {
  total: number;
  items: ParseRule[];
}

export interface RuleValidationOut {
  at: string;
  price_version_id: string;
  metrics: Record<string, unknown>;
  passed: boolean;
  samples?: RuleValidationSample[];
}

export interface RuleValidationSample {
  source_sheet: string;
  source_row: number;
  name: string;
  article: string | null;
  unit: string | null;
  category: string | null;
  attributes: Array<{
    code: string | null;
    name: string;
    raw_value: string;
    unit: string | null;
  }>;
  prices: Array<{
    kind: string;
    amount: string | null;
    is_on_request: boolean;
    vat_mode: string;
    currency: string | null;
    source_column_index: number | null;
  }>;
}

export interface RuleMatrixPreview {
  source_section_id: string;
  sheet_index: number;
  sheet_name: string;
  row_offset: number;
  total_rows: number;
  total_columns: number;
  rows: Array<{
    row_index: number;
    excel_row: number;
    cells: Array<{
      column_index: number;
      excel_column: string;
      value: string | null;
      is_merged_fill?: boolean;
    }>;
  }>;
  total_items: number;
  items: RuleValidationSample[];
}

export interface RuleAutoDraftOut {
  recipe: Record<string, unknown>;
  metrics: Record<string, unknown>;
  passed: boolean;
  sheet_count: number;
}

export type RecipeColumnRole =
  | 'name' | 'article' | 'manufacturer' | 'description' | 'attribute' | 'unit'
  | 'price' | 'quantity_threshold' | 'discount_percent' | 'weight' | 'volume'
  | 'dimensions' | 'stock' | 'lead_time' | 'category' | 'photo' | 'row_number'
  | 'ignore';

export type RecipePriceKind = 'wholesale' | 'retail' | 'dealer' | 'purchase' | 'other';
export type RecipeVatMode = 'included' | 'excluded' | 'unknown';

export interface RecipePriceDescriptor {
  kind: RecipePriceKind;
  vat_mode: RecipeVatMode;
}

export type RecipeExtractorKind =
  | 'number_with_unit' | 'ip_code' | 'dimension_triplet' | 'standard_code' | 'fire_class';

export interface RecipeColumn {
  index: number;
  role: RecipeColumnRole;
  header?: string;
  price_descriptor?: RecipePriceDescriptor | null;
  attribute_code?: string | null;
  attribute_value_kind?: AttributeValueKind | null;
  canonical_unit?: string | null;
  quantity_threshold_column_index?: number | null;
  [key: string]: unknown;
}

export interface RecipeInlineAttribute {
  source_column_index: number;
  attribute_code: string;
  display_name: string;
  value_kind: AttributeValueKind;
  canonical_unit: string | null;
  extractor_kind: RecipeExtractorKind;
}

export interface RecipeSheetMatch {
  name_pattern?: string;
  or_index?: number | null;
}

export interface RecipeSheet {
  match: RecipeSheetMatch;
  skip?: boolean;
  header_row?: number | null;
  headerless?: boolean;
  header_match?: 'signature' | null;
  dynamic_body?: boolean;
  repeat?: boolean;
  repeat_sections?: number;
  start_row?: number | null;
  end_row?: number | null;
  name_fallback_column_indexes?: number[];
  default_category_source?: 'sheet_name' | null;
  default_category_path?: string | null;
  allow_qualified_prices?: boolean;
  source_section_id?: string | null;
  source_panel_index?: number | null;
  source_column_start?: number | null;
  source_column_end?: number | null;
  columns: RecipeColumn[];
  inline_attributes?: RecipeInlineAttribute[];
  [key: string]: unknown;
}

export interface RecipeOptions {
  repeat_headers?: string;
  category_rows?: string;
  default_unit?: string | null;
  currency?: string | null;
}

export interface Recipe {
  sheets: RecipeSheet[];
  options?: RecipeOptions;
  [key: string]: unknown;
}

export interface ServiceClient {
  id: string;
  name: string;
  key_prefix: string;
  is_active: boolean;
  requests_per_minute: number;
  max_concurrent_imports: number;
  max_input_tokens_per_import: number;
  max_output_tokens_per_import: number;
  last_used_at: string | null;
  created_at: string;
}

export interface ServiceClientSecret {
  client: ServiceClient;
  api_key: string;
  webhook_secret: string;
}

export interface ReviewAttempt {
  id: string;
  section_id: string;
  status: string;
  provider: string;
  model: string;
  prompt_version: string;
  input_context: Record<string, unknown>;
  proposal: Record<string, unknown> | null;
  validation: Record<string, unknown> | null;
  error_message: string | null;
}

export interface ReviewValidation {
  revision: number;
  passed: boolean;
  at: string;
  metrics: Record<string, unknown>;
  diff: Record<string, unknown>;
  state_hash: string;
}

export type ReviewColumnRole =
  | 'name'
  | 'article'
  | 'manufacturer'
  | 'description'
  | 'attribute'
  | 'unit'
  | 'price'
  | 'quantity_threshold'
  | 'discount_percent'
  | 'weight'
  | 'volume'
  | 'dimensions'
  | 'stock'
  | 'lead_time'
  | 'category'
  | 'photo'
  | 'row_number'
  | 'ignore';

export interface ReviewPriceDescriptor {
  kind: 'wholesale' | 'retail' | 'dealer' | 'purchase' | 'other';
  vat_mode: 'included' | 'excluded' | 'unknown';
}

export interface ReviewMappingColumn {
  index: number;
  role: ReviewColumnRole;
  price_descriptor?: ReviewPriceDescriptor;
  attribute_code?: string;
  attribute_value_kind?: 'text' | 'integer' | 'decimal' | 'boolean' | 'date' | 'datetime' | 'option';
  canonical_unit?: string;
  quantity_threshold_column_index?: number;
}

export interface ReviewMappingTable {
  sheet_name: string;
  header_row_index: number | null;
  columns: ReviewMappingColumn[];
}

export interface ReviewMapping {
  tables: ReviewMappingTable[];
}

export interface ReviewDecision {
  sheet: string;
  row_start: number;
  row_end: number;
  panel_index: number | null;
  decision: 'catalog_without_price' | 'not_price_section';
  reason_code: string;
  comment: string;
  /** Явное подтверждение обхода гарда `UNSAFE_NON_PRICE_DECISION`. */
  override_guard?: boolean;
}

/** Ячейка, которую решение скрывает, по мнению гарда ценового признака. */
export interface ReviewBlockedCell {
  sheet?: string;
  row?: number;
  column?: number;
  value?: string;
}

export interface ReviewSourceCell {
  column_index: number;
  excel_column: string;
  value: string | number | boolean | null;
  is_bold: boolean;
  is_merged_fill: boolean;
}

export interface ReviewSourceRow {
  row_index: number;
  excel_row: number;
  kind: 'header' | 'source' | string;
  cells: ReviewSourceCell[];
}

export interface ReviewSectionDetail {
  import_id: string;
  review_id: string;
  section_id: string;
  revision: number;
  sheet_name: string;
  section_kind: string;
  header_row_index: number | null;
  header_values: Array<string | null>;
  rows: ReviewSourceRow[];
  horizontal_panels: Array<Record<string, unknown>>;
  mapping: ReviewMapping | null;
  decisions: ReviewDecision[];
  attempt: ReviewAttempt | null;
  validation: ReviewValidation | null;
  actions: PriceImportReview['actions'];
}

export interface ReviewQueueSection {
  id: string;
  import_id: string;
  review_id: string;
  section_id: string;
  supplier_name: string;
  filename: string;
  sheet_name: string;
  section_kind: string;
  start_row_index: number | null;
  end_row_index: number | null;
  status: 'pending' | 'in_progress' | 'validated' | 'accepted' | 'rejected';
  attempt_status: string | null;
  reason_code: string | null;
  revision: number;
  review_status: PriceImportReview['status'];
  decision_count: number;
  can_edit: boolean;
  updated_at: string;
}

export interface ReviewQueuePage {
  total: number;
  totals: Record<string, number>;
  items: ReviewQueueSection[];
}

export interface PriceImportReview {
  import_id: string;
  review_id: string;
  revision: number;
  status: 'draft' | 'validated' | 'applying' | 'applied' | 'superseded';
  mapping: ReviewMapping | null;
  rule: { id: string | null; name: string | null; recipe: Record<string, unknown> } | null;
  section_decisions: ReviewDecision[];
  currency_override: string | null;
  sections: Array<Record<string, unknown>>;
  attempts: ReviewAttempt[];
  validation: ReviewValidation | null;
  actions: {
    can_edit: boolean;
    can_validate: boolean;
    can_apply: boolean;
    can_activate: boolean;
    [key: string]: boolean | string;
  };
  updated_by: string;
  updated_at: string;
}

export interface ReviewActionAccepted {
  import_id: string;
  review_id: string;
  revision: number;
  status: string;
  job_id: string | null;
}
