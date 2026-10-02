import {
  type QueryClient,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { apiClient } from "@/shared/api/client";
import { locallyReportedMutation } from "@/shared/api/query-client";
import { getAccessToken } from "@/shared/api/auth-token";
import { uploadOrderPriceDocument } from "@/shared/api/resources";
import type { UploadOrderPriceDocumentInput } from "@/shared/api/backend-types";
import { env } from "@/shared/config/env";
import type {
  AdminData,
  BatchKitStatus,
  BitrixDeal,
  BitrixDealFiles,
  BitrixHealth,
  BitrixImportSelection,
  BitrixPreparedDeal,
  BitrixJob,
  Certificate,
  CheckHistoryItem,
  ComparisonCandidates,
  ComparisonResult,
  DashboardData,
  DictionaryData,
  OrderDetail,
  ItemDocuments,
  ItemParameter,
  ItemParametersPage,
  ItemRun,
  ItemVerdict,
  OrderCard,
  OrderCommercials,
  OrderLineOffers,
  OrderStatus,
  OrderSummary,
  QuoteLineSelection,
  PipelineEventMessage,
  Product,
  ProfileData,
  ProfileResponse,
  ReportRequestResult,
  ReportStatus,
  Role,
  SuggestionResponse,
  Supplier,
  VerdictValue,
} from "@/shared/model/types";

const queryKeys = {
  dashboard: ["dashboard"] as const,
  orders: (filters?: Record<string, string>) => ["orders", filters] as const,
  order: (id: string) => ["orders", id] as const,
  orderCommercials: (id: string) => ["orders", id, "commercials"] as const,
  orderLineOffers: (orderId: string, lineId: string) =>
    ["orders", orderId, "lines", lineId, "offers"] as const,
  checks: (filters?: Record<string, string>) => ["checks", filters] as const,
  suppliers: ["suppliers"] as const,
  products: (filters?: Record<string, string>) => ["products", filters] as const,
  certificates: ["certificates"] as const,
  dictionary: ["dictionary"] as const,
  mySuggestions: ["dictionary", "synonyms", "mine"] as const,
  itemDocuments: (itemId: string) =>
    ["check-items", itemId, "documents"] as const,
  itemParameters: (itemId: string) =>
    ["check-items", itemId, "parameters"] as const,
  itemRuns: (itemId: string) => ["check-items", itemId, "runs"] as const,
  itemComparisons: (itemId: string) =>
    ["check-items", itemId, "comparisons"] as const,
  comparisonCandidates: (comparisonId: string) =>
    ["comparisons", comparisonId, "candidates"] as const,
  itemVerdict: (itemId: string) => ["check-items", itemId, "verdict"] as const,
  batchVerdict: (batchId: string) =>
    ["check-batches", batchId, "verdict"] as const,
  report: (reportId: string) => ["reports", reportId] as const,
  bitrixHealth: ["integrations", "bitrix", "health"] as const,
  bitrixDeal: (tenderId: string) =>
    ["integrations", "bitrix", "deals", tenderId] as const,
  bitrixDealFiles: (tenderId: string) =>
    ["integrations", "bitrix", "deals", tenderId, "files"] as const,
  bitrixKitStatus: (batchId: string) =>
    ["integrations", "bitrix", "check-batches", batchId, "status"] as const,
  bitrixJob: (jobId: string) => ["integrations", "bitrix", "jobs", jobId] as const,
  admin: ["admin"] as const,
  profile: ["profile"] as const,
};

async function getData<T>(url: string, params?: Record<string, string>) {
  const response = await apiClient.get<T>(url, { params });
  return response.data;
}

type RunAcceptedResponse = {
  checkItemId: string;
  pipelineRunId: string;
  status: string;
  eventsUrl: string;
};

const TERMINAL_PIPELINE_EVENTS = new Set([
  "pipeline.run_completed.v1",
  "pipeline.run_failed.v1",
  "pipeline.run_cancelled.v1",
]);

function invalidateItemWorkflow(
  queryClient: QueryClient,
  itemId: string,
  orderId?: string,
) {
  void queryClient.invalidateQueries({ queryKey: ["orders"] });
  if (orderId) {
    void queryClient.invalidateQueries({ queryKey: queryKeys.order(orderId) });
  }
  void queryClient.invalidateQueries({ queryKey: queryKeys.itemRuns(itemId) });
  void queryClient.invalidateQueries({
    queryKey: queryKeys.itemParameters(itemId),
  });
  void queryClient.invalidateQueries({
    queryKey: queryKeys.itemComparisons(itemId),
  });
  void queryClient.invalidateQueries({
    queryKey: queryKeys.itemVerdict(itemId),
  });
}

function apiUrl(path: string) {
  return new URL(`${env.apiBaseUrl}${path}`, window.location.origin).toString();
}

function parseSseBlock(block: string): {
  id: number;
  event: string;
  data: Record<string, unknown>;
} | null {
  let id: number | null = null;
  let event = "message";
  const data: string[] = [];

  for (const line of block.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue;
    const index = line.indexOf(":");
    const field = index === -1 ? line : line.slice(0, index);
    const value = index === -1 ? "" : line.slice(index + 1).trimStart();
    if (field === "id") id = Number(value);
    if (field === "event") event = value;
    if (field === "data") data.push(value);
  }

  if (id === null || Number.isNaN(id) || !data.length) return null;
  return { id, event, data: JSON.parse(data.join("\n")) };
}

function toPipelineEventMessage(
  parsed: ReturnType<typeof parseSseBlock>,
): PipelineEventMessage | null {
  if (!parsed) return null;
  const progress = parsed.data.progress as
    | PipelineEventMessage["progress"]
    | undefined;
  return {
    id: parsed.id,
    event: parsed.event,
    checkItemId: String(parsed.data.checkItemId ?? ""),
    pipelineRunId: String(parsed.data.pipelineRunId ?? ""),
    stage:
      typeof parsed.data.stage === "string" ? parsed.data.stage : null,
    status:
      typeof parsed.data.status === "string" ? parsed.data.status : null,
    occurredAt: String(parsed.data.occurredAt ?? ""),
    progress,
  };
}

export async function readSse(
  stream: ReadableStream<Uint8Array>,
  onMessage: (message: PipelineEventMessage) => void,
) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminal = false;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split(/\r?\n\r?\n/);
    buffer = blocks.pop() ?? "";
    for (const block of blocks) {
      const message = toPipelineEventMessage(parseSseBlock(block));
      if (!message) continue;
      onMessage(message);
      if (TERMINAL_PIPELINE_EVENTS.has(message.event)) terminal = true;
    }
  }

  return terminal;
}

// --- Дашборд и заявки (ордерный домен Фазы 3) -------------------------------

/** Ответ `GET /dashboard` (docs/api/dashboard/README.md). */
interface DashboardResponse {
  period: { from: string; to: string; days: number };
  kpis: DashboardData["kpis"];
  recentCheckBatches: Array<{
    id: string;
    number: string;
    customer: string;
    status: string;
    statusLabel: string;
    progress: number;
    createdAt: string;
  }>;
}

/**
 * Приводит ответ API к view-model экрана.
 *
 * Имена полей уже camelCase (соглашение API_CONVENTIONS §1), но часть названа
 * иначе: `recentCheckBatches` → `latestOrders`, `customer` → `client`,
 * `createdAt` → `date`. Статусы приходят в верхнем регистре.
 */
function toDashboardData(response: DashboardResponse): DashboardData {
  return {
    kpis: response.kpis,
    latestOrders: response.recentCheckBatches.map((batch) => ({
      id: batch.id,
      number: batch.number,
      client: batch.customer,
      status: batch.status.toLowerCase() as OrderSummary["status"],
      statusLabel: batch.statusLabel,
      progress: batch.progress,
      date: batch.createdAt,
    })),
  };
}

export function useDashboard() {
  return useQuery({
    queryKey: queryKeys.dashboard,
    queryFn: async () =>
      toDashboardData(await getData<DashboardResponse>("/dashboard")),
  });
}

/** Ответ пакета проверки (`check-batches`, docs/api/check-batches/README.md). */
interface BatchResponse {
  id: string;
  tenderId: string | null;
  title: string | null;
  customer: string | null;
  supplier: string | null;
  /** Точный статус FSM пакета, а не укрупнённый статус экрана. */
  status: string;
  finalVerdict: string;
  version: number;
  author: { id: string; displayName: string };
  items: Array<{
    id: string;
    positionNo: number;
    name: string;
    status: string;
  }>;
  createdAt: string;
  updatedAt: string;
}

/**
 * Статус пакета в базе → укрупнённый статус экрана.
 *
 * Ответ `check-batches` отдаёт точное состояние FSM (`QUEUED`, `RUNNING`,
 * `REVIEW_REQUIRED`, ...), а списку заявок нужна укрупнённая картина. Таблица
 * повторяет `STATUS_TO_API` бэкенда — там же собран статус для дашборда.
 */
const ORDER_STATUS: Record<string, OrderStatus> = {
  DRAFT: "draft",
  QUEUED: "active",
  RUNNING: "active",
  REVIEW_REQUIRED: "confirmation",
  COMPLETED: "closed",
  CANCELLED: "closed",
  ARCHIVED: "archived",
};

const ORDER_STATUS_LABELS: Record<OrderStatus, string> = {
  draft: "Черновик",
  active: "В работе",
  confirmation: "На подтверждении",
  closed: "Завершена",
  archived: "В архиве",
};

/** Статусы позиции, означающие, что работа по ней закончена. */
const ITEM_FINISHED = ["COMPLETED", "CANCELLED"];

const ITEM_STATUS_LABELS: Record<string, string> = {
  DRAFT: "Черновик",
  QUEUED: "В очереди",
  INGESTING: "Приём документов",
  NEEDS_INPUT: "Нужны данные",
  EXTRACTING: "Извлечение",
  VERIFYING_SOURCES: "Проверка источников",
  NORMALIZING: "Нормализация",
  AWAITING_PARAMETER_REVIEW: "Ожидает проверки параметров",
  MATCHING: "Сопоставление",
  REVIEW_REQUIRED: "Требует проверки",
  READY_FOR_MANAGER_REVIEW: "Готово к проверке менеджером",
  VERDICT_APPROVED: "Вердикт утверждён",
  REPORTING: "Формирование отчёта",
  COMPLETED: "Завершена",
  FAILED: "Ошибка",
  CANCELLED: "Отменена",
};

function itemStatusLabel(status: string) {
  return ITEM_STATUS_LABELS[status] ?? status;
}

/**
 * Номер заявки. В схеме его нет: пакет опознаётся названием, а без него —
 * началом идентификатора. Тем же правилом номер собирает дашборд на сервере.
 */
function orderNumber(batch: BatchResponse) {
  return batch.title || `З-${batch.id.slice(0, 8)}`;
}

/** Доля завершённых позиций — то же правило, что у дашборда на сервере. */
function orderProgress(batch: BatchResponse) {
  if (!batch.items.length) return 0;
  const finished = batch.items.filter((item) =>
    ITEM_FINISHED.includes(item.status),
  ).length;
  return Math.round((finished * 100) / batch.items.length);
}

function toOrderSummary(batch: BatchResponse): OrderSummary {
  // Незнакомый статус не должен ронять весь список: экран показывает его
  // кодом как есть, а оформление берёт от «в работе».
  const known = ORDER_STATUS[batch.status];
  const status = known ?? "active";
  return {
    id: batch.id,
    number: orderNumber(batch),
    client: batch.customer || "—",
    tenderId: batch.tenderId ?? undefined,
    status,
    statusLabel: known ? ORDER_STATUS_LABELS[known] : batch.status,
    progress: orderProgress(batch),
    date: batch.createdAt,
  };
}

function toOrderCard(batch: BatchResponse): OrderCard {
  return {
    ...toOrderSummary(batch),
    title: batch.title,
    supplier: batch.supplier,
    author: batch.author.displayName,
    version: batch.version,
    finalVerdict: batch.finalVerdict,
    updatedAt: batch.updatedAt,
    items: batch.items.map((item) => ({
      id: item.id,
      positionNo: item.positionNo,
      name: item.name,
      status: item.status,
      statusLabel: itemStatusLabel(item.status),
    })),
  };
}

export function useOrders(filters: { status?: string; search?: string }) {
  return useQuery({
    queryKey: queryKeys.orders(filters),
    queryFn: async () => {
      // `all` — состояние фильтра на экране, а не значение контракта: сервер
      // ждёт отсутствия параметра.
      const params: Record<string, string> = {};
      if (filters.status && filters.status !== "all") {
        params.status = filters.status.toUpperCase();
      }
      if (filters.search) params.search = filters.search;
      const page = await getData<{ items: BatchResponse[] }>(
        "/check-batches",
        params,
      );
      return page.items.map(toOrderSummary);
    },
  });
}

export function useOrder(orderId: string) {
  return useQuery({
    queryKey: queryKeys.order(orderId),
    queryFn: async () =>
      toOrderCard(await getData<BatchResponse>(`/check-batches/${orderId}`)),
    enabled: Boolean(orderId),
  });
}

/**
 * Контур КП (задача №7): расчётный лист заявки.
 *
 * `GET /orders/{orderId}` принимает id заявки (= id check-batch) и
 * резолвляет связанный PriceRequest. Лист — производная заявки, существует
 * с её создания; пока расчёт не создан, коллекции вкладок пустые, и
 * страница показывает пустые состояния, а не выдуманные данные.
 */
export function useOrderCommercials(orderId: string) {
  return useQuery({
    queryKey: queryKeys.orderCommercials(orderId),
    queryFn: () => getData<OrderCommercials>(`/orders/${orderId}`),
    enabled: Boolean(orderId),
  });
}

/** Заголовок идемпотентности: повтор запроса не должен плодить сущности. */
function idempotency() {
  return { "Idempotency-Key": crypto.randomUUID() };
}

export function useCreateOrder() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async (payload: {
      customer: string;
      title?: string;
      supplier?: string;
      tenderId?: string;
      items: Array<{ name: string }>;
    }) => {
      const response = await apiClient.post<BatchResponse>(
        "/check-batches",
        payload,
        { headers: idempotency() },
      );
      return toOrderCard(response.data);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.dashboard });
    },
  });
}

export function useDeleteOrder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (orderId: string) => {
      await apiClient.delete(`/check-batches/${orderId}`);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
      void queryClient.invalidateQueries({ queryKey: queryKeys.dashboard });
    },
  });
}

/** Запуск проверки всего пакета: `202` с разбором принятых и отклонённых. */
export function useRunOrder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (orderId: string) => {
      const response = await apiClient.post<{
        started: Array<{ checkItemId: string }>;
        rejected: Array<{ checkItemId: string; code: string; message: string }>;
      }>(`/check-batches/${orderId}/run`, {}, { headers: idempotency() });
      return response.data;
    },
    onSuccess: (_data, orderId) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.order(orderId) });
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
    },
  });
}

export function useCancelOrder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (orderId: string) => {
      await apiClient.post(`/check-batches/${orderId}/cancel`, {});
    },
    onSuccess: (_data, orderId) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.order(orderId) });
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
    },
  });
}

// --- Действия и правки заявки (прайс/КП контур Фазы 2, вкладки шага 07) ------

/**
 * Мутация контура КП меняет расчётный лист, а не саму заявку Фазы 3 —
 * ответ `OrderDetail` не кладётся в кэш `queryKeys.order` (там живёт
 * `OrderCard` с канонической шапкой), а инвалидирует связанные запросы.
 */
function invalidateOrderContours(
  queryClient: QueryClient,
  orderId: string,
): void {
  void queryClient.invalidateQueries({ queryKey: queryKeys.order(orderId) });
  void queryClient.invalidateQueries({ queryKey: queryKeys.orderCommercials(orderId) });
  void queryClient.invalidateQueries({ queryKey: ["orders"] });
}

export function useOrderAction() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      orderId,
      action,
      payload,
    }: {
      orderId: string;
      action:
        | "send"
        | "cancel-send"
        | "close"
        | "archive"
        | "complete-check"
        | "confirm-quote";
      payload?: Record<string, unknown>;
    }) => {
      const response = await apiClient.post<OrderDetail>(
        `/orders/${orderId}/${action}`,
        payload,
      );
      return response.data;
    },
    onSuccess: (_data, { orderId }) => {
      invalidateOrderContours(queryClient, orderId);
      void queryClient.invalidateQueries({ queryKey: queryKeys.dashboard });
    },
  });
}

export function useUpdateQuote() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      orderId,
      payload,
    }: {
      orderId: string;
      payload: {
        vatRate?: number;
        specialDiscount?: number;
        supplierId?: string;
        reason?: string;
      };
    }) => {
      const response = await apiClient.patch<OrderDetail>(
        `/orders/${orderId}/quote`,
        payload,
      );
      return response.data;
    },
    onSuccess: (_data, { orderId }) => {
      invalidateOrderContours(queryClient, orderId);
    },
  });
}

export function useUploadOrderFile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      orderId,
      fileName,
      kind,
    }: {
      orderId: string;
      fileName: string;
      kind: string;
    }) => {
      const response = await apiClient.post<OrderDetail>(
        `/orders/${orderId}/files`,
        { fileName, kind },
      );
      return response.data;
    },
    onSuccess: (_data, { orderId }) => {
      invalidateOrderContours(queryClient, orderId);
    },
  });
}

/**
 * Загрузка ценового документа со страницы заявки (мостик Ф2→Ф3).
 *
 * Идемпотентность держит ключ: повторная отправка того же файла возвращает
 * `replayed` вместо второго разбора. Инвалидация — и контуры заявки, и
 * позиции: после разбора документ появится в подборе именно там, а сам файл
 * до конца разбора на карточке не виден.
 */
export function useUploadOrderPriceDocument() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: ({
      batchId,
      ...input
    }: UploadOrderPriceDocumentInput & { batchId: string }) =>
      uploadOrderPriceDocument(batchId, input),
    onSuccess: (_data, { batchId }) => {
      invalidateOrderContours(queryClient, batchId);
      void queryClient.invalidateQueries({ queryKey: ["check-items"] });
    },
  });
}

/**
 * Предложения прайсов по строке заявки (задача №7, дополнение): поиск по
 * имени позиции, а не по дефисному обозначению — выбор исполнения остаётся
 * за менеджером. Запрос живёт, пока открыт пикер строки.
 */
export function useOrderLineOffers(orderId: string, lineId: string | null) {
  return useQuery({
    queryKey: queryKeys.orderLineOffers(orderId, lineId ?? ""),
    queryFn: () =>
      getData<OrderLineOffers>(`/orders/${orderId}/lines/${lineId}/offers`),
    enabled: Boolean(orderId) && Boolean(lineId),
  });
}

/**
 * Собрать КП из выбранных менеджером предложений: `POST /orders/{id}/quote`.
 *
 * Контракт бэка — snake_case selections; маппинг живёт здесь, чтобы виджет
 * оперировал фронтовыми типами.
 */
export function useAssembleQuote() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      orderId,
      selections,
    }: {
      orderId: string;
      selections: QuoteLineSelection[];
    }) => {
      const response = await apiClient.post<{
        id: string;
        total_amount?: number;
        skipped?: Array<{ source_line: string; reason: string }>;
      }>(`/orders/${orderId}/quote`, {
        selections: selections.map((item) => ({
          line_id: item.lineId,
          canonical_id: item.canonicalId,
          price_item_id: item.priceItemId,
          quantity: item.quantity,
        })),
      });
      return response.data;
    },
    onSuccess: (_data, { orderId }) => {
      invalidateOrderContours(queryClient, orderId);
    },
  });
}

export function useItemDocuments(itemId: string) {
  return useQuery({
    queryKey: queryKeys.itemDocuments(itemId),
    queryFn: () => getData<ItemDocuments>(`/check-items/${itemId}/documents`),
    enabled: Boolean(itemId),
  });
}

export function useItemRuns(itemId: string) {
  return useQuery({
    queryKey: queryKeys.itemRuns(itemId),
    queryFn: () => getData<ItemRun[]>(`/check-items/${itemId}/runs`),
    enabled: Boolean(itemId),
  });
}

/**
 * Загрузка документа позиции: multipart на `POST /uploads`.
 *
 * `Content-Type` снимается намеренно — его вместе с boundary ставит браузер,
 * а заголовок клиента по умолчанию (`application/json`) сломал бы разбор.
 */
export function useUploadItemDocument() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async ({
      itemId,
      file,
      role,
    }: {
      itemId: string;
      file: File;
      role: "REQUIREMENT" | "FACT";
    }) => {
      const form = new FormData();
      form.append("file", file);
      form.append("checkItemId", itemId);
      form.append("role", role);
      const response = await apiClient.post("/uploads", form, {
        headers: { ...idempotency(), "Content-Type": undefined },
      });
      return response.data;
    },
    onSuccess: (_data, { itemId }) => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.itemDocuments(itemId),
      });
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
    },
  });
}

export function useRunItem() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async ({ itemId }: { itemId: string; orderId: string }) => {
      const response = await apiClient.post(
        `/check-items/${itemId}/run`,
        {},
        { headers: idempotency() },
      );
      return response.data;
    },
    onSuccess: (_data, { itemId, orderId }) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.order(orderId) });
      void queryClient.invalidateQueries({
        queryKey: queryKeys.itemRuns(itemId),
      });
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
    },
  });
}

export function useCancelItem() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async ({ itemId }: { itemId: string; orderId: string }) => {
      await apiClient.post(`/check-items/${itemId}/cancel`, {});
    },
    onSuccess: (_data, { itemId, orderId }) => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.order(orderId) });
      void queryClient.invalidateQueries({
        queryKey: queryKeys.itemRuns(itemId),
      });
      void queryClient.invalidateQueries({ queryKey: ["orders"] });
    },
  });
}

export function usePipelineEvents(itemId: string, enabled: boolean) {
  const queryClient = useQueryClient();
  const [events, setEvents] = useState<PipelineEventMessage[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const lastEventId = useRef(0);

  useEffect(() => {
    if (!itemId || !enabled) {
      setConnected(false);
      return;
    }

    const controller = new AbortController();
    let stopped = false;
    lastEventId.current = 0;
    setEvents([]);
    setError(null);

    const connect = async () => {
      while (!stopped) {
        try {
          const url = new URL(apiUrl(`/check-items/${itemId}/events`));
          if (lastEventId.current > 0) {
            url.searchParams.set("lastEventId", String(lastEventId.current));
          }
          const token = getAccessToken();
          const response = await fetch(url, {
            credentials: "include",
            headers: {
              Accept: "text/event-stream",
              ...(token ? { Authorization: `Bearer ${token}` } : {}),
            },
            signal: controller.signal,
          });

          if (!response.ok || !response.body) {
            throw new Error(`SSE ${response.status}`);
          }

          setConnected(true);
          const terminal = await readSse(response.body, (message) => {
            if (message.id <= lastEventId.current) return;
            lastEventId.current = message.id;
            setEvents((current) => [...current, message].slice(-40));
            if (TERMINAL_PIPELINE_EVENTS.has(message.event)) {
              invalidateItemWorkflow(queryClient, itemId);
            }
          });
          if (terminal) return;
        } catch (cause) {
          if (controller.signal.aborted || stopped) return;
          setError(
            cause instanceof Error
              ? cause.message
              : "Не удалось получить события pipeline",
          );
        } finally {
          setConnected(false);
        }

        await new Promise((resolve) => window.setTimeout(resolve, 1500));
      }
    };

    void connect();

    return () => {
      stopped = true;
      controller.abort();
      setConnected(false);
    };
  }, [enabled, itemId, queryClient]);

  return {
    connected,
    error,
    events,
    lastEventId: lastEventId.current,
    latest: events.at(-1) ?? null,
  };
}

export function useItemParameters(itemId: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.itemParameters(itemId),
    queryFn: () =>
      getData<ItemParametersPage>(`/check-items/${itemId}/parameters`, {
        includeExcluded: "true",
      }),
    enabled: Boolean(itemId) && enabled,
  });
}

export function useAddItemParameter() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async ({
      itemId,
      role,
      name,
      valueText,
      unitText,
      reasonCode,
      comment,
    }: {
      itemId: string;
      orderId: string;
      role: "REQUIREMENT" | "FACT";
      name: string;
      valueText?: string | null;
      unitText?: string | null;
      reasonCode: string;
      comment: string;
    }) => {
      const response = await apiClient.post<ItemParameter>(
        `/check-items/${itemId}/parameters`,
        {
          role,
          name,
          valueText,
          unitText,
          reasonCode,
          comment,
        },
      );
      return response.data;
    },
    onSuccess: (_data, { itemId, orderId }) => {
      invalidateItemWorkflow(queryClient, itemId, orderId);
    },
  });
}

export function useReviseParameter() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async ({
      parameterId,
      expectedVersion,
      name,
      valueText,
      unitText,
      reasonCode,
      comment,
    }: {
      itemId: string;
      orderId: string;
      parameterId: string;
      expectedVersion: number;
      name?: string | null;
      valueText?: string | null;
      unitText?: string | null;
      reasonCode: string;
      comment: string;
    }) => {
      const response = await apiClient.post<ItemParameter>(
        `/parameters/${parameterId}/revisions`,
        {
          expectedVersion,
          name,
          valueText,
          unitText,
          reasonCode,
          comment,
        },
      );
      return response.data;
    },
    onSuccess: (_data, { itemId, orderId }) => {
      invalidateItemWorkflow(queryClient, itemId, orderId);
    },
  });
}

export function useExcludeParameter() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async ({
      parameterId,
      expectedVersion,
      reasonCode,
      comment,
    }: {
      itemId: string;
      orderId: string;
      parameterId: string;
      expectedVersion: number;
      reasonCode: string;
      comment: string;
    }) => {
      const response = await apiClient.post<ItemParameter>(
        `/parameters/${parameterId}/exclude`,
        { expectedVersion, reasonCode, comment },
      );
      return response.data;
    },
    onSuccess: (_data, { itemId, orderId }) => {
      invalidateItemWorkflow(queryClient, itemId, orderId);
    },
  });
}

export function useRestoreParameter() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async ({
      parameterId,
      expectedVersion,
      reasonCode,
      comment,
    }: {
      itemId: string;
      orderId: string;
      parameterId: string;
      expectedVersion: number;
      reasonCode: string;
      comment: string;
    }) => {
      const response = await apiClient.post<ItemParameter>(
        `/parameters/${parameterId}/restore`,
        { expectedVersion, reasonCode, comment },
      );
      return response.data;
    },
    onSuccess: (_data, { itemId, orderId }) => {
      invalidateItemWorkflow(queryClient, itemId, orderId);
    },
  });
}

export function useConfirmItemParameters() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async ({
      itemId,
    }: {
      itemId: string;
      orderId: string;
    }) => {
      const response = await apiClient.post<RunAcceptedResponse>(
        `/check-items/${itemId}/parameters/confirm`,
        {},
        { headers: idempotency() },
      );
      return response.data;
    },
    onSuccess: (_data, { itemId, orderId }) => {
      invalidateItemWorkflow(queryClient, itemId, orderId);
    },
  });
}

export function useItemComparisons(itemId: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.itemComparisons(itemId),
    queryFn: () =>
      getData<ComparisonResult[]>(`/check-items/${itemId}/comparisons`),
    enabled: Boolean(itemId) && enabled,
  });
}

export function useComparisonCandidates(
  comparisonId: string | null,
  enabled = true,
) {
  return useQuery({
    queryKey: queryKeys.comparisonCandidates(comparisonId ?? ""),
    queryFn: () =>
      getData<ComparisonCandidates>(`/comparisons/${comparisonId}/candidates`),
    enabled: Boolean(comparisonId) && enabled,
  });
}

export function useOverrideComparison() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async ({
      comparisonId,
      expectedVersion,
      status,
      reasonCode,
      comment,
    }: {
      itemId: string;
      orderId: string;
      comparisonId: string;
      expectedVersion: number;
      status: ComparisonResult["effectiveStatus"];
      reasonCode?: string;
      comment: string;
    }) => {
      const response = await apiClient.post<ComparisonResult>(
        `/comparisons/${comparisonId}/override`,
        {
          expectedVersion,
          status,
          reasonCode,
          comment,
        },
      );
      return response.data;
    },
    onSuccess: (_data, { itemId, orderId }) => {
      invalidateItemWorkflow(queryClient, itemId, orderId);
    },
  });
}

export function useCancelComparisonOverride() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      comparisonId,
      expectedVersion,
    }: {
      itemId: string;
      orderId: string;
      comparisonId: string;
      expectedVersion: number;
    }) => {
      const response = await apiClient.delete<ComparisonResult>(
        `/comparisons/${comparisonId}/override`,
        { headers: { "If-Match": `"${expectedVersion}"` } },
      );
      return response.data;
    },
    onSuccess: (_data, { itemId, orderId }) => {
      invalidateItemWorkflow(queryClient, itemId, orderId);
    },
  });
}

export function useSelectComparisonCandidate() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async ({
      comparisonId,
      expectedVersion,
      factParameterId,
      comment,
    }: {
      itemId: string;
      orderId: string;
      comparisonId: string;
      expectedVersion: number;
      factParameterId: string;
      comment?: string;
    }) => {
      const response = await apiClient.post<ComparisonResult>(
        `/comparisons/${comparisonId}/select-candidate`,
        {
          expectedVersion,
          factParameterId,
          comment,
          recalculate: true,
        },
      );
      return response.data;
    },
    onSuccess: (_data, { itemId, orderId }) => {
      invalidateItemWorkflow(queryClient, itemId, orderId);
    },
  });
}

export function useRecalculateComparisons() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      itemId,
    }: {
      itemId: string;
      orderId: string;
    }) => {
      const response = await apiClient.post<ComparisonResult[]>(
        `/check-items/${itemId}/comparisons/recalculate`,
      );
      return response.data;
    },
    onSuccess: (_data, { itemId, orderId }) => {
      invalidateItemWorkflow(queryClient, itemId, orderId);
    },
  });
}

export function useItemVerdict(itemId: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.itemVerdict(itemId),
    queryFn: () => getData<ItemVerdict>(`/check-items/${itemId}/verdict`),
    enabled: Boolean(itemId) && enabled,
  });
}

export function useDecideItemVerdict() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async ({
      itemId,
      expectedVersion,
      verdict,
      comment,
    }: {
      itemId: string;
      orderId: string;
      expectedVersion: number;
      verdict: Exclude<VerdictValue, "PENDING">;
      comment?: string;
    }) => {
      const response = await apiClient.post<ItemVerdict>(
        `/check-items/${itemId}/verdict`,
        {
          expectedVersion,
          verdict,
          comment,
        },
      );
      return response.data;
    },
    onSuccess: (_data, { itemId, orderId }) => {
      invalidateItemWorkflow(queryClient, itemId, orderId);
    },
  });
}

export function useRequestItemReport() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async ({
      itemId,
    }: {
      itemId: string;
      orderId: string;
    }) => {
      const response = await apiClient.post<ReportRequestResult>(
        `/check-items/${itemId}/reports`,
        {
          formats: ["XLSX"],
          language: "ru",
          includeEvidenceQuotes: true,
          publishToBitrix: false,
        },
        { headers: idempotency() },
      );
      return response.data;
    },
    onSuccess: (_data, { itemId, orderId }) => {
      invalidateItemWorkflow(queryClient, itemId, orderId);
    },
  });
}

export function useBatchVerdict(batchId: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.batchVerdict(batchId),
    queryFn: () => getData<ItemVerdict>(`/check-batches/${batchId}/verdict`),
    enabled: Boolean(batchId) && enabled,
  });
}

export function useDecideBatchVerdict() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async ({
      orderId,
      expectedVersion,
      verdict,
      comment,
    }: {
      orderId: string;
      expectedVersion: number;
      verdict: Exclude<VerdictValue, "PENDING">;
      comment?: string;
    }) => {
      const response = await apiClient.post<ItemVerdict>(
        `/check-batches/${orderId}/verdict`,
        { expectedVersion, verdict, comment },
      );
      return response.data;
    },
    onSuccess: (_data, { orderId }) => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.batchVerdict(orderId),
      });
      void queryClient.invalidateQueries({ queryKey: queryKeys.order(orderId) });
    },
  });
}

export function useRequestBatchReport() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    // Сводный отчёт собирается синхронно и отвечает готовым снимком, поэтому
    // опрашивать состояние, как у позиции, не нужно.
    mutationFn: async ({ orderId }: { orderId: string }) => {
      const response = await apiClient.post<ReportStatus>(
        `/check-batches/${orderId}/reports`,
        {
          formats: ["XLSX"],
          language: "ru",
          includeEvidenceQuotes: true,
          publishToBitrix: false,
        },
        { headers: idempotency() },
      );
      return response.data;
    },
    onSuccess: (_data, { orderId }) => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.batchVerdict(orderId),
      });
      void queryClient.invalidateQueries({ queryKey: queryKeys.order(orderId) });
    },
  });
}

export function useReport(reportId: string | null, enabled = true) {
  return useQuery({
    queryKey: queryKeys.report(reportId ?? ""),
    queryFn: () => getData<ReportStatus>(`/reports/${reportId}`),
    enabled: Boolean(reportId) && enabled,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && ["COMPLETED", "PARTIALLY_COMPLETED", "FAILED"].includes(status)
        ? false
        : 2_000;
    },
  });
}

export function useDownloadReportArtifact() {
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async ({
      artifactId,
      fileName,
    }: {
      artifactId: string;
      fileName: string;
    }) => {
      const response = await apiClient.get<Blob>(
        `/report-artifacts/${artifactId}/download`,
        { responseType: "blob" },
      );
      const disposition = response.headers["content-disposition"] as
        | string
        | undefined;
      const encodedName = disposition?.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
      return {
        blob: response.data,
        fileName: encodedName ? decodeURIComponent(encodedName) : fileName,
        mediaType:
          (response.headers["content-type"] as string | undefined) ||
          response.data.type,
      };
    },
  });
}

export function useChecks(filters: {
  search?: string;
  status?: string;
  date?: string;
}) {
  // @TODO: заменить на реальный эндпоинт GET /checks
  return useQuery({
    queryKey: queryKeys.checks(filters),
    queryFn: () => getData<CheckHistoryItem[]>("/checks", filters),
  });
}

export function useSuppliers() {
  // @TODO: заменить на реальный эндпоинт GET /suppliers
  return useQuery({
    queryKey: queryKeys.suppliers,
    queryFn: () => getData<Supplier[]>("/suppliers"),
  });
}

export function useCreateSupplier() {
  const queryClient = useQueryClient();
  // @TODO: заменить на реальный эндпоинт POST /suppliers
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async (payload: { name: string; email: string }) => {
      const response = await apiClient.post<Supplier>("/suppliers", payload);
      return response.data;
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.suppliers }),
  });
}

export function useUploadSupplierFile() {
  const queryClient = useQueryClient();
  // @TODO: заменить на реальный эндпоинт POST /suppliers/{supplierId}/files
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async ({
      supplierId,
      fileName,
      kind,
    }: {
      supplierId: string;
      fileName: string;
      kind: string;
    }) => {
      const response = await apiClient.post<Supplier>(
        `/suppliers/${supplierId}/files`,
        { fileName, kind },
      );
      return response.data;
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.suppliers }),
  });
}

export function useProducts(filters: { category?: string; search?: string }) {
  // @TODO: заменить на реальный эндпоинт GET /products
  return useQuery({
    queryKey: queryKeys.products(filters),
    queryFn: () => getData<Product[]>("/products", filters),
  });
}

export function useCertificates() {
  // @TODO: заменить на реальный эндпоинт GET /certificates
  return useQuery({
    queryKey: queryKeys.certificates,
    queryFn: () => getData<Certificate[]>("/certificates"),
  });
}

export function useUploadCertificate() {
  const queryClient = useQueryClient();
  // @TODO: заменить на реальный эндпоинт POST /certificates
  return useMutation({
    mutationFn: async (payload: {
      supplier: string;
      product: string;
      number: string;
    }) => {
      const response = await apiClient.post<Certificate>(
        "/certificates",
        payload,
      );
      return response.data;
    },
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.certificates }),
  });
}

/** Ответ `GET /dictionary` — проекция опубликованной версии каталога. */
interface DictionaryResponse {
  catalogVersion: string | null;
  parameters: Array<{
    id: string;
    code: string;
    canonical: string;
    valueKind: string;
    dimension: string | null;
    unit: string | null;
    synonyms: string[];
  }>;
  units: Array<{
    id: string;
    code: string;
    symbol: string;
    dimension: string;
    synonyms: string[];
    baseUnit: string | null;
    scale: string | null;
    offset: string | null;
  }>;
  materials: Array<{
    id: string;
    code: string;
    name: string;
    path: string;
    parentId: string | null;
  }>;
}

/**
 * Запись перевода в базовую единицу.
 *
 * Множители приходят строками (API_CONVENTIONS §2): в число их превращать
 * нельзя — 0.001 в float уже не то же самое число. Сдвиг показывается только
 * когда он ненулевой, иначе «× 1 + 0» засоряло бы каждую строку температур.
 */
function toConversion(unit: DictionaryResponse["units"][number]) {
  if (unit.scale === null) return null;
  const offset = Number(unit.offset ?? 0);
  return offset === 0 ? `× ${unit.scale}` : `× ${unit.scale} + ${unit.offset}`;
}

/**
 * Собирает дерево материалов из плоского списка.
 *
 * API отдаёт узлы с `parentId` и `path`, отсортированные по пути, поэтому
 * родитель встречается раньше детей и одного прохода достаточно. Узел с
 * неизвестным родителем (родитель неактивен и в проекцию не попал) остаётся
 * корневым, а не теряется.
 */
function toMaterialTree(
  nodes: DictionaryResponse["materials"],
): DictionaryData["materials"] {
  const byId = new Map<
    string,
    DictionaryData["materials"][number]
  >(
    nodes.map((node) => [
      node.id,
      { id: node.id, code: node.code, name: node.name, children: [] },
    ]),
  );
  const roots: DictionaryData["materials"] = [];
  for (const node of nodes) {
    const item = byId.get(node.id)!;
    const parent = node.parentId ? byId.get(node.parentId) : undefined;
    if (parent) parent.children.push(item);
    else roots.push(item);
  }
  return roots;
}

function toDictionaryData(response: DictionaryResponse): DictionaryData {
  return {
    catalogVersion: response.catalogVersion,
    nomenclature: response.parameters,
    units: response.units.map((unit) => ({
      id: unit.id,
      code: unit.code,
      symbol: unit.symbol,
      dimension: unit.dimension,
      conversion: toConversion(unit),
      baseUnit: unit.baseUnit,
      synonyms: unit.synonyms,
    })),
    materials: toMaterialTree(response.materials),
  };
}

export function useDictionary() {
  return useQuery({
    queryKey: queryKeys.dictionary,
    queryFn: async () =>
      toDictionaryData(await getData<DictionaryResponse>("/dictionary")),
  });
}

/** Свои предложения синонимов: что предложено, принято и отклонено. */
export function useMySuggestions() {
  return useQuery({
    queryKey: queryKeys.mySuggestions,
    queryFn: () => getData<SuggestionResponse[]>("/dictionary/synonyms"),
  });
}

export function useSuggestSynonym() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: {
      canonical: string;
      synonym: string;
      // Стабильный идентификатор параметра: имя ключом не является — оно
      // меняется при переименовании и может совпадать у разных параметров.
      canonicalParameterId: string;
    }) => {
      const response = await apiClient.post<SuggestionResponse>(
        "/dictionary/synonyms",
        payload,
      );
      return response.data;
    },
    // Опубликованный каталог от предложения не меняется — синоним заводится в
    // черновике и появится в словаре после публикации. Инвалидируется список
    // предложений на экране «Управление», ради которого предложение и делается.
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.admin });
      void queryClient.invalidateQueries({ queryKey: queryKeys.mySuggestions });
    },
    // Отказ показывается локально (словарь: catch вокруг mutateAsync с
    // toast.error из envelope) — глобальный тост подавлен.
    meta: { suppressGlobalError: true },
  });
}

export function useAdminData() {
  // @TODO: заменить на реальный эндпоинт GET /admin
  return useQuery({
    queryKey: queryKeys.admin,
    queryFn: () => getData<AdminData>("/admin"),
  });
}

export function useUpdateSettings() {
  const queryClient = useQueryClient();
  // @TODO: заменить на реальный эндпоинт PATCH /admin/settings
  return useMutation({
    mutationFn: async (payload: AdminData["settings"]) => {
      const response = await apiClient.patch<AdminData>(
        "/admin/settings",
        payload,
      );
      return response.data;
    },
    onSuccess: (data) => queryClient.setQueryData(queryKeys.admin, data),
  });
}

export function useSynonymDecision() {
  const queryClient = useQueryClient();
  // @TODO: заменить на реальный эндпоинт PATCH /admin/synonyms/{synonymId}
  return useMutation({
    mutationFn: async ({
      synonymId,
      status,
    }: {
      synonymId: string;
      status: "accepted" | "rejected";
    }) => {
      const response = await apiClient.patch<AdminData>(
        `/admin/synonyms/${synonymId}`,
        { status },
      );
      return response.data;
    },
    onSuccess: (data) => queryClient.setQueryData(queryKeys.admin, data),
  });
}

/**
 * Ответ API → view-model экрана. Мутации профиля возвращают тот же payload,
 * что и `GET /profile`, поэтому преобразование одно на всех: иначе после
 * сохранения прав уведомления теряли бы `orderId`.
 */
function toProfileData(response: ProfileResponse): ProfileData {
  return {
    name: response.name,
    email: response.email,
    initials: response.initials,
    role: response.role,
    notifications: response.notifications.map((item) => ({
      id: item.id,
      orderId: item.checkBatchId,
      title: item.title,
      createdAt: item.createdAt,
      type: item.type,
      status: item.status,
    })),
    permissions: response.permissions,
    permissionCatalog: response.permissionCatalog,
    mandatoryPermissions: response.mandatoryPermissions,
  };
}

export function useProfile(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.profile,
    queryFn: async () => toProfileData(await getData<ProfileResponse>("/profile")),
    enabled: options?.enabled ?? true,
  });
}

export function useNotificationDecision() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      notificationId,
      status,
    }: {
      notificationId: string;
      status: "approved" | "rejected";
    }) => {
      const response = await apiClient.patch<ProfileResponse>(
        `/profile/notifications/${notificationId}`,
        { status },
      );
      return toProfileData(response.data);
    },
    onSuccess: (data) => queryClient.setQueryData(queryKeys.profile, data),
  });
}

export function useSavePermissions() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (payload: { role: Role; permissions: string[] }) => {
      const response = await apiClient.patch<ProfileResponse>(
        "/profile/permissions",
        payload,
      );
      return toProfileData(response.data);
    },
    onSuccess: (data) => queryClient.setQueryData(queryKeys.profile, data),
  });
}

// --- Интеграция с Bitrix24 (docs/api/integrations/bitrix) -------------------

const BITRIX = "/integrations/bitrix";

/** Задание завершилось: опрашивать больше нечего. */
const BITRIX_JOB_TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED"]);

/**
 * Состояние интеграции. Экран прячет действия B24, когда она не настроена:
 * кнопка, которая гарантированно ответит 503, только вводит в заблуждение.
 */
export function useBitrixHealth(enabled = true) {
  return useQuery({
    queryKey: queryKeys.bitrixHealth,
    queryFn: () => getData<BitrixHealth>(`${BITRIX}/health`),
    enabled,
    // Конфигурация меняется перезапуском контейнера, а не в течение сессии.
    staleTime: 5 * 60_000,
    retry: false,
  });
}

export function useBitrixDeal(tenderId: string | null, enabled = true) {
  return useQuery({
    queryKey: queryKeys.bitrixDeal(tenderId ?? ""),
    queryFn: () => getData<BitrixDeal>(`${BITRIX}/deals/${tenderId}`),
    enabled: Boolean(tenderId) && enabled,
    retry: false,
  });
}

export function useBitrixDealFiles(tenderId: string | null, enabled = true) {
  return useQuery({
    queryKey: queryKeys.bitrixDealFiles(tenderId ?? ""),
    queryFn: () =>
      getData<BitrixDealFiles>(`${BITRIX}/deals/${tenderId}/files`),
    enabled: Boolean(tenderId) && enabled,
    retry: false,
  });
}

export function usePrepareBitrixDeal() {
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async ({
      tenderId,
      orderId,
      items,
    }: {
      tenderId: string;
      orderId: string;
      items: Array<{ positionNo: number; name: string; supplier: string | null }>;
    }) => {
      const response = await apiClient.post<BitrixPreparedDeal>(
        `${BITRIX}/deals/${tenderId}/prepare`,
        { checkBatchId: orderId, items },
        { headers: idempotency() },
      );
      return response.data;
    },
  });
}

export function useBitrixKitStatus(batchId: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.bitrixKitStatus(batchId),
    queryFn: () =>
      getData<BatchKitStatus>(
        `${BITRIX}/check-batches/${batchId}/status`,
      ),
    enabled: Boolean(batchId) && enabled,
    retry: false,
  });
}

export function useImportBitrixFiles() {
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async ({
      tenderId,
      orderId,
      files,
    }: {
      tenderId: string;
      orderId: string;
      files: BitrixImportSelection[];
    }) => {
      const response = await apiClient.post<{
        jobId: string;
        status: BitrixJob["status"];
        filesTotal: number;
      }>(
        `${BITRIX}/deals/${tenderId}/imports`,
        { checkBatchId: orderId, files },
        { headers: idempotency() },
      );
      return response.data;
    },
  });
}

export function usePublishToBitrix() {
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async ({
      orderId,
      reportArtifactIds,
      addTimelineComment = true,
    }: {
      orderId: string;
      reportArtifactIds: string[];
      addTimelineComment?: boolean;
    }) => {
      const response = await apiClient.post<{
        jobId: string;
        status: BitrixJob["status"];
        tenderId: string;
      }>(
        `${BITRIX}/check-batches/${orderId}/publish`,
        { reportArtifactIds, addTimelineComment, includeKeyMismatches: true },
        { headers: idempotency() },
      );
      return response.data;
    },
  });
}

/**
 * Состояние задания с опросом до терминального статуса.
 *
 * `imports` и `publications` отдают одну и ту же модель, поэтому путь выбирается
 * по типу задания, а не отдельными хуками.
 */
export function useBitrixJob(
  jobId: string | null,
  kind: "imports" | "publications" = "imports",
) {
  return useQuery({
    queryKey: queryKeys.bitrixJob(jobId ?? ""),
    queryFn: () => getData<BitrixJob>(`${BITRIX}/${kind}/${jobId}`),
    enabled: Boolean(jobId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      // RETRY_SCHEDULED опрашивается дальше: повтор поставит beat-задача.
      return status && BITRIX_JOB_TERMINAL.has(status) ? false : 2_000;
    },
  });
}

export function useRetryBitrixJob() {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: async (jobId: string) => {
      const response = await apiClient.post<BitrixJob>(
        `${BITRIX}/publications/${jobId}/retry`,
      );
      return response.data;
    },
    onSuccess: (job) =>
      queryClient.setQueryData(queryKeys.bitrixJob(job.id), job),
  });
}

export function useChangeRole() {
  const queryClient = useQueryClient();
  // @TODO: заменить на реальный эндпоинт PATCH /profile/role
  return useMutation({
    mutationFn: async (role: Role) => {
      const response = await apiClient.patch<ProfileData>("/profile/role", {
        role,
      });
      return response.data;
    },
    onSuccess: (data) => queryClient.setQueryData(queryKeys.profile, data),
  });
}
