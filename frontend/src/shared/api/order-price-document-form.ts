import type { UploadOrderPriceDocumentInput } from "@/shared/api/backend-types";

/**
 * Тело multipart-запроса мостика «заявка → прайс/счёт».
 *
 * Отдельным модулем, а не внутри `resources.ts`: сборку тела можно проверить
 * без axios и без браузера (`tests/order-price-document-form.test.mts`), как это
 * уже сделано для сервисной загрузки прайса (`price-import-form.ts`).
 *
 * Имя файла отправляется как есть — его проверяет и нормализует backend
 * (`safe_file_name`): доверять клиентскому пути нельзя, но и решать за
 * пользователя, как назвать документ, незачем.
 */
export function buildOrderPriceDocumentFormData(
  input: UploadOrderPriceDocumentInput,
): FormData {
  const form = new FormData();

  form.append("file", input.file);
  form.append("supplier_external_id", input.supplierExternalId);
  form.append("document_type", input.documentType);

  if (input.supplierName) form.append("supplier_name", input.supplierName);
  if (input.currencyHint) form.append("currency_hint", input.currencyHint);

  return form;
}
