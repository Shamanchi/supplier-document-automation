import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { locallyReportedMutation } from "@/shared/api/query-client";

import {
  TERMINAL_IMPORT_STATUSES,
  type CreatePriceImportInput,
} from "@/shared/api/backend-types";
import {
  createPriceImport,
  fetchImportedItems,
  fetchLlmProfiles,
  fetchPriceImport,
  fetchPriceImportIssues,
  fetchPriceImportItems,
  fetchPriceImports,
  fetchSuppliers,
  updatePriceImport,
} from "@/shared/api/resources";

const resourceKeys = {
  suppliers: ["live", "suppliers"] as const,
  importedItems: (
    projectId: string,
    query: string,
    supplierId: string,
    offset: number,
    limit: number,
  ) =>
    [
      "live",
      "price-items",
      projectId,
      query,
      supplierId,
      offset,
      limit,
    ] as const,
  imports: (projectId: string) => ["live", "price-imports", projectId] as const,
  llmProfiles: ["live", "reference", "llm-profiles"] as const,
  import: (projectId: string, importId: string) =>
    ["live", "price-import", projectId, importId] as const,
  items: (projectId: string, importId: string, offset: number) =>
    ["live", "price-import-items", projectId, importId, offset] as const,
  issues: (projectId: string, importId: string, offset: number) =>
    ["live", "price-import-issues", projectId, importId, offset] as const,
};

export function useBackendSuppliers() {
  return useQuery({
    queryKey: resourceKeys.suppliers,
    queryFn: fetchSuppliers,
    staleTime: 5 * 60_000,
  });
}

export function useImportedItems(
  projectId: string,
  filters: {
    query: string;
    supplierId: string;
    offset: number;
    limit: number;
  },
) {
  return useQuery({
    queryKey: resourceKeys.importedItems(
      projectId,
      filters.query,
      filters.supplierId,
      filters.offset,
      filters.limit,
    ),
    queryFn: () =>
      fetchImportedItems({
        query: filters.query || undefined,
        supplier_id: filters.supplierId || undefined,
        limit: filters.limit,
        offset: filters.offset,
      }),
  });
}

export function useLlmProfiles() {
  return useQuery({
    queryKey: resourceKeys.llmProfiles,
    queryFn: fetchLlmProfiles,
    staleTime: 5 * 60_000,
    refetchOnMount: "always",
    refetchOnWindowFocus: "always",
  });
}

export function usePriceImports(projectId: string) {
  return useQuery({
    queryKey: resourceKeys.imports(projectId),
    queryFn: () => fetchPriceImports({ limit: 50, offset: 0 }),
    refetchInterval: (query) =>
      query.state.data?.items.some(
        (item) => !TERMINAL_IMPORT_STATUSES.has(item.status),
      )
        ? 2_000
        : false,
  });
}

export function useCreatePriceImport(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: (input: CreatePriceImportInput) => createPriceImport(input),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: resourceKeys.imports(projectId),
      }),
  });
}

export function usePriceImport(projectId: string, importId: string) {
  return useQuery({
    queryKey: resourceKeys.import(projectId, importId),
    queryFn: () => fetchPriceImport(importId),
    enabled: Boolean(importId),
    refetchInterval: (query) =>
      query.state.data &&
      !TERMINAL_IMPORT_STATUSES.has(query.state.data.status)
        ? 2_000
        : false,
  });
}

export function usePriceImportItems(
  projectId: string,
  importId: string,
  offset: number,
  limit: number,
  enabled: boolean,
) {
  return useQuery({
    queryKey: resourceKeys.items(projectId, importId, offset),
    queryFn: () => fetchPriceImportItems(importId, { limit, offset }),
    enabled: Boolean(importId) && enabled,
  });
}

export function usePriceImportIssues(
  projectId: string,
  importId: string,
  offset: number,
  limit: number,
  enabled: boolean,
) {
  return useQuery({
    queryKey: resourceKeys.issues(projectId, importId, offset),
    queryFn: () => fetchPriceImportIssues(importId, { limit, offset }),
    enabled: Boolean(importId) && enabled,
  });
}

export function useUpdatePriceImport(projectId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    meta: locallyReportedMutation,
    mutationFn: ({
      importId,
      input,
    }: {
      importId: string;
      input: { vatRatePercent?: string };
    }) => updatePriceImport(importId, input),
    onSuccess: (_, variables) => {
      queryClient.invalidateQueries({
        queryKey: resourceKeys.import(projectId, variables.importId),
      });
      queryClient.invalidateQueries({
        queryKey: resourceKeys.imports(projectId),
      });
    },
  });
}
