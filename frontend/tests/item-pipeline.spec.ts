import { expect, test, type Page, type Route } from "@playwright/test";

import { json, stubSession } from "./session";

/**
 * Контур позиции на странице «Заявки»: параметры с провенансом, ручная правка,
 * сопоставление и вердикт.
 *
 * Бэкенд подменяется на уровне сети: поведение самих эндпоинтов покрыто
 * backend/tests/test_parameters.py и test_matching_pipeline.py. Здесь
 * проверяется, что экран показывает и отправляет ровно то, что описано в
 * docs/api/parameters и docs/api/comparisons.
 */

test.use({ serviceWorkers: "block" });

const ITEM_ID = "22222222-2222-2222-2222-222222222222";
const BATCH_ID = "11111111-1111-1111-1111-111111111111";
const PARAMETER_ID = "44444444-4444-4444-4444-444444444444";
const COMPARISON_ID = "55555555-5555-5555-5555-555555555555";

/** Машинная цитата, которую правка оставляет в истории. */
const MACHINE_EVIDENCE = {
  fragmentId: "f-machine",
  quote: "Мощность 12,5 кВт",
  quoteStart: 0,
  quoteEnd: 17,
  valueStart: 9,
  valueEnd: 13,
  verified: true,
  locator: { kind: "DOCX", table: 1 },
  current: true,
};

const FACT_PARAMETER = {
  id: PARAMETER_ID,
  role: "FACT",
  raw: { name: "Мощность", valueText: "12,5", unitText: "кВт" },
  normalized: {
    id: "n-1",
    canonicalParameterId: "c-1",
    canonicalName: "Мощность",
    valueKind: "NUMBER",
    typedValue: { kind: "NUMBER", value: "12.5", canonicalUnit: "KW" },
    normalizedOk: true,
    version: 1,
  },
  extractionMethod: "TABLE",
  confidence: "0.95",
  issues: [],
  evidence: [MACHINE_EVIDENCE],
  manuallyEdited: false,
  excluded: false,
  version: 1,
  createdAt: "2026-08-29T10:00:00Z",
};

const REQUIREMENT_PARAMETER = {
  ...FACT_PARAMETER,
  id: "66666666-6666-6666-6666-666666666666",
  role: "REQUIREMENT",
  raw: { name: "Мощность", valueText: ">= 10", unitText: "кВт" },
  evidence: [{ ...MACHINE_EVIDENCE, fragmentId: "f-requirement" }],
};

const COMPARISON = {
  id: COMPARISON_ID,
  canonicalParameter: { id: "c-1", code: "POWER", name: "Мощность" },
  requirementParameterId: REQUIREMENT_PARAMETER.id,
  factParameterId: PARAMETER_ID,
  rowKind: "PAIRED",
  operator: "GREATER_OR_EQUAL",
  computedStatus: "COMPLIANT",
  effectiveStatus: "COMPLIANT",
  explanation: {
    required: { value: "10", unit: "KW" },
    fact: { value: "12.5", unit: "KW" },
    expression: "12.5 >= 10",
    result: true,
    ruleVersion: "rules-1.0.0",
  },
  lineageChain: null,
  overridden: false,
  stale: false,
  version: 1,
};

const VERDICT = {
  id: "v-1",
  scope: "CHECK_ITEM",
  scopeId: ITEM_ID,
  computedVerdict: "COMPLIANT",
  managerVerdict: "PENDING",
  basis: { COMPLIANT: 1 },
  comment: null,
  approvedBy: null,
  approvedAt: null,
  version: 1,
};

interface Options {
  status?: string;
  parameters?: unknown[];
  comparisons?: unknown[];
  onAdd?: (route: Route) => unknown;
  onRevise?: (route: Route) => unknown;
  onConfirm?: (route: Route) => unknown;
  onOverride?: (route: Route) => unknown;
  onVerdict?: (route: Route) => unknown;
}

function batch(status: string) {
  return {
    id: BATCH_ID,
    tenderId: "B24-12345",
    title: "З-00142",
    customer: "Supplier EСтрой»",
    supplier: "ООО «Поставщик»",
    status: "RUNNING",
    finalVerdict: "PENDING",
    version: 1,
    author: { id: "u-1", displayName: "Менеджер" },
    items: [{ id: ITEM_ID, positionNo: 1, name: "Насос Н1", status }],
    createdAt: "2026-08-29T10:00:00Z",
    updatedAt: "2026-08-29T11:00:00Z",
  };
}

/** Открывает карточку заявки с единственной позицией в нужном статусе. */
async function openItem(page: Page, options: Options = {}) {
  const status = options.status ?? "AWAITING_PARAMETER_REVIEW";
  const body = batch(status);

  await stubSession(page);
  // Один обработчик на весь API: маршрутизация по пути и методу надёжнее
  // набора glob-масок — запросы с query-параметрами мимо масок не проходят.
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
    if (pathname === "/api/v1/check-batches") {
      return json(route, { items: [body], nextCursor: null });
    }
    if (pathname === `/api/v1/check-batches/${BATCH_ID}`) {
      return json(route, body);
    }
    if (pathname.endsWith("/documents")) {
      return json(route, { requirement: [], fact: [] });
    }
    if (pathname.endsWith("/runs")) {
      return json(route, []);
    }
    if (pathname.endsWith("/parameters/confirm")) {
      return (
        options.onConfirm?.(route) ??
        json(
          route,
          { checkItemId: ITEM_ID, pipelineRunId: "r-1", status: "QUEUED" },
          202,
        )
      );
    }
    if (pathname.endsWith("/parameters")) {
      if (post) {
        return options.onAdd?.(route) ?? json(route, FACT_PARAMETER, 201);
      }
      return json(route, {
        items: options.parameters ?? [REQUIREMENT_PARAMETER, FACT_PARAMETER],
        nextCursor: null,
      });
    }
    if (pathname.endsWith("/revisions")) {
      return options.onRevise?.(route) ?? json(route, FACT_PARAMETER, 201);
    }
    if (pathname.endsWith("/comparisons")) {
      return json(route, options.comparisons ?? [COMPARISON]);
    }
    if (pathname.endsWith("/override")) {
      return (
        options.onOverride?.(route) ??
        json(route, { ...COMPARISON, overridden: true, version: 2 })
      );
    }
    if (pathname.endsWith("/verdict")) {
      if (post) {
        return (
          options.onVerdict?.(route) ??
          json(route, { ...VERDICT, managerVerdict: "COMPLIANT", version: 2 })
        );
      }
      return json(route, VERDICT);
    }
    // Незнакомый путь отвечает пустым списком: тест про экран позиции, а не
    // про остальные экраны.
    return json(route, []);
  });

  await page.goto("/orders");
  await page.getByRole("button", { name: "Открыть" }).click();
  await expect(page.getByText("Позиция 1. Насос Н1")).toBeVisible();
}

function dialog(page: Page) {
  return page.getByRole("dialog");
}

test("таблица параметров показывает роль, нормализацию и цитату", async ({
  page,
}) => {
  await openItem(page);

  const row = page.getByRole("row").filter({ hasText: "Мощность" }).last();
  await expect(row).toContainText("Факт");
  await expect(row).toContainText("12,5");
  await expect(row).toContainText("Мощность 12,5 кВт");
});

test("после правки источником считается ручная цитата, а не машинная", async ({
  page,
}) => {
  // Регрессия: экран брал первую цитату из списка, а после ручной правки это
  // машинная цитата прежнего значения — она нового значения не подтверждает.
  await openItem(page, {
    parameters: [
      {
        ...FACT_PARAMETER,
        raw: { name: "Мощность", valueText: "20", unitText: "кВт" },
        manuallyEdited: true,
        version: 2,
        evidence: [
          { ...MACHINE_EVIDENCE, current: false },
          {
            fragmentId: "f-manual",
            quote: "Мощность: 20 кВт",
            quoteStart: 0,
            quoteEnd: 16,
            valueStart: 10,
            valueEnd: 12,
            verified: true,
            locator: { kind: "MANUAL", actorId: "u-1" },
            current: true,
          },
        ],
      },
    ],
  });

  const row = page.getByRole("row").filter({ hasText: "Мощность" }).last();
  await expect(row).toContainText("Мощность: 20 кВт");
  await expect(row).not.toContainText("Мощность 12,5 кВт");
  await expect(row).toContainText("ручной");
});

test("правка уходит с версией, причиной и комментарием", async ({ page }) => {
  let sent: Record<string, unknown> | null = null;
  await openItem(page, {
    onRevise: (route) => {
      sent = route.request().postDataJSON();
      return json(route, FACT_PARAMETER, 201);
    },
  });

  await page.getByRole("button", { name: "Изменить" }).last().click();
  const form = dialog(page);
  await expect(form).toContainText("Изменить параметр");
  await form.getByLabel("Значение").fill("20");
  await form.getByLabel("Комментарий").fill("Сверено с таблицей на стр. 4");
  await form.getByRole("button", { name: "Сохранить" }).click();

  await expect.poll(() => sent).not.toBeNull();
  expect(sent).toMatchObject({
    expectedVersion: 1,
    valueText: "20",
    comment: "Сверено с таблицей на стр. 4",
  });
  expect(typeof (sent as Record<string, unknown>).reasonCode).toBe("string");
});

test("ручное добавление уходит на позицию", async ({ page }) => {
  let sent: Record<string, unknown> | null = null;
  await openItem(page, {
    onAdd: (route) => {
      sent = route.request().postDataJSON();
      return json(route, FACT_PARAMETER, 201);
    },
  });

  await page.getByRole("button", { name: "Добавить" }).click();
  const form = dialog(page);
  await expect(form).toContainText("Добавить параметр");
  await form.getByLabel("Название").fill("Напор");
  await form.getByLabel("Значение").fill("32");
  await form.getByLabel("Единица").fill("м");
  await form.getByLabel("Комментарий").fill("Со страницы 3, таблица не читается");
  await form.getByRole("button", { name: "Сохранить" }).click();

  await expect.poll(() => sent).not.toBeNull();
  expect(sent).toMatchObject({
    name: "Напор",
    valueText: "32",
    unitText: "м",
    comment: "Со страницы 3, таблица не читается",
  });
});

test("позиция без распознанных значений зовёт ввести их руками", async ({
  page,
}) => {
  // Регрессия: пустой паспорт ронял прогон, и позиция становилась тупиком —
  // править параметры можно только после успешного извлечения.
  await openItem(page, { status: "NEEDS_INPUT", parameters: [] });

  await expect(
    page.getByText("не распозналось ни одного значения", { exact: false }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Добавить" })).toBeVisible();
  // Подтверждать нечего: сопоставление запускается только из разбора.
  await expect(
    page.getByRole("button", { name: "Подтвердить параметры" }),
  ).toHaveCount(0);
});

test("подтверждение параметров запускает сопоставление", async ({ page }) => {
  let called = false;
  await openItem(page, {
    onConfirm: (route) => {
      called = true;
      return json(
        route,
        { checkItemId: ITEM_ID, pipelineRunId: "r-1", status: "QUEUED" },
        202,
      );
    },
  });

  await page.getByRole("button", { name: "Подтвердить параметры" }).click();

  await expect.poll(() => called).toBe(true);
  await expect(
    page.getByText("сопоставление запущено", { exact: false }),
  ).toBeVisible();
});

test("ручной статус строки уходит с обязательным комментарием", async ({
  page,
}) => {
  let sent: Record<string, unknown> | null = null;
  await openItem(page, {
    status: "REVIEW_REQUIRED",
    onOverride: (route) => {
      sent = route.request().postDataJSON();
      return json(route, { ...COMPARISON, overridden: true, version: 2 });
    },
  });

  await expect(page.getByText("Сопоставление")).toBeVisible();
  await page.getByRole("button", { name: "Статус" }).click();
  const form = dialog(page);
  await expect(form).toContainText("Ручной статус");
  // Без комментария сохранение недоступно — это требование контракта.
  await expect(form.getByRole("button", { name: "Сохранить" })).toBeDisabled();
  await form.getByLabel("Комментарий").fill("Отклонение согласовано");
  await form.getByRole("button", { name: "Сохранить" }).click();

  await expect.poll(() => sent).not.toBeNull();
  expect(sent).toMatchObject({
    expectedVersion: 1,
    comment: "Отклонение согласовано",
  });
});

test("вердикт утверждается решением менеджера", async ({ page }) => {
  let sent: Record<string, unknown> | null = null;
  await openItem(page, {
    status: "READY_FOR_MANAGER_REVIEW",
    onVerdict: (route) => {
      sent = route.request().postDataJSON();
      return json(route, { ...VERDICT, managerVerdict: "COMPLIANT", version: 2 });
    },
  });

  await expect(
    page.getByRole("heading", { name: "Вердикт", exact: true }),
  ).toBeVisible();
  // На карточке заявки рядом живёт «Утвердить заявку» — нужен точный матч.
  await page.getByRole("button", { name: "Утвердить", exact: true }).click();

  await expect.poll(() => sent).not.toBeNull();
  expect(sent).toMatchObject({ expectedVersion: 1, verdict: "COMPLIANT" });
});
