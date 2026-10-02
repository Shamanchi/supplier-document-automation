import assert from "node:assert/strict";
import test from "node:test";

import { buildOrderPriceDocumentFormData } from "../src/shared/api/order-price-document-form.ts";

test("счёт заявки уходит с назначением и кодом поставщика", () => {
  const form = buildOrderPriceDocumentFormData({
    file: new File(["invoice"], "счёт-таурус.pdf"),
    supplierExternalId: "tau-rus",
    supplierName: "Таурус",
    documentType: "SUPPLIER_QUOTE",
    currencyHint: "RUB",
  });

  assert.equal(form.get("file") instanceof File, true);
  assert.equal(form.get("supplier_external_id"), "tau-rus");
  assert.equal(form.get("supplier_name"), "Таурус");
  assert.equal(form.get("document_type"), "SUPPLIER_QUOTE");
  assert.equal(form.get("currency_hint"), "RUB");
});

test("необязательные поля не отправляются пустыми строками", () => {
  const form = buildOrderPriceDocumentFormData({
    file: new File(["price"], "price.xlsx"),
    supplierExternalId: "supplier-42",
    documentType: "PRICE_LIST",
  });

  // Пустая строка в multipart — это «поле пришло пустым»: backend отличит её
  // от отсутствия поля и подставит пустое имя поставщика вместо кода.
  assert.equal(form.get("supplier_name"), null);
  assert.equal(form.get("currency_hint"), null);
  assert.equal(form.get("document_type"), "PRICE_LIST");
});
