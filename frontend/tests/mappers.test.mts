import assert from "node:assert/strict";
import test from "node:test";

import {
  formatImportPrice,
  formatImportPriceDetails,
  importPriceKey,
  importPriceLabel,
  mapBackendRole,
  mapBackendUserRole,
  mapCatalogItem,
  mapImportIssue,
  mapImportItem,
  mapImportStatus,
  toPriceCheckInput,
  toQuoteInput,
} from "../src/shared/api/mappers.ts";
import type {
  ImportIssueDto,
  ImportItemDto,
  ImportItemPriceDto,
  ImportedItemDto,
} from "../src/shared/api/backend-types.ts";
import type {
  OrderCard,
  OrderCommercials,
} from "../src/shared/model/types.ts";

test("backend roles map to existing frontend visibility roles", () => {
  // Роли бэкенда совпадают с ролями фронтенда один в один (identity-модуль
  // Фазы 3, шаг 02): старого «user» → «manager» больше нет.
  assert.equal(mapBackendRole("admin"), "admin");
  assert.equal(mapBackendRole("manager"), "manager");
  assert.equal(mapBackendRole("viewer"), "viewer");
});

test("sysadmin visibility follows the complete backend roles list", () => {
  assert.equal(mapBackendUserRole("admin", ["sysadmin"]), "sysadmin");
  assert.equal(mapBackendUserRole("admin", ["admin"]), "admin");
});

test("terminal import statuses stop polling and preserve warning states", () => {
  assert.deepEqual(mapImportStatus("completed"), {
    label: "Обработан",
    tone: "success",
    terminal: true,
  });
  assert.equal(mapImportStatus("review_required").terminal, true);
  assert.equal(mapImportStatus("needs_ocr").tone, "warning");
  assert.equal(mapImportStatus("quality_check").terminal, false);
});

test("price mapper keeps range and on-request semantics", () => {
  const base: ImportItemPriceDto = {
    kind: "retail",
    amount: null,
    qualifier: "range",
    amount_from: "100",
    amount_to: "150",
    currency: "RUB",
    currency_symbol: "₽",
    currency_source: "file",
    min_quantity: null,
    max_quantity: null,
    basis_quantity: null,
    basis_unit: null,
    raw_value: "100-150",
    source_column_index: 3,
    vat_mode: "unknown",
    is_on_request: false,
    source_label: null,
  };

  assert.equal(formatImportPrice(base), "100–150 ₽");
  assert.equal(
    formatImportPrice({
      ...base,
      qualifier: "on_request",
      is_on_request: true,
    }),
    "По запросу",
  );
});

test("price presentation uses backend metadata without array indexes", () => {
  const price: ImportItemPriceDto = {
    kind: "wholesale", amount: "100", qualifier: "exact", amount_from: null, amount_to: null,
    currency: "RUB", currency_symbol: "₽", currency_source: "column", min_quantity: "10",
    max_quantity: null, basis_quantity: null, basis_unit: "шт", raw_value: "100",
    source_column_index: 3, vat_mode: "included", is_on_request: false, source_label: "Опт с НДС",
  };
  assert.equal(importPriceLabel(price), "Опт с НДС");
  assert.equal(formatImportPriceDetails(price), "100 ₽ · с НДС · от 10 шт");
  assert.notEqual(importPriceKey(price), importPriceKey({ ...price, min_quantity: "100" }));
});

test("item mapper exposes reviewed category, attributes and source coordinates", () => {
  const item: ImportItemDto = {
    id: "3c452a3e-c4ca-4546-b92d-6d26f1dbeb0e",
    source_name: "Огнетушитель ОП-4",
    normalized_name: "ОГНЕТУШИТЕЛЬ ОП-4",
    article: "OP-4",
    unit: "шт",
    category_path: "Исходная категория",
    category: {
      source_path: "Исходная категория",
      supplier_category_id: null,
      supplier_normalized_path: null,
      global_category_id: "8a29de44-1954-4afd-8c6b-ee45db63a60a",
      global_path: "Огнетушители / Порошковые",
      mapping_status: "manual",
      matching_identity: "ОГНЕТУШИТЕЛИ",
      matching_scope: "global:8a29de44-1954-4afd-8c6b-ee45db63a60a",
    },
    dimensions: null,
    description: null,
    attributes: [
      {
        id: "f04a43fc-dba0-4212-8e8f-89474406880e",
        code: "mass",
        name: "Масса заряда",
        value_kind: "decimal",
        value: "4",
        raw_value: "4",
        unit: "кг",
        parse_status: "parsed",
      },
    ],
    source_sheet: "Прайс",
    source_row: 12,
    source_section_id: null,
    source_panel_index: null,
    source_column_start: null,
    source_column_end: null,
    provenance: null,
    source_evidence: null,
    prices: [],
  };

  assert.deepEqual(mapImportItem(item), {
    id: item.id,
    name: "Огнетушитель ОП-4",
    article: "OP-4",
    category: "Огнетушители / Порошковые",
    mappingStatus: "manual",
    unit: "шт",
    price: "Цена не найдена",
    prices: [],
    attributes: ["Масса заряда: 4 кг"],
    source: "Прайс, строка 12",
  });
});

test("item mapper preserves every parsed price for frontend selection", () => {
  const first: ImportItemPriceDto = {
    kind: "wholesale", amount: "100", qualifier: "exact", amount_from: null, amount_to: null,
    currency: "RUB", currency_symbol: "₽", currency_source: "column", min_quantity: null,
    max_quantity: null, basis_quantity: null, basis_unit: null, raw_value: "100",
    source_column_index: 3, vat_mode: "included", is_on_request: false, source_label: "Опт с НДС",
  };
  const second = { ...first, kind: "retail", amount: "130", source_column_index: 4, source_label: "Розница" };
  const item = {
    id: "item-1", source_name: "Товар", normalized_name: "ТОВАР", article: null, unit: null,
    category_path: null,
    category: { source_path: null, supplier_category_id: null, supplier_normalized_path: null,
      global_category_id: null, global_path: null, mapping_status: "unmapped" as const,
      matching_identity: "", matching_scope: null },
    dimensions: null, description: null, attributes: [], source_sheet: "Лист1", source_row: 2,
    source_section_id: null, source_panel_index: null, source_column_start: null, source_column_end: null,
    provenance: null, source_evidence: null, prices: [first, second],
  } satisfies ImportItemDto;
  const mapped = mapImportItem(item);
  assert.equal(mapped.prices.length, 2);
  assert.equal(mapped.prices[1]?.source_column_index, 4);
  assert.equal(mapped.prices[1]?.source_label, "Розница");
});

test("catalog mapper adds supplier and price-version context", () => {
  const item: ImportedItemDto = {
    id: "3c452a3e-c4ca-4546-b92d-6d26f1dbeb0e",
    source_name: "Огнетушитель ОП-4",
    normalized_name: "ОГНЕТУШИТЕЛЬ ОП-4",
    article: "OP-4",
    unit: "шт",
    category_path: "Огнетушители",
    category: {
      source_path: "Огнетушители",
      supplier_category_id: null,
      supplier_normalized_path: null,
      global_category_id: null,
      global_path: null,
      mapping_status: "unmapped",
      matching_identity: "ОГНЕТУШИТЕЛИ",
      matching_scope: null,
    },
    dimensions: null,
    description: null,
    attributes: [],
    source_sheet: "Прайс",
    source_row: 12,
    source_section_id: null,
    source_panel_index: null,
    source_column_start: null,
    source_column_end: null,
    provenance: null,
    source_evidence: null,
    prices: [],
    supplier_id: "56fc6bc6-ad13-4e9d-befa-46c8a0d20881",
    supplier_name: "ПожТехСнаб",
    price_version_id: "0d993951-bfd4-450c-a7b8-fb5ec6f2e55d",
    version_no: 3,
    version_status: "active",
    filename: "price.xlsx",
    imported_at: "2026-08-16T00:00:00Z",
  };

  assert.deepEqual(mapCatalogItem(item), {
    id: item.id,
    article: "OP-4",
    name: "Огнетушитель ОП-4",
    category: "Огнетушители",
    attributes: [],
    unit: "шт",
    supplier: "ПожТехСнаб",
    filename: "price.xlsx",
    versionNo: 3,
  });
});

test("issue mapper keeps validation code and physical source location", () => {
  const issue: ImportIssueDto = {
    id: "588f964e-469e-4d2a-b097-feaa84ea1d73",
    severity: "warning",
    code: "PRICE_CURRENCY_UNRESOLVED",
    sheet: "Лист1",
    row_no: 8,
    column_ref: "D",
    source_value: "1250",
    message: "Валюта не определена",
  };

  assert.deepEqual(mapImportIssue(issue), {
    id: issue.id,
    severity: "warning",
    code: "PRICE_CURRENCY_UNRESOLVED",
    location: "Лист1, строка 8, колонка D",
    sourceValue: "1250",
    message: "Валюта не определена",
  });
});

test("order detail adapters stay neutral until the calculation sheet loads", () => {
  // Задача №7, шаг 5: вкладки цены/КП читают расчётный лист заявки
  // (`GET /orders/{checkBatchId}`). Пока лист не загружен, адаптер
  // нейтрален — страница не выдумывает данные.
  const order: OrderCard = {
    id: "b1",
    number: "ЗК-101",
    client: "АО Заказчик",
    status: "draft",
    statusLabel: "Черновик",
    progress: 10,
    date: "2026-09-08T00:00:00Z",
    title: "Насосы для котельной",
    supplier: null,
    author: "manager@novakhim.internal",
    version: 1,
    finalVerdict: "—",
    updatedAt: "2026-09-08T00:00:00Z",
    items: [],
  };

  const priceEmpty = toPriceCheckInput(order);
  assert.equal(priceEmpty.id, order.id);
  assert.equal(priceEmpty.calculationVersion, "—");
  assert.deepEqual(priceEmpty.files, []);
  assert.deepEqual(priceEmpty.pricePositions, []);
  assert.equal(priceEmpty.vatRate, 0);

  const commercials: OrderCommercials = {
    id: "pr-1",
    checkBatchId: order.id,
    calculationVersion: "Р-ab12cd34",
    lifecycle: ["Черновик", "В работе", "На подтверждении", "Закрыта"],
    currentLifecycle: 1,
    sent: true,
    readOnly: false,
    files: [
      {
        id: "f1",
        name: "price.xlsx",
        kind: "price",
        version: "3",
        date: "2026-09-07T00:00:00Z",
        progress: 100,
        status: "processed",
      },
    ],
    pricePositions: [
      {
        id: "p1",
        product: "Огнетушитель ОП-4",
        unit: "шт",
        supplier: "ПожТехСнаб",
        price: 1000,
        discount: 5,
        method: "auto",
        status: "success",
        source: "прайс v3",
        selected: true,
      },
    ],
    quotePositions: [
      {
        id: "q1",
        product: "Огнетушитель ОП-4",
        quantity: 2,
        unit: "шт",
        supplier: "ПожТехСнаб",
        price: 1000,
        discount: 5,
        subtotal: 1900,
        vat: 418,
        total: 2318,
      },
    ],
    vatRate: 20,
    automaticDiscount: 5,
    specialDiscount: 3,
    discountApproval: "pending",
    supplierTotals: [
      { supplier: "ПожТехСнаб", amount: 2318, weight: "9 кг", volume: "0,02 м³" },
    ],
    versions: [],
    auditLog: [],
  };

  const price = toPriceCheckInput(order, commercials);
  assert.equal(price.vatRate, 20);
  assert.equal(price.files.length, 1);
  assert.equal(price.pricePositions[0]?.supplier, "ПожТехСнаб");
  // id заявки (check-batch) — мутации контура принимают именно его,
  // а не id расчётного листа
  assert.equal(price.id, order.id);

  const quote = toQuoteInput(order, commercials);
  assert.equal(quote.quotePositions[0]?.total, 2318);
  assert.equal(quote.calculationVersion, "Р-ab12cd34");
  assert.equal(quote.supplierTotals[0]?.supplier, "ПожТехСнаб");
  assert.equal(quote.id, order.id);
});
