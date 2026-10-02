import type { CreatePriceImportInput } from "@/shared/api/backend-types";

export function buildPriceImportFormData(input: CreatePriceImportInput): FormData {
  const form = new FormData();

  form.append("file", input.file);
  form.append("supplier_external_id", input.supplierExternalId);

  if (input.supplierName) form.append("supplier_name", input.supplierName);
  if (input.currencyHint) form.append("currency_hint", input.currencyHint);
  if (input.vatRatePercent) form.append("vat_rate_percent", input.vatRatePercent);
  if (input.provider) form.append("provider", input.provider);
  if (input.model) form.append("model", input.model);

  return form;
}
