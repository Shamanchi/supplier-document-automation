import type {
  BackendRole,
  ImportIssueDto,
  ImportItemDto,
  ImportItemPriceDto,
  ImportedItemDto,
  PriceImportStatusName,
} from "./backend-types";
import type {
  OrderCard,
  OrderCommercials,
  OrderDetail,
  Role,
} from "../model/types";
import type { StatusTone } from "../ui/page";

export interface ImportStatusView {
  label: string;
  tone: StatusTone;
  terminal: boolean;
}

const IMPORT_STATUS_VIEWS: Record<PriceImportStatusName, ImportStatusView> = {
  queued: { label: "В очереди", tone: "info", terminal: false },
  parsing: { label: "Разбор файла", tone: "info", terminal: false },
  generating_rule: { label: "Построение правила", tone: "info", terminal: false },
  compiling_rule: { label: "Проверка правила", tone: "info", terminal: false },
  reparsing: { label: "Повторный разбор", tone: "info", terminal: false },
  quality_check: { label: "Проверка качества", tone: "info", terminal: false },
  completed: { label: "Обработан", tone: "success", terminal: true },
  review_required: {
    label: "Требуется проверка",
    tone: "warning",
    terminal: true,
  },
  needs_ocr: { label: "Требуется OCR", tone: "warning", terminal: true },
  failed: { label: "Ошибка", tone: "danger", terminal: true },
};

// Канон (миграция, шаг 06, решение №5): identity-маппинг 1:1 — легаси-кейс
// `"user"` от бэка больше не приходит, бэк отдаёт admin/manager/sysadmin.
export function mapBackendRole(role: BackendRole): Role {
  return role;
}

export function mapBackendUserRole(
  role: BackendRole,
  roles: BackendRole[],
): Role {
  return roles.includes("sysadmin") ? "sysadmin" : mapBackendRole(role);
}

export function mapImportStatus(status: PriceImportStatusName): ImportStatusView {
  return IMPORT_STATUS_VIEWS[status];
}

function formatAmount(value: string | number | null): string | null {
  if (value === null) return null;
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 2,
  }).format(numeric);
}

export function formatImportPrice(price: ImportItemPriceDto | undefined): string {
  if (!price) return "Цена не найдена";
  if (price.qualifier === "on_request" || price.is_on_request) {
    return "По запросу";
  }
  const currency = price.currency_symbol ?? price.currency ?? "";
  if (price.qualifier === "range") {
    const from = formatAmount(price.amount_from);
    const to = formatAmount(price.amount_to);
    return from && to ? `${from}–${to} ${currency}`.trim() : "Диапазон не распознан";
  }
  if (price.qualifier === "from") {
    const from = formatAmount(price.amount_from ?? price.amount);
    return from ? `от ${from} ${currency}`.trim() : "Цена не распознана";
  }
  const amount = formatAmount(price.amount);
  return amount ? `${amount} ${currency}`.trim() : "Цена не распознана";
}

export function importPriceKey(price: ImportItemPriceDto): string {
  return [
    price.source_column_index ?? "unknown",
    price.kind,
    price.vat_mode,
    price.min_quantity ?? "",
    price.max_quantity ?? "",
    price.basis_quantity ?? "",
    price.basis_unit ?? "",
  ].join(":");
}

export function importPriceLabel(price: ImportItemPriceDto): string {
  if (price.source_label) return price.source_label;
  const vat = price.vat_mode === "included"
    ? "с НДС"
    : price.vat_mode === "excluded"
      ? "без НДС"
      : "НДС не определён";
  return `${price.kind} · ${vat}`;
}

export function formatImportPriceDetails(price: ImportItemPriceDto): string {
  const vat = price.vat_mode === "included"
    ? "с НДС"
    : price.vat_mode === "excluded"
      ? "без НДС"
      : "НДС не определён";
  const quantity = price.min_quantity != null
    ? `от ${price.min_quantity} ${price.basis_unit ?? "ед."}`
    : null;
  return [formatImportPrice(price), vat, quantity].filter(Boolean).join(" · ");
}

export interface ImportItemView {
  id: string;
  name: string;
  article: string | null;
  category: string | null;
  mappingStatus: ImportItemDto["category"]["mapping_status"];
  unit: string | null;
  price: string;
  prices: ImportItemPriceDto[];
  attributes: string[];
  source: string;
}

export function mapImportItem(item: ImportItemDto): ImportItemView {
  return {
    id: item.id,
    name: item.source_name,
    article: item.article,
    category: item.category.global_path ?? item.category_path,
    mappingStatus: item.category.mapping_status,
    unit: item.unit,
    price: formatImportPrice(item.prices[0]),
    prices: item.prices,
    attributes: item.attributes.map((attribute) => {
      const value = attribute.raw_value || String(attribute.value ?? "");
      return `${attribute.name}: ${value}${attribute.unit ? ` ${attribute.unit}` : ""}`;
    }),
    source: `${item.source_sheet}, строка ${item.source_row}`,
  };
}

export interface CatalogItemView {
  id: string;
  article: string | null;
  name: string;
  category: string | null;
  attributes: string[];
  unit: string | null;
  supplier: string;
  filename: string;
  versionNo: number;
}

export function mapCatalogItem(item: ImportedItemDto): CatalogItemView {
  const base = mapImportItem(item);
  return {
    id: base.id,
    article: base.article,
    name: base.name,
    category: base.category,
    attributes: base.attributes,
    unit: base.unit,
    supplier: item.supplier_name,
    filename: item.filename,
    versionNo: item.version_no,
  };
}

export interface ImportIssueView {
  id: string;
  severity: ImportIssueDto["severity"];
  code: string;
  location: string;
  sourceValue: string | null;
  message: string;
}

export function mapImportIssue(issue: ImportIssueDto): ImportIssueView {
  const parts = [
    issue.sheet,
    issue.row_no === null ? null : `строка ${issue.row_no}`,
    issue.column_ref === null ? null : `колонка ${issue.column_ref}`,
  ].filter((part): part is string => part !== null);

  return {
    id: issue.id,
    severity: issue.severity,
    code: issue.code,
    location: parts.join(", "),
    sourceValue: issue.source_value,
    message: issue.message,
  };
}

// --- Адаптеры контуров Фазы 2 в страницу заявки Фазы 3 (шаг 07) -------------

/**
 * Общие поля P2-модели: шапка — из `OrderCard` (канон `GET /check-batches`),
 * данные контура КП — из расчётного листа (`OrderCommercials`, задача №7),
 * когда он загрузился. Без листа поля вкладок нейтральны: страница не
 * выдумывает данные, а НДС и скидки берутся нулевыми, чтобы виджет
 * считал итоги без вклада этих полей, а не валился на `undefined`.
 */
function toOrderDetailBase(
  order: OrderCard,
  commercials: OrderCommercials | null,
): Omit<
  OrderDetail,
  "files" | "pricePositions" | "quotePositions" | "supplierTotals"
> {
  return {
    id: order.id,
    number: order.number,
    client: order.client,
    source: order.tenderId ? "bitrix" : "manual",
    tenderId: order.tenderId,
    status: order.status,
    statusLabel: order.statusLabel,
    progress: order.progress,
    date: order.date,
    createdBy: order.author,
    importedAt: order.date,
    calculationVersion: commercials?.calculationVersion ?? "—",
    lifecycle: commercials?.lifecycle ?? [],
    currentLifecycle: commercials?.currentLifecycle ?? 0,
    sent: commercials?.sent ?? false,
    readOnly:
      (commercials?.readOnly ?? false) ||
      order.status === "closed" ||
      order.status === "archived",
    vatRate: commercials?.vatRate ?? 0,
    automaticDiscount: commercials?.automaticDiscount ?? 0,
    specialDiscount: commercials?.specialDiscount ?? 0,
    discountApproval: commercials?.discountApproval ?? "pending",
    versions: commercials?.versions ?? [],
    auditLog: commercials?.auditLog ?? [],
  };
}

/**
 * Собирает из карточки заявки Фазы 3 и её расчётного листа структуру,
 * которую читает вкладка «Подбор цен» Фазы 2.
 *
 * Прайс-файлы и позиции цен появляются в листе после загрузки прайса
 * поставщика в контур Фазы 2; до этого адаптер честно отдаёт пустые
 * списки, и вкладка показывает пустое состояние вместо выдуманных цен.
 */
export function toPriceCheckInput(
  order: OrderCard,
  commercials: OrderCommercials | null = null,
): OrderDetail {
  return {
    ...toOrderDetailBase(order, commercials),
    files: commercials?.files ?? [],
    pricePositions: commercials?.pricePositions ?? [],
    quotePositions: commercials?.quotePositions ?? [],
    supplierTotals: commercials?.supplierTotals ?? [],
  };
}

/**
 * Вход вкладки «КП»: позиции — из последнего расчёта листа заявки
 * (`Calculation.request_id` → PriceRequest, задача №7). Пока расчёт
 * не создан, список пуст и вкладка показывает пустое состояние.
 */
export function toQuoteInput(
  order: OrderCard,
  commercials: OrderCommercials | null = null,
): OrderDetail {
  return {
    ...toOrderDetailBase(order, commercials),
    files: commercials?.files ?? [],
    pricePositions: commercials?.pricePositions ?? [],
    quotePositions: commercials?.quotePositions ?? [],
    supplierTotals: commercials?.supplierTotals ?? [],
  };
}
