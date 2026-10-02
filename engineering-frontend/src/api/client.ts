import axios from 'axios';
import type {
  AuthSession,
  AuthUser,
  AutoClassifyRequest,
  AutoClassifyResult,
  CategoryBootstrapResult,
  CategoryDirectoryStats,
  CategorySupplierSummary,
  CreatePriceImportInput,
  GlobalCategory,
  GlobalCategoryMergeResult,
  GlobalCategoryPage,
  GlobalCategorySourcePage,
  ImportItem,
  ImportedPriceItemsPage,
  ImportedPriceVersionsPage,
  ParseRule,
  ParseRulePage,
  RuleAutoDraftOut,
  RuleDraftProgress,
  RuleProposalAttempt,
  RuleProposalBatch,
  RuleProposalOptions,
  RuleMatrixPreview,
  RuleValidationOut,
  PriceImportAccepted,
  PriceImportIssuesPage,
  PriceImportItemsPage,
  PriceImportListPage,
  PriceImportOptions,
  PriceImportReview,
  PriceImportStatus,
  PriceVersionPreview,
  Project,
  ReviewActionAccepted,
  ReviewDecision,
  ReviewMapping,
  ReviewQueuePage,
  ReviewSectionDetail,
  ReviewValidation,
  ServiceClient,
  ServiceClientSecret,
  SupplierCategory,
  SupplierCategoryPage,
} from './types';

export type { AuthUser } from './types';

export const http = axios.create({
  baseURL: '/api/v1',
  withCredentials: true,
});

export function setAuthToken(token: string | null): void {
  if (token) {
    http.defaults.headers.common.Authorization = `Bearer ${token}`;
  } else {
    delete http.defaults.headers.common.Authorization;
  }
}

export function setProjectId(projectId: string | null): void {
  if (projectId) {
    http.defaults.headers.common['X-Project-ID'] = projectId;
  } else {
    delete http.defaults.headers.common['X-Project-ID'];
  }
}

export async function fetchProjects(): Promise<Project[]> {
  const { data } = await http.get<Project[]>('/projects');
  return data;
}

export async function createProject(input: {
  slug: string;
  name: string;
  description?: string;
}): Promise<Project> {
  const { data } = await http.post<Project>('/projects', input);
  return data;
}

let refreshInFlight: Promise<AuthSession> | null = null;

http.interceptors.response.use(
  (response) => response,
  async (error) => {
    if (!axios.isAxiosError(error) || error.response?.status !== 401 || !error.config) {
      throw error;
    }
    const config = error.config as typeof error.config & { _authRetry?: boolean };
    const url = config.url ?? '';
    if (config._authRetry || url.includes('/auth/login') || url.includes('/auth/refresh')) {
      throw error;
    }
    config._authRetry = true;
    try {
      refreshInFlight ??= refreshSession().finally(() => {
        refreshInFlight = null;
      });
      const session = await refreshInFlight;
      setAuthToken(session.accessToken);
      config.headers.Authorization = `Bearer ${session.accessToken}`;
      return http.request(config);
    } catch (refreshError) {
      setAuthToken(null);
      window.dispatchEvent(new Event('price-loader-auth-expired'));
      throw refreshError;
    }
  },
);

// Канон identity фазы 3 (merge-remote, шаг 06b): login/refresh возвращают
// camelCase-сессию { accessToken, expiresIn } без пользователя в теле —
// источник истины о пользователе отдельная ручка GET /auth/me. Составная
// сессия { accessToken, expiresIn, user } собирается здесь, наверх
// (AuthContext) отдаётся как единое целое. Refresh живёт в httpOnly cookie
// и уходит браузером автоматически (withCredentials).
interface SessionTokens {
  accessToken: string;
  expiresIn: number;
}

export async function login(username: string, password: string): Promise<AuthSession> {
  const { data } = await http.post<SessionTokens>('/auth/login', { username, password });
  return composeSession(data);
}

export async function refreshSession(): Promise<AuthSession> {
  const { data } = await http.post<SessionTokens>('/auth/refresh');
  return composeSession(data);
}

async function composeSession(tokens: SessionTokens): Promise<AuthSession> {
  // Токен нужно выставить до /auth/me: ручка авторизуется Bearer-заголовком.
  setAuthToken(tokens.accessToken);
  const user = await fetchCurrentUser();
  return { accessToken: tokens.accessToken, expiresIn: tokens.expiresIn, user };
}

export async function fetchCurrentUser(): Promise<AuthUser> {
  const { data } = await http.get<AuthUser>('/auth/me');
  return data;
}

export async function logout(): Promise<void> {
  await http.post('/auth/logout');
}

export async function createPriceImport(
  input: CreatePriceImportInput,
): Promise<PriceImportAccepted> {
  const form = new FormData();
  form.append('file', input.file);
  form.append('supplier_external_id', input.supplierExternalId);
  if (input.supplierName) form.append('supplier_name', input.supplierName);
  if (input.currencyHint) form.append('currency_hint', input.currencyHint);
  if (input.provider) form.append('provider', input.provider);
  if (input.model) form.append('model', input.model);
  const { data } = await http.post<PriceImportAccepted>('/price-imports', form, {
    headers: { 'Idempotency-Key': input.idempotencyKey },
  });
  return data;
}

export async function fetchPriceImports(params: {
  status?: string;
  limit: number;
  offset: number;
}): Promise<PriceImportListPage> {
  const { data } = await http.get<PriceImportListPage>('/price-imports', { params });
  return data;
}

export async function fetchPriceImport(importId: string): Promise<PriceImportStatus> {
  const { data } = await http.get<PriceImportStatus>(`/price-imports/${importId}`);
  return data;
}

export async function fetchPriceImportItems(
  importId: string,
  params: { limit: number; offset: number },
): Promise<PriceImportItemsPage> {
  const { data } = await http.get<PriceImportItemsPage>(
    `/price-imports/${importId}/items`,
    { params },
  );
  return data;
}

export async function fetchImportedPriceItems(params: {
  query?: string;
  supplierId?: string;
  priceVersionId?: string;
  limit: number;
  offset: number;
}): Promise<ImportedPriceItemsPage> {
  const { data } = await http.get<ImportedPriceItemsPage>('/price-items', {
    params: {
      query: params.query,
      supplier_id: params.supplierId,
      price_version_id: params.priceVersionId,
      limit: params.limit,
      offset: params.offset,
    },
  });
  return data;
}

export async function assignItemCategory(
  itemId: string,
  globalCategoryId: string | null,
): Promise<ImportItem> {
  const { data } = await http.put<ImportItem>(`/price-items/${itemId}`, {
    global_category_id: globalCategoryId,
  });
  return data;
}

export async function autoClassifyItems(
  request: AutoClassifyRequest,
): Promise<AutoClassifyResult> {
  const { data } = await http.post<AutoClassifyResult>('/price-items/auto-classify', {
    price_version_id: request.price_version_id,
    apply: request.apply ?? false,
    min_score: request.min_score ?? 0.5,
    limit: request.limit ?? 200,
  });
  return data;
}

export async function fetchImportedPriceVersions(params: {
  query?: string;
  supplierId?: string;
  hasItems?: boolean;
  limit: number;
  offset: number;
}): Promise<ImportedPriceVersionsPage> {
  const { data } = await http.get<ImportedPriceVersionsPage>('/price-versions', {
    params: {
      query: params.query,
      supplier_id: params.supplierId,
      has_items: params.hasItems,
      limit: params.limit,
      offset: params.offset,
    },
  });
  return data;
}

/**
 * Переводит validated-версию общего прайса в active.
 *
 * Действующий прайс поставщика вытесняется (superseded), поэтому вызов
 * всегда идёт за явным подтверждением пользователя.
 */
export async function activatePriceVersion(versionId: string): Promise<{
  id: string;
  status: string;
}> {
  const { data } = await http.put<{ id: string; status: string }>(
    `/price-versions/${versionId}/status`,
  );
  return data;
}

export async function fetchRuleProposalOptions(): Promise<RuleProposalOptions> {
  const { data } = await http.get<RuleProposalOptions>('/reference/llm-profiles');
  return data;
}

export async function startRuleProposals(
  versionId: string,
  input: { provider: string; model: string; thinking: 'enabled' | 'disabled'; reasoning_effort: 'high' | 'max' },
): Promise<RuleProposalBatch> {
  const { data } = await http.post<RuleProposalBatch>(
    `/price-versions/${versionId}/rule-proposals`,
    input,
  );
  return data;
}

export async function fetchRuleProposals(
  versionId: string,
  batchId: string,
): Promise<RuleProposalAttempt[]> {
  const { data } = await http.get<RuleProposalAttempt[]>(
    `/price-versions/${versionId}/rule-proposals`,
    { params: { batch_id: batchId } },
  );
  return data;
}

export async function createRuleDraftFromProposals(
  versionId: string,
  batchId: string,
): Promise<ParseRule> {
  const { data } = await http.post<ParseRule>(
    `/price-versions/${versionId}/rule-proposals/${batchId}/create-draft`,
  );
  return data;
}

export async function fetchRuleDraftProgress(
  versionId: string,
  batchId: string,
): Promise<RuleDraftProgress> {
  const { data } = await http.get<RuleDraftProgress>(
    `/price-versions/${versionId}/rule-proposals/${batchId}/draft-progress`,
  );
  return data;
}

export async function fetchPriceImportIssues(
  importId: string,
  params: { severity?: string; limit: number; offset: number },
): Promise<PriceImportIssuesPage> {
  const { data } = await http.get<PriceImportIssuesPage>(
    `/price-imports/${importId}/issues`,
    { params },
  );
  return data;
}

export async function fetchPriceImportOptions(): Promise<PriceImportOptions> {
  const { data } = await http.get<PriceImportOptions>('/reference/llm-profiles');
  return data;
}

export async function fetchCategoryDirectoryStats(): Promise<CategoryDirectoryStats> {
  const { data } = await http.get<CategoryDirectoryStats>(
    '/catalog/stats',
  );
  return data;
}

// Одна ручка на все три способа читать дерево: поиск по всему дереву, корни и
// прямые потомки различаются только параметром `parent`.
export async function fetchGlobalCategoryPage(params: {
  query?: string;
  includeDeprecated?: boolean;
  limit: number;
  offset: number;
}): Promise<GlobalCategoryPage> {
  const { data } = await http.get<GlobalCategoryPage>('/catalog/categories', {
    params: {
      query: params.query,
      include_deprecated: params.includeDeprecated,
      limit: params.limit,
      offset: params.offset,
    },
  });
  return data;
}

export async function fetchGlobalCategoryRoots(params: {
  includeDeprecated?: boolean;
  limit: number;
  offset: number;
}): Promise<GlobalCategoryPage> {
  const { data } = await http.get<GlobalCategoryPage>('/catalog/categories', {
    params: {
      parent: 'root',
      include_deprecated: params.includeDeprecated,
      limit: params.limit,
      offset: params.offset,
    },
  });
  return data;
}

export async function fetchGlobalCategoryChildren(
  categoryId: string,
  params: { includeDeprecated?: boolean; limit?: number; offset?: number } = {},
): Promise<GlobalCategoryPage> {
  const { data } = await http.get<GlobalCategoryPage>('/catalog/categories', {
    params: {
      parent: categoryId,
      include_deprecated: params.includeDeprecated,
      limit: params.limit ?? 500,
      offset: params.offset ?? 0,
    },
  });
  return data;
}

export async function fetchGlobalCategorySources(
  categoryId: string,
  params: { limit?: number; offset?: number } = {},
): Promise<GlobalCategorySourcePage> {
  const { data } = await http.get<GlobalCategorySourcePage>(
    `/catalog/categories/${categoryId}/sources`,
    { params: { limit: params.limit ?? 50, offset: params.offset ?? 0 } },
  );
  return data;
}

export async function fetchPriceVersionOriginal(versionId: string): Promise<Blob> {
  const { data } = await http.get<Blob>(`/price-versions/${versionId}/original`, {
    responseType: 'blob',
  });
  return data;
}

export async function fetchPriceVersionPreview(
  versionId: string,
  params: { sheetIndex: number; rowOffset: number; rowLimit: number },
): Promise<PriceVersionPreview> {
  const { data } = await http.get<PriceVersionPreview>(
    `/price-versions/${versionId}/preview`,
    {
      params: {
        sheet_index: params.sheetIndex,
        row_offset: params.rowOffset,
        row_limit: params.rowLimit,
      },
    },
  );
  return data;
}

export async function fetchRuleMatrixPreview(
  ruleId: string,
  input: { priceVersionId: string; sourceSectionId: string; recipe: Record<string, unknown> },
): Promise<RuleMatrixPreview> {
  const { data } = await http.post<RuleMatrixPreview>(
    `/parse-rules/${ruleId}/matrix-preview`,
    {
      price_version_id: input.priceVersionId,
      source_section_id: input.sourceSectionId,
      recipe: input.recipe,
    },
  );
  return data;
}

export async function bootstrapCategoryDirectory(): Promise<CategoryBootstrapResult> {
  const { data } = await http.post<CategoryBootstrapResult>(
    '/catalog/bootstrap',
  );
  return data;
}

export async function mergeGlobalCategory(
  sourceCategoryId: string,
  targetCategoryId: string,
): Promise<GlobalCategoryMergeResult> {
  const { data } = await http.post<GlobalCategoryMergeResult>(
    `/catalog/categories/${sourceCategoryId}/merge`,
    { target_category_id: targetCategoryId },
  );
  return data;
}

export async function createGlobalCategory(input: {
  name: string;
  parentId: string | null;
}): Promise<GlobalCategory> {
  const { data } = await http.post<GlobalCategory>(
    '/catalog/categories',
    { name: input.name, parent_id: input.parentId },
  );
  return data;
}

export async function fetchCategorySuppliers(): Promise<CategorySupplierSummary[]> {
  const { data } = await http.get<CategorySupplierSummary[]>(
    '/catalog/suppliers',
  );
  return data;
}

export async function fetchSupplierCategoryPage(params: {
  supplierId?: string;
  mapped?: boolean;
  query?: string;
  limit: number;
  offset: number;
}): Promise<SupplierCategoryPage> {
  const { data } = await http.get<SupplierCategoryPage>(
    '/catalog/supplier-categories',
    {
      params: {
        supplier_id: params.supplierId,
        mapped: params.mapped,
        query: params.query,
        limit: params.limit,
        offset: params.offset,
      },
    },
  );
  return data;
}

export async function mapSupplierCategory(
  categoryId: string,
  globalCategoryId: string | null,
): Promise<SupplierCategory> {
  const { data } = await http.put<SupplierCategory>(
    `/catalog/supplier-categories/${categoryId}/mapping`,
    { global_category_id: globalCategoryId },
  );
  return data;
}

export async function fetchParseRulePage(params: {
  query?: string;
  status?: string;
  loader?: string;
  limit: number;
  offset: number;
}): Promise<ParseRulePage> {
  const { data } = await http.get<ParseRulePage>('/parse-rules', { params });
  return data;
}

export async function fetchParseRule(ruleId: string): Promise<ParseRule> {
  const { data } = await http.get<ParseRule>(`/parse-rules/${ruleId}`);
  return data;
}

export async function updateParseRule(
  ruleId: string,
  input: { name: string; recipe: Record<string, unknown> },
): Promise<ParseRule> {
  const { data } = await http.put<ParseRule>(`/parse-rules/${ruleId}`, input);
  return data;
}

export async function activateParseRule(ruleId: string): Promise<ParseRule> {
  const { data } = await http.put<ParseRule>(`/parse-rules/${ruleId}/status`, {
    status: 'active',
  });
  return data;
}

export async function deprecateParseRule(ruleId: string): Promise<ParseRule> {
  const { data } = await http.put<ParseRule>(`/parse-rules/${ruleId}/status`, {
    status: 'deprecated',
  });
  return data;
}

export async function validateParseRule(
  ruleId: string,
  priceVersionId: string,
): Promise<RuleValidationOut> {
  const { data } = await http.post<RuleValidationOut>(`/parse-rules/${ruleId}/validate`, {
    price_version_id: priceVersionId,
  });
  return data;
}

export async function exportParseRuleValidation(
  ruleId: string,
  priceVersionId: string,
  format: 'json' | 'xlsx',
): Promise<Blob> {
  const { data } = await http.post<Blob>(
    `/parse-rules/${ruleId}/validation-export`,
    { price_version_id: priceVersionId },
    { params: { format }, responseType: 'blob' },
  );
  return data;
}

export async function cloneParseRule(ruleId: string): Promise<ParseRule> {
  const { data } = await http.post<ParseRule>(`/parse-rules/${ruleId}/clone`);
  return data;
}

export async function autoDraftParseRule(ruleId: string): Promise<RuleAutoDraftOut> {
  const { data } = await http.post<RuleAutoDraftOut>(`/parse-rules/${ruleId}/auto-draft`);
  return data;
}

export async function rebindParseRuleMatrix(
  ruleId: string,
  priceVersionId: string,
  recipe: Record<string, unknown>,
): Promise<RuleAutoDraftOut> {
  const { data } = await http.post<RuleAutoDraftOut>(
    `/parse-rules/${ruleId}/matrix-rebind`,
    { price_version_id: priceVersionId, recipe },
  );
  return data;
}

export async function fetchServiceClients(): Promise<ServiceClient[]> {
  const { data } = await http.get<ServiceClient[]>('/service-clients');
  return data;
}

export async function createServiceClient(input: {
  name: string;
  requestsPerMinute: number;
  maxConcurrentImports: number;
}): Promise<ServiceClientSecret> {
  const { data } = await http.post<ServiceClientSecret>('/service-clients', {
    name: input.name,
    requests_per_minute: input.requestsPerMinute,
    max_concurrent_imports: input.maxConcurrentImports,
  });
  return data;
}

export async function rotateServiceClient(clientId: string): Promise<ServiceClientSecret> {
  const { data } = await http.post<ServiceClientSecret>(`/service-clients/${clientId}/rotate`);
  return data;
}

export async function revokeServiceClient(clientId: string): Promise<ServiceClient> {
  const { data } = await http.post<ServiceClient>(`/service-clients/${clientId}/revoke`);
  return data;
}

export async function fetchPriceImportReview(importId: string): Promise<PriceImportReview> {
  const { data } = await http.get<PriceImportReview>(`/review/${importId}`);
  return data;
}

// Черновик review правится одним PATCH: раньше на каждое поле была своя
// ручка, хотя все четыре меняли один и тот же черновик под одной ревизией.
async function patchReviewDraft(
  importId: string,
  patch: Record<string, unknown>,
): Promise<PriceImportReview> {
  const { data } = await http.patch<PriceImportReview>(`/review/${importId}`, patch);
  return data;
}

export async function updatePriceImportReviewMapping(
  importId: string,
  revision: number,
  mapping: ReviewMapping,
): Promise<PriceImportReview> {
  return patchReviewDraft(importId, { revision, mapping });
}

export async function updatePriceImportReviewDecisions(
  importId: string,
  revision: number,
  decisions: ReviewDecision[],
): Promise<PriceImportReview> {
  return patchReviewDraft(importId, { revision, decisions });
}

export async function fetchPriceImportReviewQueue(params: {
  status?: string;
  reason?: string;
  query?: string;
  limit?: number;
  offset?: number;
} = {}): Promise<ReviewQueuePage> {
  const { data } = await http.get<ReviewQueuePage>('/review/queue', { params });
  return data;
}

export async function fetchPriceImportReviewSection(
  importId: string,
  sectionId: string,
  rowOffset = 0,
  rowLimit = 100,
): Promise<ReviewSectionDetail> {
  const { data } = await http.get<ReviewSectionDetail>(
    `/review/${importId}/sections/${encodeURIComponent(sectionId)}`,
    { params: { row_offset: rowOffset, row_limit: rowLimit } },
  );
  return data;
}

export async function updatePriceImportReviewCurrency(
  importId: string,
  revision: number,
  currency: string | null,
): Promise<PriceImportReview> {
  return patchReviewDraft(importId, { revision, currency });
}

export async function validatePriceImportReview(
  importId: string,
  revision: number,
): Promise<ReviewValidation> {
  const { data } = await http.post<ReviewValidation>(
    `/review/${importId}/validate`,
    { revision },
  );
  return data;
}

export async function applyPriceImportReview(
  importId: string,
  revision: number,
): Promise<ReviewActionAccepted> {
  const { data } = await http.post<ReviewActionAccepted>(
    `/review/${importId}/apply`,
    { revision },
  );
  return data;
}

export async function activatePriceImportReviewRule(
  importId: string,
  revision: number,
  name?: string,
): Promise<ReviewActionAccepted> {
  const { data } = await http.post<ReviewActionAccepted>(
    `/review/${importId}/activate-rule`,
    { revision, name },
  );
  return data;
}

export interface ApiErrorEnvelope {
  code?: string;
  message?: string;
  details?: Record<string, unknown>;
}

/**
 * Разбирает конверт контура Фазы 3 (`{"error": {code, message, details}}`).
 *
 * Доменные ошибки review (например, `UNSAFE_NON_PRICE_DECISION`) несут в
 * `details` координаты ячеек, поэтому их нужно доставать отдельно, а не
 * терять в общем тексте ошибки axios.
 */
export function extractApiErrorEnvelope(error: unknown): ApiErrorEnvelope | null {
  if (!axios.isAxiosError(error)) return null;
  const envelope = (error.response?.data as { error?: unknown } | undefined)?.error;
  if (!envelope || typeof envelope !== 'object') return null;
  const { code, message, details } = envelope as {
    code?: unknown;
    message?: unknown;
    details?: unknown;
  };
  return {
    code: typeof code === 'string' ? code : undefined,
    message: typeof message === 'string' ? message : undefined,
    details: details && typeof details === 'object' ? (details as Record<string, unknown>) : undefined,
  };
}

/** Машинные подробности отказа: конверт Фазы 3 либо словарный `detail`. */
export function extractApiErrorDetails(error: unknown): Record<string, unknown> | undefined {
  const envelope = extractApiErrorEnvelope(error);
  if (envelope?.details) return envelope.details;
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail as unknown;
    if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
      return detail as Record<string, unknown>;
    }
  }
  return undefined;
}

export function extractApiError(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const envelope = extractApiErrorEnvelope(error);
    if (envelope?.message) return envelope.message;
    const detail = error.response?.data?.detail as unknown;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((row) => (typeof row?.msg === 'string' ? row.msg : 'Ошибка проверки данных'))
        .join('; ');
    }
    if (detail && typeof detail === 'object') {
      const message = (detail as { message?: unknown }).message;
      if (typeof message === 'string') return message;
      return JSON.stringify(detail);
    }
    return error.message || 'Неизвестная ошибка API';
  }
  return error instanceof Error ? error.message : 'Неизвестная ошибка';
}

// --------------------------------------------------------------------------
// Подбор позиций: канон, каскад, песочница
// --------------------------------------------------------------------------

export interface MatchCandidate {
  canonical_id: string;
  title: string;
  article: string | null;
  merge_status: 'auto' | 'confirmed' | 'disputed';
  stage: 'synonym' | 'article' | 'vector' | 'fuzzy' | 'none';
  score: number;
}

export interface CertificateInfo {
  available: boolean;
  verdict: string;
  registration_number: string;
  valid_until: string;
  manufacturer: string;
  matched_by: string;
  candidates: number;
  needs_manual_review: boolean;
  review_reasons: string[];
  confirmed_by: string;
  attachable: boolean;
  reason_unavailable: string;
}

export interface VariantOffer {
  supplier: string;
  // Строка прайса: по ней предложение кладётся в расчёт, где заново берутся
  // цена под количество и скидки поставщика.
  price_item_id: string;
  name: string;
  unit: string | null;
  price: number | null;
  currency: string | null;
}

export interface ProductVariant {
  canonical_id: string;
  title: string;
  article: string | null;
  merge_status: 'auto' | 'confirmed' | 'disputed';
  matched_designation: string;
  relevance: number;
  offers: VariantOffer[];
}

export interface PlaygroundLine {
  ordinal: number;
  source_line: string;
  query_name: string;
  quantity: number | null;
  unit_code: string | null;
  warnings: string[];
  decision: 'auto' | 'review' | 'clarify' | 'not_found';
  stage: string;
  candidates: MatchCandidate[];
  designations?: string[];
  variants?: ProductVariant[];
  certificate?: CertificateInfo;
}

export interface PlaygroundResult {
  checked_on: string;
  project?: string;
  certificates_configured: boolean;
  thresholds: { auto: number; review: number };
  lines: PlaygroundLine[];
}

export async function runPlayground(
  text: string,
  withCertificates = true,
  supplier = '',
  // Каталог компании — по умолчанию; 'current' сужает до проекта в шапке.
  project = '',
): Promise<PlaygroundResult> {
  const { data } = await http.post<PlaygroundResult>('/matching/playground', {
    text,
    with_certificates: withCertificates,
    supplier,
    project,
  });
  return data;
}

export async function fetchVariants(
  query: string,
  supplier = '',
  project = '',
): Promise<{ query: string; designations: string[]; total: number; items: ProductVariant[] }> {
  const { data } = await http.post('/matching/variants', { query, supplier, project });
  return data;
}

export async function fetchCanonicalStats(): Promise<{
  canonicals: number;
  auto: number;
  confirmed: number;
  disputed: number;
  sources: number;
}> {
  const { data } = await http.get('/matching/canonical/stats');
  return data;
}

export async function rebuildCanonical(): Promise<{
  items: number;
  canonicals: number;
  disputed: number;
}> {
  const { data } = await http.post('/matching/canonical/rebuild');
  return data;
}

export async function rememberSynonym(phrase: string, canonicalId: string): Promise<void> {
  await http.post('/matching/synonyms', { phrase, canonical_id: canonicalId });
}

export async function fetchIndexStats(): Promise<{
  vectors: number;
  versions: number;
  models: string[];
  current_model: string;
}> {
  const { data } = await http.get('/matching/index/stats');
  return data;
}

export interface CatalogStats {
  suppliers_total: number;
  suppliers_searchable: number;
  versions_searchable: number;
  positions_searchable: number;
  current_project_suppliers: number;
  current_project_versions: number;
  current_project_positions: number;
}

export async function fetchCatalogStats(): Promise<CatalogStats> {
  const { data } = await http.get<CatalogStats>('/matching/catalog/stats');
  return data;
}

// --------------------------------------------------------------------------
// Расчёт: скидки поставщиков, позиции, ручная корректировка цены
// --------------------------------------------------------------------------

export interface CalculationItem {
  id: string;
  ordinal: number;
  source_line: string;
  name: string;
  supplier_id: string | null;
  supplier_name: string;
  quantity: number;
  unit_code: string | null;
  base_price: number | null;
  currency: string | null;
  weight_kg: number | null;
  volume_m3: number | null;
  weight_source: string;
  volume_source: string;
  certificate_number: string | null;
  certificate_valid_until: string | null;
  certificate_confirmed_by: string | null;
  certificate_reference: string | null;
  discount_percent: number;
  discount_source: 'none' | 'supplier' | 'category' | 'article' | 'volume';
  price_after_discount: number | null;
  manual_price: number | null;
  manual_reason: string;
  manual_by: string;
  manual_at: string | null;
  final_price: number | null;
}

export interface CalculationTotals {
  base: number;
  discounted: number;
  final: number;
  by_supplier: Record<string, number>;
  weight_kg: number;
  volume_m3: number;
}

export interface CalculationResult {
  id: string;
  title: string;
  status: string;
  total_amount: number | null;
  items: CalculationItem[];
  totals?: CalculationTotals;
}

export interface CalculationLineInput {
  price_item_id: string;
  quantity: number;
  source_line?: string;
  canonical_id?: string;
}

export async function createCalculation(
  lines: CalculationLineInput[],
  title = '',
): Promise<CalculationResult> {
  const { data } = await http.post<CalculationResult>('/calculations', { lines, title });
  return data;
}

export async function uploadPhysicalReference(file: File): Promise<{ imported: number; skipped: number }> {
  const form = new FormData();
  form.append('file', file);
  const { data } = await http.post('/calculations/physical-reference/import', form);
  return data;
}

export async function fetchPhysicalReferenceStats(): Promise<{
  rows: number;
  with_weight: number;
  with_volume: number;
}> {
  const { data } = await http.get('/calculations/physical-reference/stats');
  return data;
}

export async function downloadCalculationExcel(calculationId: string): Promise<void> {
  const { data } = await http.get(`/calculations/${calculationId}/export.xlsx`, {
    responseType: 'blob',
  });
  const url = URL.createObjectURL(data);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `quote-${calculationId}.xlsx`;
  anchor.click();
  URL.revokeObjectURL(url);
}

export async function overrideCalculationPrice(
  itemId: string,
  price: number | null,
  reason: string,
): Promise<CalculationItem> {
  const { data } = await http.post<CalculationItem>(`/calculations/items/${itemId}/price`, {
    price,
    reason,
  });
  return data;
}

export interface PriceOverrideEntry {
  previous_price: number | null;
  new_price: number | null;
  reason: string;
  changed_by: string;
  created_at: string;
}

export async function fetchPriceHistory(
  itemId: string,
): Promise<{ total: number; items: PriceOverrideEntry[] }> {
  const { data } = await http.get(`/calculations/items/${itemId}/price-history`);
  return data;
}

export interface DiscountRule {
  id: string;
  supplier_id: string;
  scope: 'supplier' | 'category' | 'article' | 'volume';
  category_id: string | null;
  normalized_article: string | null;
  min_amount: number | null;
  percent: number;
  is_active: boolean;
  note: string;
}

export async function fetchDiscounts(
  supplierId?: string,
): Promise<{ total: number; items: DiscountRule[] }> {
  const { data } = await http.get('/calculations/discounts', {
    params: supplierId ? { supplier_id: supplierId } : undefined,
  });
  return data;
}

export async function indexPriceVersion(
  versionId: string,
  rebuild = false,
): Promise<{ mode: string; items: number; inserted: number; reused: number; removed: number }> {
  const { data } = await http.post(`/matching/index/versions/${versionId}`, null, {
    params: { rebuild },
  });
  return data;
}
