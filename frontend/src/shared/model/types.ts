export type Role = "viewer" | "admin" | "manager" | "sysadmin";

export type OrderStatus =
  | "draft"
  | "active"
  | "confirmation"
  | "closed"
  | "archived";

export type VerdictStatus =
  | "success"
  | "warning"
  | "danger"
  | "missing";

export interface DashboardData {
  kpis: Array<{
    id: string;
    label: string;
    value: number;
    detail: string;
    tone: "success" | "warning" | "default";
  }>;
  latestOrders: OrderSummary[];
}

export interface OrderSummary {
  id: string;
  number: string;
  client: string;
  /** Приоритет и источник — поля прайс-контура P2: в схеме Фазы 3 их нет,
   *  поэтому ордерные хуки их не заполняют. */
  source?: "bitrix" | "manual" | "email";
  tenderId?: string;
  priority?: "High" | "Medium" | "Low";
  status: OrderStatus;
  statusLabel: string;
  progress: number;
  priceVersionId?: string;
  date: string;
}

export interface OrderFile {
  id: string;
  name: string;
  kind: "specification" | "passport" | "price" | "other";
  version: string;
  date: string;
  progress: number;
  priceVersionId?: string;
  status: "processed" | "warning" | "pending";
  rawRows?: Array<Record<string, string>>;
  mappedRows?: Array<{
    name: string;
    value: string;
    status: VerdictStatus;
  }>;
}

// --- Ордерный домен Фазы 3 (страница заявки) -------------------------------

/** Позиция пакета проверки в кратком виде (`items[]` ответа пакета). */
export interface OrderItem {
  id: string;
  positionNo: number;
  name: string;
  status: string;
  statusLabel: string;
}

/**
 * Строка списка заявок — проекция пакета проверки (`GET /check-batches`).
 *
 * Приоритета и источника в схеме Фазы 3 нет: приоритет не хранится вовсе, а
 * источник выводится из наличия ссылки на тендер. Выдумывать их экран не
 * должен, поэтому колонка приоритета убрана, а источник вычисляется.
 */
export interface OrderSummaryP3 {
  id: string;
  number: string;
  client: string;
  tenderId?: string;
  status: OrderStatus;
  statusLabel: string;
  progress: number;
  date: string;
}

/** Карточка заявки: реквизиты пакета и его позиции. */
export interface OrderCard extends OrderSummaryP3 {
  title: string | null;
  supplier: string | null;
  author: string;
  /** Версия для optimistic lock (`If-Match`). */
  version: number;
  finalVerdict: string;
  updatedAt: string;
  items: OrderItem[];
}

/** Роль документа в позиции: требование заказчика либо факт поставщика. */
export type DocumentRole = "REQUIREMENT" | "FACT";

/**
 * Тип файла сделки: документ позиции (Ф3) или ценовой документ (Ф2).
 * `suggestedKind` — только предложение по имени файла; подтверждает менеджер.
 */
export type FileKind = "REQUIREMENT" | "FACT" | "PRICE";

/** Назначение ценового документа: общий прайс либо разовый счёт/КП заявки. */
export type PriceDocumentKind = "PRICE_LIST" | "SUPPLIER_QUOTE";

/** Документ позиции с его активной версией (`GET /check-items/{id}/documents`). */
export interface ItemDocument {
  documentId: string;
  documentVersionId: string | null;
  role: DocumentRole;
  fileName: string | null;
  mediaType: string | null;
  sizeBytes: number | null;
  sha256: string | null;
  status: string | null;
  createdAt: string;
}

export interface ItemDocuments {
  requirement: ItemDocument[];
  fact: ItemDocument[];
}

/** Прогон позиции (`GET /check-items/{id}/runs`). */
export interface ItemRun {
  id: string;
  runNo: number;
  status: string;
  catalogVersion: string | null;
  startedAt: string | null;
  finishedAt: string | null;
  createdAt: string;
}

export interface PipelineProgress {
  current: number;
  total: number;
  unit: string;
}

export interface PipelineEventMessage {
  id: number;
  event: string;
  checkItemId: string;
  pipelineRunId: string;
  stage: string | null;
  status: string | null;
  occurredAt: string;
  progress?: PipelineProgress;
}

export interface ItemParameterEvidence {
  fragmentId: string;
  quote: string;
  quoteStart: number | null;
  quoteEnd: number | null;
  valueStart: number | null;
  valueEnd: number | null;
  verified: boolean;
  locator: Record<string, unknown> | null;
  /** Подтверждает ли цитата текущее значение: после ручной правки машинная
   *  цитата остаётся историей и приходит с `false`. */
  current: boolean;
}

export interface ItemParameterIssue {
  issueType: string;
  description: string | null;
  severity: string;
}

export interface ItemParameter {
  id: string;
  role: "REQUIREMENT" | "FACT";
  raw: {
    name: string;
    valueText: string | null;
    unitText: string | null;
  };
  normalized: {
    id: string;
    canonicalParameterId: string | null;
    canonicalName: string | null;
    valueKind: string;
    typedValue: Record<string, unknown> | null;
    normalizedOk: boolean;
    version: number;
  } | null;
  extractionMethod: string;
  confidence: string | null;
  issues: ItemParameterIssue[];
  evidence: ItemParameterEvidence[];
  manuallyEdited: boolean;
  excluded: boolean;
  version: number;
  createdAt: string;
}

export interface ItemParametersPage {
  items: ItemParameter[];
  nextCursor: string | null;
}

export type ComparisonStatus =
  | "COMPLIANT"
  | "NON_COMPLIANT"
  | "REVIEW_REQUIRED"
  | "NOT_FOUND_IN_FACT"
  | "NOT_REQUIRED";

export interface ComparisonResult {
  id: string;
  canonicalParameter: {
    id: string;
    code: string;
    name: string;
  } | null;
  requirementParameterId: string | null;
  factParameterId: string | null;
  rowKind: string;
  operator: string | null;
  computedStatus: ComparisonStatus;
  effectiveStatus: ComparisonStatus;
  explanation: Record<string, unknown> | null;
  lineageChain: unknown[] | null;
  overridden: boolean;
  stale: boolean;
  version: number;
}

export interface ComparisonCandidate {
  factParameterId: string | null;
  method: string;
  score: string | null;
  rank: number | null;
  eligible: boolean;
  rejectionReasons: string[];
}

export interface ComparisonCandidates {
  comparisonId: string;
  candidates: ComparisonCandidate[];
}

export type VerdictValue =
  | "PENDING"
  | "COMPLIANT"
  | "PARTIAL"
  | "NON_COMPLIANT";

export interface ItemVerdict {
  id: string;
  /** У заявки нет своей строки вердикта: расчёт и решение живут в колонках
   *  `check_batches`, поэтому `id` там — идентификатор заявки. */
  scope: "CHECK_ITEM" | "CHECK_BATCH";
  scopeId: string;
  computedVerdict: VerdictValue;
  managerVerdict: VerdictValue;
  basis: Record<string, unknown> | null;
  comment: string | null;
  approvedBy: string | null;
  approvedAt: string | null;
  version: number;
}

export interface ReportArtifact {
  id: string;
  format: string;
  fileName: string;
  sizeBytes: number | null;
  sha256: string | null;
  createdAt: string;
}

export interface ReportRequestResult {
  reportId: string;
  status: string;
  requestedFormats: string[];
}

export interface ReportStatus {
  id: string;
  snapshotId: string | null;
  status: string;
  checksum: string | null;
  artifacts: ReportArtifact[];
}

/** Состояние интеграции с Bitrix24 (`GET /integrations/bitrix/health`). */
export interface BitrixHealth {
  configured: boolean;
  mode: "disabled" | "fake" | "webhook";
  status: "OK" | "ERROR" | "DISABLED";
  checkedAt: string;
  errorCode: string | null;
}

/** Карточка сделки — нормализованная модель, а не payload портала. */
export interface BitrixDeal {
  tenderId: string;
  bitrixDealId: string;
  title: string | null;
  customer: string | null;
  stage: string | null;
  assignee: string | null;
  updatedAt: string | null;
  externalUrl: string | null;
}

/**
 * Файл сделки. `suggestedKind`/`suggestedRole` — только предложение по имени
 * файла; подтверждает менеджер, поэтому экран не выбирает за него молча.
 * Признаки импорта разделены: документ позиции и ценовой документ живут в
 * разных контурах, файл может быть уже загружен в один из них.
 */
export interface BitrixDealFile {
  externalFileId: string;
  name: string;
  sizeBytes: number | null;
  mediaType: string | null;
  source: string;
  createdAt: string | null;
  suggestedKind: FileKind | null;
  kindConfidence: string | null;
  alreadyImported: boolean;
  suggestedRole: DocumentRole | null;
  roleConfidence: string | null;
  alreadyImportedDocumentId: string | null;
  alreadyImportedPriceFileId: string | null;
}

/** Ответ списка файлов сделки: сами файлы и текст ленты. */
export interface BitrixDealFiles {
  items: BitrixDealFile[];
  /** Комментарии ленты: там Фаза 1 перечисляет вложения, которые не загрузила. */
  notes: string[];
}

/** Единый ответ подготовки заявки и получения комплекта из Bitrix24. */
export interface BitrixPreparedDeal {
  checkBatchId: string;
  tenderRefId: string;
  created: boolean;
  status: "FILES_AVAILABLE";
  deal: BitrixDeal;
  files: BitrixDealFile[];
  notes: string[];
}

export type BitrixJobStatus =
  | "QUEUED"
  | "RUNNING"
  | "RETRY_SCHEDULED"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

/** Состояние внешней операции: импорта файлов или публикации отчёта. */
export interface BitrixJob {
  id: string;
  type: "IMPORT_FILE" | "PUBLISH_REPORT";
  status: BitrixJobStatus;
  attempt: number;
  nextRetryAt: string | null;
  result: Record<string, unknown> | null;
  error: { code: string; message: string | null; retryable: boolean } | null;
}

/** Что импортировать: файл сделки → позиция заявки либо ценовой документ. */
export interface BitrixImportSelection {
  externalFileId: string;
  /** Для документов Ф3 — REQUIREMENT/FACT, для ценовых — PRICE. */
  kind: FileKind;
  /** Позиция и роль нужны только документам Ф3. */
  checkItemId?: string;
  role?: DocumentRole;
  /** Назначение и поставщик нужны только ценовым документам Ф2. */
  priceDocumentType?: PriceDocumentKind;
  supplierId?: string;
}

/**
 * Статус комплекта заявки из сделки
 * (`GET /integrations/bitrix/check-batches/{id}/status`).
 */
export type KitStatus =
  | "BITRIX_NOT_LINKED"
  | "BITRIX_DEAL_FOUND"
  | "FILES_AVAILABLE"
  | "DOCUMENTS_IMPORTED"
  | "PRICE_IMPORT_RUNNING"
  | "PRICE_REVIEW_REQUIRED"
  | "PRICE_READY"
  | "READY_FOR_CALCULATION";

export interface BatchKitStatus {
  checkBatchId: string;
  status: KitStatus;
  dealLinked: boolean;
  dealId: string | null;
  tenderId: string | null;
  filesAvailable: boolean;
  filesCount: number | null;
  documentsImported: number;
  priceDocuments: number;
  priceReady: number;
  priceRunning: number;
  priceAttention: number;
}

export interface CertificateFilters {
  status?: string;
  processingStatus?: string;
  search?: string;
  cursor?: string;
  limit?: number;
}

export interface PricePosition {
  id: string;
  product: string;
  unit: string;
  supplier: string;
  price: number;
  discount: number;
  method: "auto" | "manual";
  status: VerdictStatus;
  source: string;
  selected: boolean;
}

export interface QuotePosition {
  id: string;
  product: string;
  quantity: number;
  unit: string;
  supplier: string;
  price: number;
  discount: number;
  subtotal: number;
  vat: number;
  total: number;
}

export interface OrderVersion {
  version: string;
  date: string;
  author: string;
  amount: number;
  status: "confirmed" | "draft" | "cancelled";
  final: boolean;
}

export interface OrderDetail extends OrderSummary {
  createdBy: string;
  importedAt: string;
  calculationVersion: string;
  lifecycle: string[];
  currentLifecycle: number;
  sent: boolean;
  readOnly: boolean;
  files: OrderFile[];
  pricePositions: PricePosition[];
  quotePositions: QuotePosition[];
  vatRate: number;
  automaticDiscount: number;
  specialDiscount: number;
  discountApproval: "pending" | "approved" | "rejected";
  supplierTotals: Array<{
    supplier: string;
    amount: number;
    weight: string;
    volume: string;
  }>;
  versions: OrderVersion[];
  auditLog: Array<{
    time: string;
    user: string;
    action: string;
    value: string;
    reason: string;
  }>;
}

/**
 * Контур КП (задача №7): расчётный лист заявки — ответ `GET /orders/{id}`.
 *
 * Id заявки в UI = id check-batch; ручка резолвит связанный PriceRequest.
 * Содержит только данные вкладок цены/КП — шапку страницы по-прежнему
 * отдаёт `GET /check-batches/{id}` (канон Фазы 3).
 */
export interface OrderCommercials
  extends Pick<
    OrderDetail,
    | "calculationVersion"
    | "lifecycle"
    | "currentLifecycle"
    | "sent"
    | "readOnly"
    | "files"
    | "pricePositions"
    | "quotePositions"
    | "vatRate"
    | "automaticDiscount"
    | "specialDiscount"
    | "discountApproval"
    | "supplierTotals"
    | "versions"
    | "auditLog"
  > {
  id: string;
  checkBatchId: string | null;
}

/**
 * Предложение прайса для строки заявки (задача №7, дополнение):
 * ответ `GET /orders/{orderId}/lines/{lineId}/offers`. Поиск — по имени
 * позиции (каскад подбора), поэтому работает и там, где дефисных
 * обозначений в прайсах нет.
 */
export interface LineOffer {
  canonicalId: string;
  canonicalTitle: string;
  priceItemId: string;
  supplier: string;
  name: string;
  unit: string | null;
  price: number | null;
  currency: string | null;
  stage: string;
  score: number;
}

export interface OrderLineOffers {
  lineId: string;
  query: string;
  offers: LineOffer[];
}

/** Выбор менеджера для одной строки при сборке КП. */
export interface QuoteLineSelection {
  lineId: string;
  canonicalId: string;
  priceItemId: string;
  quantity: number;
}

export interface CheckHistoryItem {
  id: string;
  date: string;
  tenderId: string;
  client: string;
  equipment: string;
  tool: string;
  status: "success" | "warning" | "danger";
  statusLabel: string;
  parameters: string;
}

export interface Supplier {
  id: string;
  name: string;
  products: string[];
  files: OrderFile[];
  pricing: {
    product: string;
    purchasePrice: number;
    retailPrice: number;
    categoryDiscount: number;
    markup: number;
    priceUploadedAt: string;
    certificateUpdatedAt: string;
  };
  discountRules: Array<{
    id: string;
    type: string;
    percent: number;
    condition: string;
    active: boolean;
  }>;
  certificates: Array<{
    id: string;
    number: string;
    type: string;
    status: "active" | "expired";
    validUntil: string;
  }>;
}

export interface Product {
  id: string;
  sku: string;
  name: string;
  category: string;
  specifications: string;
  unit: string;
  supplier: string;
  documentType: string;
}

export interface Certificate {
  id: string;
  supplier: string;
  product: string;
  number: string;
  status: "active" | "expired";
  validUntil: string;
  parameters: Array<{
    name: string;
    value: string;
    document: string;
    page: string;
    quote: string;
  }>;
}

// --- Сертификаты Фазы 3 (страница сертификатов) -----------------------------

export type CertificateValidityStatus = "ACTIVE" | "EXPIRED";
export type CertificateProcessingStatus =
  | "UPLOADED"
  | "PROCESSING"
  | "READY"
  | "FAILED";

export interface CertificateEvidence {
  fragmentId: string;
  document: string;
  page: number | null;
  quote: string;
  verified: boolean;
  locator: Record<string, unknown> | null;
}

export interface CertificateParameter {
  name: string;
  value: unknown;
  evidence: CertificateEvidence;
}

export interface CertificateP3 {
  id: string;
  supplier: string;
  product: string;
  number: string;
  status: CertificateValidityStatus;
  validUntil: string | null;
  documentId: string;
  processingStatus: CertificateProcessingStatus;
  processingError: string | null;
  processingErrorCode: string | null;
  processingErrorDetails: Record<string, unknown> | null;
  processingProgress: number;
  processingStage: string | null;
  parameters: CertificateParameter[];
  version: number;
}

export interface CertificatePage {
  items: CertificateP3[];
  nextCursor: string | null;
}

export interface ExtractionPreset {
  code: string;
  name: string;
  description: string;
  docType: string;
  columns: Record<string, unknown>;
  labels: string[];
  sectionKeywords: string[];
  headerKeywords: string[];
  units: string[];
  includeAll: boolean;
  keyvalue: boolean;
  fingerprint: string | null;
  builtin: boolean;
  version: number;
}

export interface CertificateStructureCell {
  text: string;
  row: number;
  col: number;
  page: number | null;
  x0: number | null;
  y0: number | null;
  x1: number | null;
  y1: number | null;
}

export interface CertificateStructureTable {
  id: string;
  page: number | null;
  sheet: string | null;
  bbox: Array<number | null>;
  source: string | null;
  nRows: number;
  nCols: number;
  cells: CertificateStructureCell[];
}

export interface CertificateStructure {
  certificateId: string;
  documentId: string;
  fileName: string;
  pages: number;
  autoPresetCode: string | null;
  autoPresetScore: number | null;
  autoPresetMethod: string | null;
  autoTableId: string | null;
  tables: CertificateStructureTable[];
}

export interface CertificatePageInfo {
  page: number;
  width: number;
  height: number;
  pdfWidth: number;
  pdfHeight: number;
}

export interface CertificateReprocessOptions {
  presetCode?: string | null;
  tableId?: string | null;
  includeAll?: boolean;
  useLlm?: boolean;
}

export interface PresetFromExtractionPayload {
  name: string;
  description?: string;
  presetCode?: string | null;
}

export interface AdminData {
  settings: {
    vatRate: number;
    confidenceHigh: number;
    confidenceMedium: number;
  };
  synonyms: Array<{
    id: string;
    synonym: string;
    canonical: string;
    author: string;
    similarity: number;
    status: "pending" | "accepted" | "rejected";
  }>;
  templates: Array<{
    id: string;
    equipment: string;
    parameters: string[];
  }>;
}

// --- Словарь (каталог Фазы 3; реальный эндпоинт `GET /dictionary`) -----------

/** Узел иерархии материалов; API отдаёт плоский список, дерево собирает клиент. */
export interface MaterialNode {
  id: string;
  code: string;
  name: string;
  children: MaterialNode[];
}

/**
 * View-model экрана «Словарь» — проекция ответа `GET /dictionary`.
 *
 * Синоним в каталоге один и тот же независимо от источника: разделения на
 * «синонимы клиентов» и «названия у поставщиков» в схеме нет, поэтому экран
 * показывает один список.
 */
export interface DictionaryData {
  catalogVersion: string | null;
  nomenclature: Array<{
    id: string;
    code: string;
    canonical: string;
    valueKind: string;
    /** Физическая размерность; у безразмерных параметров её нет. */
    dimension: string | null;
    unit: string | null;
    synonyms: string[];
  }>;
  units: Array<{
    id: string;
    code: string;
    symbol: string;
    dimension: string;
    /** Готовая запись перевода в базовую единицу: «× 0.1». */
    conversion: string | null;
    baseUnit: string | null;
    synonyms: string[];
  }>;
  materials: MaterialNode[];
}

/** Ответ `GET /profile` Фазы 3 (docs/api/profile/README.md). */
export interface ProfileResponse {
  name: string;
  email: string;
  initials: string;
  role: Role;
  permissions: Record<Role, string[]>;
  notifications: Array<{
    id: string;
    checkBatchId: string;
    title: string;
    type: "discount" | "vat";
    status: "pending" | "approved" | "rejected";
    createdAt: string;
  }>;
  /** Все права, которые можно выдать. Каталог приходит с сервера: он же
   *  решает, какие значения принять, поэтому список на экране не хардкодится. */
  permissionCatalog: string[];
  /** Права роли, которые нельзя снять. Экран блокирует их чекбоксы, но
   *  авторитетную проверку выполняет бэкенд. */
  mandatoryPermissions: Partial<Record<Role, string[]>>;
}

/** Ответ `POST /dictionary/synonyms` Фазы 3. */
export interface SuggestionResponse {
  id: string;
  canonical: string;
  synonym: string;
  status: string;
  createdAt: string;
  decidedAt?: string | null;
  /** Черновик, в который лёг принятый синоним. */
  draftVersionCode?: string | null;
}

export interface ProfileData {
  name: string;
  email: string;
  initials: string;
  role: Role;
  notifications: Array<{
    id: string;
    orderId: string;
    title: string;
    createdAt: string;
    type: "discount" | "vat";
    status: "pending" | "approved" | "rejected";
  }>;
  permissions: Record<Role, string[]>;
  /** Права из ответа Фазы 3: на экране профиля P2 не показываются, но
   *  приходят с сервера и должны типизироваться. */
  permissionCatalog?: string[];
  mandatoryPermissions?: Partial<Record<Role, string[]>>;
}
