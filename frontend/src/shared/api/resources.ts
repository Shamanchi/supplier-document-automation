import type {
  CreatePriceImportInput,
  ImportedItemsPageDto,
  LlmProfilesDto,
  OrderPriceDocumentAcceptedDto,
  PriceImportAcceptedDto,
  PriceImportIssuesPageDto,
  PriceImportItemsPageDto,
  PriceImportListPageDto,
  PriceImportStatusDto,
  ProjectDto,
  SupplierDto,
  UploadOrderPriceDocumentInput,
} from "@/shared/api/backend-types";
import { apiClient } from "@/shared/api/client";
import { buildOrderPriceDocumentFormData } from "@/shared/api/order-price-document-form";
import { buildPriceImportFormData } from "@/shared/api/price-import-form";

export async function startSmartRuleSearch(
  priceVersionId: string,
  input: { provider: string; model: string },
): Promise<{ batch_id: string; status: string }> {
  const { data } = await apiClient.post<{ batch_id: string; status: string }>(
    `/price-versions/${priceVersionId}/rule-proposals`,
    { ...input, thinking: "disabled", reasoning_effort: "high" },
  );
  return data;
}

export async function fetchProjects(): Promise<ProjectDto[]> {
  const { data } = await apiClient.get<ProjectDto[]>("/projects");
  return data;
}

export async function fetchSuppliers(): Promise<SupplierDto[]> {
  const { data } = await apiClient.get<SupplierDto[]>("/suppliers");
  return data;
}

export async function fetchImportedItems(params: {
  query?: string;
  supplier_id?: string;
  limit: number;
  offset: number;
}): Promise<ImportedItemsPageDto> {
  const { data } = await apiClient.get<ImportedItemsPageDto>("/price-items", {
    params,
  });
  return data;
}

export async function fetchPriceImports(params: {
  limit: number;
  offset: number;
}): Promise<PriceImportListPageDto> {
  const { data } = await apiClient.get<PriceImportListPageDto>(
    "/price-imports",
    { params },
  );
  return data;
}

export async function fetchLlmProfiles(): Promise<LlmProfilesDto> {
  const { data } = await apiClient.get<LlmProfilesDto>("/reference/llm-profiles");
  return data;
}

export async function createPriceImport(
  input: CreatePriceImportInput,
): Promise<PriceImportAcceptedDto> {
  const form = buildPriceImportFormData(input);

  const { data } = await apiClient.post<PriceImportAcceptedDto>(
    "/price-imports",
    form,
    { headers: { "Idempotency-Key": input.idempotencyKey } },
  );
  return data;
}

export async function fetchPriceImport(
  importId: string,
): Promise<PriceImportStatusDto> {
  const { data } = await apiClient.get<PriceImportStatusDto>(
    `/price-imports/${importId}`,
  );
  return data;
}

export async function fetchPriceImportItems(
  importId: string,
  params: { limit: number; offset: number },
): Promise<PriceImportItemsPageDto> {
  const { data } = await apiClient.get<PriceImportItemsPageDto>(
    `/price-imports/${importId}/items`,
    { params },
  );
  return data;
}

export async function fetchPriceImportIssues(
  importId: string,
  params: { limit: number; offset: number },
): Promise<PriceImportIssuesPageDto> {
  const { data } = await apiClient.get<PriceImportIssuesPageDto>(
    `/price-imports/${importId}/issues`,
    { params },
  );
  return data;
}

export async function updatePriceImport(
  importId: string,
  input: { vatRatePercent?: string },
): Promise<PriceImportStatusDto> {
  const form = new FormData();
  if (input.vatRatePercent !== undefined) {
    form.append("vat_rate_percent", input.vatRatePercent);
  }
  const { data } = await apiClient.patch<PriceImportStatusDto>(
    `/price-imports/${importId}`,
    form,
  );
  return data;
}

/**
 * Мостик «заявка → прайс/счёт»: ценовой документ со страницы заявки.
 *
 * `batchId` — идентификатор заявки (в контуре Ф3 он же `check_batch_id`).
 * Рука принимает только PDF/XLSX/DOCX — расширения, которые разбор умеет
 * читать; `.csv`/`.txt` бэкенд пропускает по имени и отвергает содержимым,
 * поэтому интерфейс их и не предлагает (фильтр `accept` в диалоге).
 */
export async function uploadOrderPriceDocument(
  batchId: string,
  input: UploadOrderPriceDocumentInput,
): Promise<OrderPriceDocumentAcceptedDto> {
  const form = buildOrderPriceDocumentFormData(input);

  const { data } = await apiClient.post<OrderPriceDocumentAcceptedDto>(
    `/check-batches/${batchId}/price-documents`,
    form,
    input.idempotencyKey
      ? { headers: { "Idempotency-Key": input.idempotencyKey } }
      : undefined,
  );
  return data;
}
