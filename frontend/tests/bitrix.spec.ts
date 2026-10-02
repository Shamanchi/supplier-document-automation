import { expect, test, type Page, type Route } from "@playwright/test";

import { json, stubSession } from "./session";

/**
 * Импорт файлов сделки и публикация отчёта на карточке заявки.
 *
 * Поведение эндпоинтов покрыто backend/tests/test_integrations_bitrix.py;
 * здесь проверяется, что экран не предлагает недоступное, отправляет выбор
 * менеджера в том виде, в каком его описывает docs/api/integrations/bitrix, и
 * показывает состояние задания вместо молчания.
 */

test.use({ serviceWorkers: "block" });

const BATCH_ID = "11111111-1111-1111-1111-111111111111";
const ITEM_ONE = "22222222-2222-2222-2222-222222222222";
const ITEM_TWO = "33333333-3333-3333-3333-333333333333";
const ARTIFACT_ID = "44444444-4444-4444-4444-444444444444";
const IMPORT_JOB = "55555555-5555-5555-5555-555555555555";
const PUBLISH_JOB = "66666666-6666-6666-6666-666666666666";
const TENDER_ID = "B24-12345";

const BATCH = {
  id: BATCH_ID,
  tenderId: TENDER_ID,
  title: "З-00142",
  customer: "Supplier EСтрой»",
  supplier: "ООО «Поставщик»",
  status: "RUNNING",
  finalVerdict: "COMPLIANT",
  version: 1,
  author: { id: "u-1", displayName: "Менеджер" },
  items: [
    { id: ITEM_ONE, positionNo: 1, name: "Насос Н1", status: "VERDICT_APPROVED" },
    { id: ITEM_TWO, positionNo: 2, name: "Насос Н2", status: "VERDICT_APPROVED" },
  ],
  createdAt: "2026-08-30T10:00:00Z",
  updatedAt: "2026-08-30T11:00:00Z",
};

const VERDICT = {
  id: BATCH_ID,
  scope: "CHECK_BATCH",
  scopeId: BATCH_ID,
  computedVerdict: "COMPLIANT",
  managerVerdict: "COMPLIANT",
  basis: { items: 2 },
  comment: null,
  approvedBy: "u-1",
  approvedAt: "2026-08-30T11:30:00Z",
  version: 4,
};

const REPORT = {
  id: "77777777-7777-7777-7777-777777777777",
  snapshotId: "77777777-7777-7777-7777-777777777777",
  status: "COMPLETED",
  checksum: "a".repeat(64),
  artifacts: [
    {
      id: ARTIFACT_ID,
      format: "XLSX",
      fileName: "batch-0a1b2c3d4e5f.xlsx",
      sizeBytes: 20480,
      sha256: "b".repeat(64),
      createdAt: "2026-08-30T12:00:00Z",
    },
  ],
};

const DEAL = {
  tenderId: TENDER_ID,
  bitrixDealId: "9876",
  title: "Поставка насосного оборудования",
  customer: "АО Заказчик",
  stage: "NEW",
  assignee: "Иван Петров",
  updatedAt: "2026-08-30T09:45:00Z",
  externalUrl: "https://portal.bitrix24.ru/crm/deal/details/9876/",
};

const FILES = [
    {
      externalFileId: "disk-file-tz",
      name: "Техническое задание.pdf",
      sizeBytes: 34291,
      mediaType: "application/pdf",
      source: "DEAL_FIELD",
      createdAt: "2026-08-29T12:00:00Z",
      suggestedKind: "REQUIREMENT",
      kindConfidence: "95.00",
      alreadyImported: false,
      suggestedRole: "REQUIREMENT",
      roleConfidence: "95.00",
      alreadyImportedDocumentId: null,
      alreadyImportedPriceFileId: null,
    },
    {
      externalFileId: "disk-file-passport",
      name: "Паспорт насоса.pdf",
      sizeBytes: 51200,
      mediaType: "application/pdf",
      source: "DISK",
      createdAt: "2026-08-29T12:05:00Z",
      suggestedKind: "FACT",
      kindConfidence: "90.00",
      alreadyImported: false,
      suggestedRole: "FACT",
      roleConfidence: "90.00",
      alreadyImportedDocumentId: null,
      alreadyImportedPriceFileId: null,
    },
    {
      externalFileId: "disk-file-price",
      name: "КП поставщика.pdf",
      sizeBytes: 65536,
      mediaType: "application/pdf",
      source: "DISK",
      createdAt: "2026-08-29T12:10:00Z",
      suggestedKind: "PRICE",
      kindConfidence: "85.00",
      alreadyImported: false,
      suggestedRole: null,
      roleConfidence: null,
      alreadyImportedDocumentId: null,
      alreadyImportedPriceFileId: null,
    },
    {
      externalFileId: "disk-file-old",
      name: "Опросный лист.xlsx",
      sizeBytes: 8192,
      mediaType:
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      source: "DEAL_FIELD",
      createdAt: "2026-08-20T10:00:00Z",
      suggestedKind: "REQUIREMENT",
      kindConfidence: "75.00",
      alreadyImported: true,
      suggestedRole: "REQUIREMENT",
      roleConfidence: "75.00",
      alreadyImportedDocumentId: "88888888-8888-8888-8888-888888888888",
      alreadyImportedPriceFileId: null,
    },
];

const SUPPLIERS = [
  { id: "a-1", name: "ООО «Поставщик А»" },
  { id: "a-2", name: "ООО «Поставщик Б»" },
];

const KIT_STATUS = {
  checkBatchId: BATCH_ID,
  status: "PRICE_READY",
  dealLinked: true,
  dealId: "9876",
  tenderId: TENDER_ID,
  filesAvailable: true,
  filesCount: 4,
  documentsImported: 2,
  priceDocuments: 1,
  priceReady: 1,
  priceRunning: 0,
  priceAttention: 0,
};

function job(overrides: Record<string, unknown> = {}) {
  return {
    id: IMPORT_JOB,
    type: "IMPORT_FILE",
    status: "COMPLETED",
    attempt: 1,
    nextRetryAt: null,
    result: { documents: { "disk-file-tz": ITEM_ONE } },
    error: null,
    ...overrides,
  };
}

interface Options {
  health?: Record<string, unknown> | null;
  batch?: Record<string, unknown>;
  files?: unknown;
  notes?: string[];
  onPrepare?: (route: Route) => unknown;
  onImport?: (route: Route) => unknown;
  onPublish?: (route: Route) => unknown;
  importJob?: Record<string, unknown>;
  publishJob?: Record<string, unknown>;
}

async function openBatch(page: Page, options: Options = {}) {
  const batch = { ...BATCH, ...(options.batch ?? {}) };
  const health = options.health ?? {
    configured: true,
    mode: "webhook",
    status: "OK",
    checkedAt: "2026-09-01T09:00:00Z",
    errorCode: null,
  };

  await stubSession(page);
  await page.route("**/api/v1/**", (route) => {
    const request = route.request();
    const { pathname } = new URL(request.url());
    const post = request.method() === "POST";

    // /projects — загрузка проектов фазы 2 (стаб в stubSession, session.ts).
    if (
      pathname.endsWith("/auth/refresh") ||
      pathname.endsWith("/auth/me") ||
      pathname.endsWith("/projects")
    ) {
      return route.fallback();
    }

    // --- интеграция ---
    if (pathname.endsWith("/integrations/bitrix/health")) {
      return json(route, health);
    }
    if (pathname.endsWith(`/integrations/bitrix/deals/${TENDER_ID}/prepare`) && post) {
      return (
        options.onPrepare?.(route) ??
        json(
          route,
          {
            checkBatchId: BATCH_ID,
            tenderRefId: "99999999-9999-9999-9999-999999999999",
            created: false,
            status: "FILES_AVAILABLE",
            deal: DEAL,
            files: options.files ?? FILES,
            notes: options.notes ?? [],
          },
          201,
        )
      );
    }
    if (pathname.endsWith(`/check-batches/${BATCH_ID}/status`)) {
      return json(route, KIT_STATUS);
    }
    if (pathname.endsWith("/suppliers")) {
      return json(route, SUPPLIERS);
    }
    if (pathname.endsWith(`/deals/${TENDER_ID}/imports`) && post) {
      return (
        options.onImport?.(route) ??
        json(route, { jobId: IMPORT_JOB, status: "QUEUED", filesTotal: 1 }, 202)
      );
    }
    if (pathname.endsWith(`/integrations/bitrix/imports/${IMPORT_JOB}`)) {
      return json(route, job(options.importJob));
    }
    if (pathname.endsWith("/publish") && post) {
      return (
        options.onPublish?.(route) ??
        json(
          route,
          { jobId: PUBLISH_JOB, status: "QUEUED", tenderId: TENDER_ID },
          202,
        )
      );
    }
    if (pathname.endsWith(`/publications/${PUBLISH_JOB}`)) {
      return json(
        route,
        job({ id: PUBLISH_JOB, type: "PUBLISH_REPORT", ...options.publishJob }),
      );
    }

    // --- заявка ---
    if (pathname === "/api/v1/check-batches") {
      return json(route, { items: [batch], nextCursor: null });
    }
    if (pathname === `/api/v1/check-batches/${BATCH_ID}`) {
      return json(route, batch);
    }
    if (pathname.endsWith("/reports")) {
      return json(route, REPORT, 201);
    }
    if (pathname.endsWith("/verdict")) {
      return json(route, VERDICT);
    }
    if (pathname.endsWith("/documents")) {
      return json(route, { requirement: [], fact: [] });
    }
    if (pathname.endsWith("/parameters")) {
      return json(route, { items: [], nextCursor: null });
    }
    return json(route, []);
  });

  await page.goto("/orders");
  await page.getByRole("button", { name: "Открыть" }).click();
  await expect(page.getByText("Заявка №")).toBeVisible();
}

test("выключенная интеграция не показывает действий B24", async ({ page }) => {
  await openBatch(page, {
    health: {
      configured: false,
      mode: "disabled",
      status: "DISABLED",
      checkedAt: "2026-09-01T09:00:00Z",
      errorCode: null,
    },
  });

  await expect(
    page.getByRole("button", { name: "Загрузить из B24" }),
  ).toHaveCount(0);
});

test("заявка без сделки не предлагает импорт", async ({ page }) => {
  const bitrixRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/integrations/bitrix/")) {
      bitrixRequests.push(new URL(request.url()).pathname);
    }
  });
  await openBatch(page, { batch: { tenderId: null } });

  await expect(
    page.getByRole("button", { name: "Загрузить из B24" }),
  ).toHaveCount(0);
  expect(bitrixRequests).toEqual([]);
});

test("диалог показывает сделку, файлы и предложенные роли", async ({ page }) => {
  await openBatch(page);
  await page.getByRole("button", { name: "Загрузить из B24" }).click();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Поставка насосного оборудования");
  await expect(dialog).toContainText("АО Заказчик");
  await expect(
    dialog.getByRole("link", { name: "Открыть сделку в Bitrix24" }),
  ).toHaveAttribute("href", DEAL.externalUrl);
  await expect(dialog).toContainText("Техническое задание.pdf");
  await expect(dialog).toContainText("Поле сделки");
});

test("подготовка отправляет текущую заявку и позиции", async ({ page }) => {
  let sent: Record<string, unknown> | null = null;
  await openBatch(page, {
    onPrepare: (route) => {
      sent = route.request().postDataJSON();
      return json(
        route,
        {
          checkBatchId: BATCH_ID,
          tenderRefId: "99999999-9999-9999-9999-999999999999",
          created: false,
          status: "FILES_AVAILABLE",
          deal: DEAL,
          files: FILES,
          notes: [],
        },
        201,
      );
    },
  });

  await page.getByRole("button", { name: "Загрузить из B24" }).click();

  await expect
    .poll(() => sent)
    .toEqual({
      checkBatchId: BATCH_ID,
      items: [
        { positionNo: 1, name: "Насос Н1", supplier: "ООО «Поставщик»" },
        { positionNo: 2, name: "Насос Н2", supplier: "ООО «Поставщик»" },
      ],
    });
});

test("повторное открытие диалога повторно подготавливает тот же batch", async ({ page }) => {
  let prepareCalls = 0;
  await openBatch(page, {
    onPrepare: (route) => {
      prepareCalls += 1;
      return json(
        route,
        {
          checkBatchId: BATCH_ID,
          tenderRefId: "99999999-9999-9999-9999-999999999999",
          created: false,
          status: "FILES_AVAILABLE",
          deal: DEAL,
          files: FILES,
          notes: [],
        },
        201,
      );
    },
  });

  const open = page.getByRole("button", { name: "Загрузить из B24" });
  await open.click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("button", { name: "Закрыть" }).first().click();
  await open.click();
  await expect(page.getByRole("dialog")).toContainText("Техническое задание.pdf");

  await expect.poll(() => prepareCalls).toBe(2);
});

test("уже импортированный файл выбрать нельзя", async ({ page }) => {
  await openBatch(page);
  await page.getByRole("button", { name: "Загрузить из B24" }).click();

  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("уже импортирован");
  await expect(
    dialog.getByRole("checkbox", { name: "Выбрать Опросный лист.xlsx" }),
  ).toBeDisabled();
});

test("импорт отправляет выбранные файлы с ролью и позицией", async ({ page }) => {
  let sent: Record<string, unknown> | null = null;
  await openBatch(page, {
    onImport: (route) => {
      sent = route.request().postDataJSON();
      return json(
        route,
        { jobId: IMPORT_JOB, status: "QUEUED", filesTotal: 1 },
        202,
      );
    },
  });

  await page.getByRole("button", { name: "Загрузить из B24" }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("checkbox", { name: "Выбрать Техническое задание.pdf" })
    .click();
  await dialog.getByRole("button", { name: /Импортировать/ }).click();

  await expect
    .poll(() => sent)
    .toEqual({
      checkBatchId: BATCH_ID,
      files: [
        {
          externalFileId: "disk-file-tz",
          kind: "REQUIREMENT",
          checkItemId: ITEM_ONE,
          role: "REQUIREMENT",
        },
      ],
    });
});

test("карточка показывает статус комплекта заявки", async ({ page }) => {
  await openBatch(page);

  await expect(page.getByText("Прайс готов")).toBeVisible();
});

test("ценовой документ без поставщика импортировать нельзя", async ({ page }) => {
  await openBatch(page);
  await page.getByRole("button", { name: "Загрузить из B24" }).click();

  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("checkbox", { name: "Выбрать КП поставщика.pdf" })
    .click();

  await expect(
    dialog.getByRole("button", { name: /Импортировать/ }),
  ).toBeDisabled();
  await expect(
    dialog.getByText("Для ценового документа выберите поставщика"),
  ).toBeVisible();
});

test("разовый счёт уходит с назначением и поставщиком", async ({ page }) => {
  let sent: Record<string, unknown> | null = null;
  await openBatch(page, {
    onImport: (route) => {
      sent = route.request().postDataJSON();
      return json(
        route,
        { jobId: IMPORT_JOB, status: "QUEUED", filesTotal: 1 },
        202,
      );
    },
  });

  await page.getByRole("button", { name: "Загрузить из B24" }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("checkbox", { name: "Выбрать КП поставщика.pdf" })
    .click();
  await dialog
    .getByRole("combobox", { name: "Назначение файла КП поставщика.pdf" })
    .click();
  await page.getByRole("option", { name: "Разовый счёт/КП заявки" }).click();
  await dialog
    .getByRole("combobox", { name: "Поставщик для КП поставщика.pdf" })
    .click();
  await page.getByRole("option", { name: "ООО «Поставщик Б»" }).click();
  await dialog.getByRole("button", { name: /Импортировать/ }).click();

  await expect
    .poll(() => sent)
    .toEqual({
      checkBatchId: BATCH_ID,
      files: [
        {
          externalFileId: "disk-file-price",
          kind: "PRICE",
          priceDocumentType: "SUPPLIER_QUOTE",
          supplierId: "a-2",
        },
      ],
    });
});

test("завершённый импорт сообщает об успехе", async ({ page }) => {
  await openBatch(page);
  await page.getByRole("button", { name: "Загрузить из B24" }).click();

  const dialog = page.getByRole("dialog");
  await dialog
    .getByRole("checkbox", { name: "Выбрать Техническое задание.pdf" })
    .click();
  await dialog.getByRole("button", { name: /Импортировать/ }).click();

  await expect(page.getByText("Файлы импортированы из сделки")).toBeVisible();
});

test("пустая сделка объясняет, что импортировать нечего", async ({ page }) => {
  await openBatch(page, { files: [] });
  await page.getByRole("button", { name: "Загрузить из B24" }).click();

  await expect(
    page.getByText("В сделке нет доступных файлов", { exact: false }),
  ).toBeVisible();
});

test("лента сделки показывает, каких вложений не хватает", async ({ page }) => {
  await openBatch(page, {
    notes: [
      "Вложения письма: 1 файл(ов). Не приложены (превышен размер): Каталог.pdf",
    ],
  });
  await page.getByRole("button", { name: "Загрузить из B24" }).click();

  await expect(page.getByText("Каталог.pdf")).toBeVisible();
});

test("отказ портала показывается менеджеру, а не глотается", async ({ page }) => {
  await openBatch(page, {
    onPrepare: (route) =>
      json(
        route,
        {
          error: {
            code: "BITRIX_CONTRACT_ERROR",
            message: "Источник файлов сделки не настроен",
          },
        },
        502,
      ),
  });
  await page.getByRole("button", { name: "Загрузить из B24" }).click();

  await expect(
    page.getByText("Источник файлов сделки не настроен"),
  ).toBeVisible();
});

test("публикация отправляет артефакты собранного отчёта", async ({ page }) => {
  let sent: Record<string, unknown> | null = null;
  await openBatch(page, {
    onPublish: (route) => {
      sent = route.request().postDataJSON();
      return json(
        route,
        { jobId: PUBLISH_JOB, status: "QUEUED", tenderId: TENDER_ID },
        202,
      );
    },
  });

  await page.getByRole("button", { name: "Сводный отчёт" }).click();
  await page.getByRole("button", { name: "Отправить в B24" }).click();

  await expect
    .poll(() => sent)
    .toEqual({
      reportArtifactIds: [ARTIFACT_ID],
      addTimelineComment: true,
      includeKeyMismatches: true,
    });
  await expect(page.getByText("Отчёт опубликован в сделке")).toBeVisible();
});

test("до сборки отчёта публиковать нечего", async ({ page }) => {
  await openBatch(page);

  await expect(page.getByRole("button", { name: "Отправить в B24" })).toHaveCount(
    0,
  );
});

test("повторяемая ошибка публикации предлагает повтор", async ({ page }) => {
  await openBatch(page, {
    publishJob: {
      status: "FAILED",
      result: null,
      error: {
        code: "BITRIX_RATE_LIMITED",
        message: "Портал ограничил частоту запросов",
        retryable: true,
      },
    },
  });

  await page.getByRole("button", { name: "Сводный отчёт" }).click();
  await page.getByRole("button", { name: "Отправить в B24" }).click();

  await expect(page.getByText("Портал ограничил частоту запросов")).toBeVisible();
  await expect(page.getByRole("button", { name: "Повторить" })).toBeVisible();
});

test("неустранимая ошибка публикации повтор не предлагает", async ({ page }) => {
  await openBatch(page, {
    publishJob: {
      status: "FAILED",
      result: null,
      error: {
        code: "BITRIX_FAILED_AUTH",
        message: "Портал отклонил учётные данные",
        retryable: false,
      },
    },
  });

  await page.getByRole("button", { name: "Сводный отчёт" }).click();
  await page.getByRole("button", { name: "Отправить в B24" }).click();

  await expect(page.getByText("Портал отклонил учётные данные")).toBeVisible();
  await expect(page.getByRole("button", { name: "Повторить" })).toHaveCount(0);
});
