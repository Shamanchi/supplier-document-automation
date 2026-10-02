import { expect, test, type Page, type Route } from "@playwright/test";

import { json, stubSession } from "./session";

/**
 * Экран «Заявки» на реальном API: список — проекция `GET /check-batches`,
 * карточка — пакет с позициями, документы позиции приходят из `check-items`
 * и загружаются через `POST /uploads`.
 *
 * Бэкенд подменяется на уровне сети: поведение самих эндпоинтов покрыто
 * backend/tests/test_checks.py и test_uploads.py.
 */

test.use({ serviceWorkers: "block" });

const BATCH = {
  id: "11111111-1111-1111-1111-111111111111",
  tenderId: "B24-12345",
  title: "З-00142",
  customer: "Supplier EСтрой»",
  supplier: "ООО «Поставщик»",
  status: "DRAFT",
  finalVerdict: "PENDING",
  version: 1,
  author: { id: "u-1", displayName: "Менеджер" },
  items: [
    {
      id: "22222222-2222-2222-2222-222222222222",
      positionNo: 1,
      name: "Насос Н1",
      status: "DRAFT",
    },
    {
      id: "33333333-3333-3333-3333-333333333333",
      positionNo: 2,
      name: "Задвижка З1",
      status: "COMPLETED",
    },
  ],
  createdAt: "2026-08-18T10:00:00Z",
  updatedAt: "2026-08-18T11:00:00Z",
};

const DOCUMENTS = {
  requirement: [
    {
      documentId: "d-1",
      documentVersionId: "v-1",
      role: "REQUIREMENT",
      fileName: "specification.pdf",
      mediaType: "application/pdf",
      sizeBytes: 2048,
      sha256: "a".repeat(64),
      status: "UPLOADED",
      createdAt: "2026-08-18T10:30:00Z",
    },
  ],
  fact: [],
};

/**
 * Расчётный лист заявки (задача №7): ответ `GET /orders/{checkBatchId}`.
 * Пустой лист — честное состояние «расчёт ещё не создан», вкладки
 * показывают пустые состояния, а не выдуманные данные.
 */
const ORDER_SHEET_EMPTY = {
  id: "44444444-4444-4444-4444-444444444444",
  number: "ЗК-101",
  client: "Supplier EСтрой»",
  source: "check_batch",
  status: "draft",
  checkBatchId: BATCH.id,
  calculationVersion: "—",
  lifecycle: [],
  currentLifecycle: 0,
  sent: false,
  readOnly: false,
  files: [],
  pricePositions: [],
  quotePositions: [],
  vatRate: 0,
  automaticDiscount: 0,
  specialDiscount: 0,
  discountApproval: "pending",
  supplierTotals: [],
  versions: [],
  auditLog: [],
};

/** Лист с расчётом: прайс загружен, цены подобраны, КП посчитано. */
const ORDER_SHEET = {
  ...ORDER_SHEET_EMPTY,
  status: "active",
  sent: true,
  calculationVersion: "Р-ab12cd34",
  lifecycle: ["Черновик", "В работе", "На подтверждении", "Закрыта"],
  currentLifecycle: 1,
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
      product: "Насос Н1",
      unit: "шт",
      supplier: "ООО «Поставщик»",
      price: 12500,
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
      product: "Насос Н1",
      quantity: 2,
      unit: "шт",
      supplier: "ООО «Поставщик»",
      price: 12500,
      discount: 5,
      subtotal: 23750,
      vat: 5225,
      total: 28975,
    },
  ],
  vatRate: 20,
  automaticDiscount: 5,
  specialDiscount: 3,
  supplierTotals: [
    {
      supplier: "ООО «Поставщик»",
      amount: 28975,
      weight: "12.5 кг",
      volume: "0.08 м³",
    },
  ],
  versions: [],
  auditLog: [],
};

/** Лист свежей заявки: позиции есть, но подбор ещё не сделан. */
const ORDER_SHEET_FRESH = {
  ...ORDER_SHEET_EMPTY,
  pricePositions: [
    {
      // У неподобранной позиции id — это id строки расчётного листа:
      // по нему ищутся предложения и собирается КП.
      id: "l-1",
      product: "Насос Н1",
      unit: "шт",
      supplier: "",
      price: null,
      discount: 0,
      method: "auto",
      status: "missing",
      source: "",
      selected: false,
    },
  ],
};

/** Ответ `GET /orders/{id}/lines/{lineId}/offers` (задача №7, дополнение). */
const LINE_OFFERS = {
  lineId: "l-1",
  query: "Насос Н1",
  offers: [
    {
      canonicalId: "c-1",
      canonicalTitle: "Насос Н1 центробежный",
      priceItemId: "pi-1",
      supplier: "ООО «Поставщик»",
      name: "Насос Н1 (1.5 кВт)",
      unit: "шт",
      price: 12500,
      currency: "RUB",
      stage: "fuzzy",
      score: 0.91,
    },
    {
      canonicalId: "c-2",
      canonicalTitle: "Насос Н2 центробежный",
      priceItemId: "pi-2",
      supplier: "ООО «Другой поставщик»",
      name: "Насос Н2 (2.2 кВт)",
      unit: "шт",
      price: 18200,
      currency: "RUB",
      stage: "fuzzy",
      score: 0.62,
    },
  ],
};

async function openOrders(
  page: Page,
  options: {
    batches?: unknown[];
    commercials?: unknown;
    onCreate?: (route: Route) => unknown;
    onRun?: (route: Route) => unknown;
    onUpload?: (route: Route) => unknown;
  } = {},
) {
  await stubSession(page);
  await page.route("**/api/v1/check-batches?*", (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    return json(route, { items: options.batches ?? [BATCH], nextCursor: null });
  });
  await page.route("**/api/v1/check-batches", (route) => {
    if (route.request().method() === "POST" && options.onCreate) {
      return options.onCreate(route);
    }
    return json(route, { items: options.batches ?? [BATCH], nextCursor: null });
  });
  await page.route(`**/api/v1/check-batches/${BATCH.id}`, (route) =>
    json(route, BATCH),
  );
  await page.route(`**/api/v1/check-batches/${BATCH.id}/run`, (route) =>
    options.onRun
      ? options.onRun(route)
      : json(route, { batchId: BATCH.id, started: [], rejected: [] }, 202),
  );
  await page.route("**/api/v1/check-items/*/documents", (route) =>
    json(route, DOCUMENTS),
  );
  await page.route("**/api/v1/check-items/*/runs", (route) => json(route, []));
  await page.route("**/api/v1/uploads", (route) =>
    options.onUpload
      ? options.onUpload(route)
      : json(route, { uploadId: "v-2", document: {} }, 201),
  );

  // Карточка дополнительно опрашивает вердикты/параметры/сравнения и health
  // битрикса. Без заглушек запросы уходят на живой бэк: он отвечает 401,
  // клиент делает refresh (токен стаба невалиден — повторный 401) и
  // разлогинивает на /login посреди теста.
  await page.route("**/api/v1/**", (route) => {
    const { pathname } = new URL(route.request().url());
    if (
      pathname.endsWith("/auth/refresh") ||
      pathname.endsWith("/auth/me") ||
      pathname.endsWith("/projects") ||
      pathname.endsWith("/check-batches") ||
      pathname.endsWith(`/check-batches/${BATCH.id}`) ||
      pathname.endsWith(`/check-batches/${BATCH.id}/run`) ||
      pathname.endsWith("/documents") ||
      pathname.endsWith("/runs") ||
      pathname.endsWith("/uploads")
    ) {
      return route.fallback();
    }
    if (pathname.endsWith(`/orders/${BATCH.id}`)) {
      // Расчётный лист заявки (задача №7): по умолчанию пуст — расчёт
      // ещё не создан, вкладки показывают пустые состояния.
      return json(route, options.commercials ?? ORDER_SHEET_EMPTY);
    }
    if (pathname.endsWith("/verdict")) {
      // Вердиктов ещё нет — панели заявки/позиции сворачиваются в объяснение.
      return json(route, {
        id: BATCH.id,
        scope: "CHECK_BATCH",
        scopeId: BATCH.id,
        computedVerdict: "PENDING",
        managerVerdict: "PENDING",
        basis: { items: 0 },
        comment: null,
        approvedBy: null,
        approvedAt: null,
        version: 1,
      });
    }
    if (pathname.endsWith("/parameters")) {
      return json(route, { items: [], nextCursor: null });
    }
    if (pathname.endsWith("/comparisons")) {
      return json(route, []);
    }
    if (pathname.endsWith("/integrations/bitrix/health")) {
      return json(route, {
        configured: true,
        mode: "fake",
        status: "OK",
        checkedAt: "2026-08-18T11:00:00Z",
        errorCode: null,
      });
    }
    return json(route, []);
  });
  await page.goto("/orders");
  await expect(
    page.getByRole("heading", { name: "Заявки (тендеры)" }),
  ).toBeVisible();
}

test("список показывает пакеты проверки с прогрессом", async ({ page }) => {
  await openOrders(page);

  const row = page.getByRole("row").filter({ hasText: "З-00142" });
  await expect(row).toContainText("Supplier EСтрой»");
  await expect(row).toContainText("Тендер B24-12345");
  await expect(row).toContainText("Черновик");
  // Одна из двух позиций завершена — прогресс считается на клиенте.
  await expect(row).toContainText("50%");
});

test("точные статусы FSM укрупняются до статусов экрана", async ({ page }) => {
  // Регрессия: список отдаёт QUEUED/REVIEW_REQUIRED/COMPLETED, а экран знал
  // только укрупнённые значения — бейдж оставался без тона, и страница падала
  // с «element type is invalid» вместо списка.
  await openOrders(page, {
    batches: [
      { ...BATCH, id: "a1", title: "З-QUEUED", status: "QUEUED" },
      { ...BATCH, id: "a2", title: "З-REVIEW", status: "REVIEW_REQUIRED" },
      { ...BATCH, id: "a3", title: "З-DONE", status: "COMPLETED" },
      { ...BATCH, id: "a4", title: "З-UNKNOWN", status: "SOMETHING_NEW" },
    ],
  });

  await expect(
    page.getByRole("row").filter({ hasText: "З-QUEUED" }),
  ).toContainText("В работе");
  await expect(
    page.getByRole("row").filter({ hasText: "З-REVIEW" }),
  ).toContainText("На подтверждении");
  await expect(
    page.getByRole("row").filter({ hasText: "З-DONE" }),
  ).toContainText("Завершена");
  // Незнакомое значение показывается кодом как есть и не роняет список.
  await expect(
    page.getByRole("row").filter({ hasText: "З-UNKNOWN" }),
  ).toContainText("SOMETHING_NEW");
});

test("пустой список объясняет, что делать", async ({ page }) => {
  await openOrders(page, { batches: [] });

  await expect(page.getByText("Заявок не найдено", { exact: false })).toBeVisible();
});

test("создание отправляет клиента и позиции", async ({ page }) => {
  let sent: Record<string, unknown> | null = null;
  let idempotencyKey: string | undefined;
  await openOrders(page, {
    onCreate: (route) => {
      sent = route.request().postDataJSON();
      idempotencyKey = route.request().headers()["idempotency-key"];
      return json(route, BATCH, 201);
    },
  });

  await page.getByRole("button", { name: "Создать заявку" }).click();
  await page.getByLabel("Клиент").fill("Supplier EСтрой»");
  await page.getByLabel("Позиция 1").fill("Насос Н1");
  await page.getByRole("button", { name: "Добавить позицию" }).click();
  await page.getByLabel("Позиция 2").fill("Задвижка З1");
  await page.getByRole("button", { name: "Создать" }).click();

  await expect(page.getByText("создана как черновик", { exact: false })).toBeVisible();
  expect(sent).toMatchObject({
    customer: "Supplier EСтрой»",
    items: [{ name: "Насос Н1" }, { name: "Задвижка З1" }],
  });
  // Повтор запроса не должен плодить пакеты.
  expect(idempotencyKey).toBeTruthy();
});

test("карточка показывает позиции и документы", async ({ page }) => {
  await openOrders(page);

  await page.getByRole("button", { name: "Открыть" }).click();

  await expect(page.getByText("Позиция 1. Насос Н1")).toBeVisible();
  await expect(page.getByText("Позиция 2. Задвижка З1")).toBeVisible();
  await expect(page.getByText("specification.pdf").first()).toBeVisible();
  // У факта файлов нет — пустая колонка называет это прямо.
  await expect(page.getByText("Файлов нет").first()).toBeVisible();
});

test("загрузка документа уходит multipart на /uploads", async ({ page }) => {
  const fields: Record<string, string> = {};
  await openOrders(page, {
    onUpload: (route) => {
      const body = route.request().postData() ?? "";
      for (const key of ["checkItemId", "role"]) {
        const match = new RegExp(`name="${key}"\\r?\\n\\r?\\n([^\\r\\n]+)`).exec(
          body,
        );
        if (match) fields[key] = match[1];
      }
      fields.fileName = /filename="([^"]+)"/.exec(body)?.[1] ?? "";
      return json(route, { uploadId: "v-2", document: {} }, 201);
    },
  });

  await page.getByRole("button", { name: "Открыть" }).click();
  const position = page
    .locator(".panel")
    .filter({ hasText: "Позиция 1. Насос Н1" });
  await position
    .getByRole("button", { name: "Загрузить" })
    .first()
    .setInputFiles({
      name: "passport.pdf",
      mimeType: "application/pdf",
      buffer: Buffer.from("%PDF-1.7 test"),
    })
    .catch(async () => {
      // Кнопка открывает скрытый input — файл задаётся ему напрямую.
      await position.locator("input[type=file]").first().setInputFiles({
        name: "passport.pdf",
        mimeType: "application/pdf",
        buffer: Buffer.from("%PDF-1.7 test"),
      });
    });

  await expect(page.getByText("Файл passport.pdf загружен")).toBeVisible();
  expect(fields.checkItemId).toBe(BATCH.items[0].id);
  expect(fields.role).toBe("REQUIREMENT");
  expect(fields.fileName).toBe("passport.pdf");
});

test("отклонённые позиции показываются причиной, а не тишиной", async ({
  page,
}) => {
  await openOrders(page, {
    onRun: (route) =>
      json(
        route,
        {
          batchId: BATCH.id,
          started: [],
          rejected: [
            {
              checkItemId: BATCH.items[0].id,
              code: "MISSING_FACT_DOCUMENT",
              message: "не загружен документ факта (паспорт)",
            },
          ],
        },
        202,
      ),
  });

  await page.getByRole("button", { name: "Открыть" }).click();
  await page
    .getByRole("button", { name: "Запустить проверку заявки" })
    .click();

  await expect(
    page.getByText("не загружен документ факта (паспорт)"),
  ).toBeVisible();
});

test("вкладки контура КП не притворяются рабочими", async ({ page }) => {
  // Лист пуст не потому, что страница не умеет, а потому что расчёта
  // ещё нет — данные правдиво отсутствуют.
  await openOrders(page);
  await page.getByRole("button", { name: "Открыть" }).click();

  await page.getByRole("tab", { name: "Проверка цен" }).click();
  await expect(
    page.getByText("Нет позиций для подбора цен", { exact: false }),
  ).toBeVisible();

  await page.getByRole("tab", { name: "КП" }).click();
  await expect(
    page.getByText("КП появится после создания расчёта", { exact: false }),
  ).toBeVisible();
});

test("вкладки цены и КП наполняются из расчётного листа", async ({ page }) => {
  let quotePatch: Record<string, unknown> | null = null;
  await openOrders(page, { commercials: ORDER_SHEET });
  // Роут теста регистрируется позже заглушек openOrders — в Playwright
  // победит он (матчинг в обратном порядке регистрации).
  await page.route(`**/api/v1/orders/${BATCH.id}/quote`, (route) => {
    quotePatch = route.request().postDataJSON();
    return json(route, ORDER_SHEET);
  });

  await page.getByRole("button", { name: "Открыть" }).click();

  // Вкладка «Проверка цен»: прайс-файл и позиции из листа
  await page.getByRole("tab", { name: "Проверка цен" }).click();
  await expect(page.getByText("Файлы для проверки цен")).toBeVisible();
  await expect(page.getByText("price.xlsx")).toBeVisible();
  // Карточка раскрыта внутри строки списка — скоупимся на панель вкладки,
  // иначе ловится и строка-предок из таблицы заявок.
  const pricePanel = page
    .getByRole("tabpanel")
    .filter({ hasText: "Таблица цен" });
  const priceRow = pricePanel.getByRole("row").filter({ hasText: "Насос Н1" });
  await expect(priceRow).toContainText("ООО «Поставщик»");
  await expect(priceRow).toContainText("прайс v3");

  // Вкладка «КП»: версия расчёта, позиции и итоги из листа
  await page.getByRole("tab", { name: "КП" }).click();
  await expect(page.getByText("КОММЕРЧЕСКОЕ ПРЕДЛОЖЕНИЕ")).toBeVisible();
  await expect(page.getByText("Р-ab12cd34")).toBeVisible();
  await expect(page.getByText("ИТОГО к оплате")).toBeVisible();
  const quotePanel = page
    .getByRole("tabpanel")
    .filter({ hasText: "ИТОГО к оплате" });
  await expect(
    quotePanel.getByRole("row").filter({ hasText: "Насос Н1" }),
  ).toContainText("23 750 ₽");
  // Сводка по поставщикам из supplierTotals листа
  await expect(page.getByText("Сводка по поставщикам")).toBeVisible();
  await expect(
    page.locator(".ss-card").filter({ hasText: "ООО «Поставщик»" }),
  ).toBeVisible();

  // Правка НДС уходит PATCH на контурный эндпоинт с причиной
  await page.getByRole("button", { name: "Изменить ставку НДС" }).click();
  await page.locator("#reason-value").fill("22");
  await page.locator("#reason-text").fill("согласовано с клиентом");
  await page.getByRole("button", { name: "Сохранить" }).click();

  await expect(page.getByText("Ставка НДС изменена")).toBeVisible();
  expect(quotePatch).toEqual({
    vatRate: 22,
    reason: "согласовано с клиентом",
  });
});

test("менеджер выбирает позиции и собирает КП из экрана", async ({ page }) => {
  // Задача №7, дополнение: свежая заявка раньше упиралась в пустое
  // состояние — расчёт нельзя было создать из UI. Теперь пикер по строке
  // даёт предложения прайсов, а «Собрать КП» зовёт POST /orders/{id}/quote.
  let assembled: Record<string, unknown> | null = null;
  await openOrders(page, { commercials: ORDER_SHEET_FRESH });
  await page.route(`**/api/v1/orders/${BATCH.id}/lines/*/offers`, (route) =>
    json(route, LINE_OFFERS),
  );
  await page.route(`**/api/v1/orders/${BATCH.id}/quote`, (route) => {
    assembled = route.request().postDataJSON();
    return json(route, {
      id: "55555555-5555-5555-5555-555555555555",
      total_amount: 25000,
      skipped: [],
    });
  });

  await page.getByRole("button", { name: "Открыть" }).click();
  await page.getByRole("tab", { name: "Проверка цен" }).click();

  const pricePanel = page
    .getByRole("tabpanel")
    .filter({ hasText: "Таблица цен" });
  await expect(pricePanel).toContainText("Требует проверки");

  // Пикер строки: предложения по имени позиции, выбираем лучшее
  await pricePanel.getByRole("button", { name: "Выбрать позицию" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Насос Н1 (1.5 кВт)");
  await expect(dialog).toContainText("Насос Н2 (2.2 кВт)");
  await dialog.locator("#offer-quantity").fill("2");
  await dialog
    .getByRole("row")
    .filter({ hasText: "Насос Н1 (1.5 кВт)" })
    .click();
  await dialog.getByRole("button", { name: "Выбрать", exact: true }).click();

  const lineRow = pricePanel.getByRole("row").filter({ hasText: "Насос Н1" });
  await expect(lineRow).toContainText("Выбрано ×2");
  await expect(lineRow).toContainText("ООО «Поставщик»");

  await pricePanel.getByRole("button", { name: /Собрать КП/ }).click();

  await expect(
    page.getByText("КП собрано", { exact: false }),
  ).toBeVisible();
  // Контракт бэка — snake_case selections: маппинг делает хук.
  expect(assembled).toEqual({
    selections: [
      {
        line_id: "l-1",
        canonical_id: "c-1",
        price_item_id: "pi-1",
        quantity: 2,
      },
    ],
  });
});
