import { expect, test, type Page, type Route } from "@playwright/test";

import { json, stubSession } from "./session";

/**
 * Вердикт заявки и сводный отчёт на карточке заявки.
 *
 * Поведение эндпоинтов покрыто backend/tests/test_batch_verdict.py; здесь
 * проверяется, что экран показывает расчёт, не предлагает подтвердить
 * неготовое и отправляет решение так, как описано в docs/api/verdicts.
 */

test.use({ serviceWorkers: "block" });

const BATCH_ID = "11111111-1111-1111-1111-111111111111";
const ITEM_ID = "22222222-2222-2222-2222-222222222222";
const ARTIFACT_ID = "33333333-3333-3333-3333-333333333333";

const BATCH = {
  id: BATCH_ID,
  tenderId: "B24-12345",
  title: "З-00142",
  customer: "Supplier EСтрой»",
  supplier: "ООО «Поставщик»",
  status: "RUNNING",
  finalVerdict: "NON_COMPLIANT",
  version: 1,
  author: { id: "u-1", displayName: "Менеджер" },
  items: [
    { id: ITEM_ID, positionNo: 1, name: "Насос Н1", status: "VERDICT_APPROVED" },
  ],
  createdAt: "2026-08-30T10:00:00Z",
  updatedAt: "2026-08-30T11:00:00Z",
};

const VERDICT = {
  id: BATCH_ID,
  scope: "CHECK_BATCH",
  scopeId: BATCH_ID,
  computedVerdict: "NON_COMPLIANT",
  managerVerdict: "PENDING",
  basis: { items: 1, COMPLIANT: 0, PARTIAL: 0, NON_COMPLIANT: 1, PENDING: 0 },
  comment: null,
  approvedBy: null,
  approvedAt: null,
  version: 3,
};

const REPORT = {
  id: "44444444-4444-4444-4444-444444444444",
  snapshotId: "44444444-4444-4444-4444-444444444444",
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

interface Options {
  verdict?: Record<string, unknown>;
  onDecide?: (route: Route) => unknown;
  onReport?: (route: Route) => unknown;
}

async function openBatch(page: Page, options: Options = {}) {
  const verdict = { ...VERDICT, ...(options.verdict ?? {}) };

  await stubSession(page);
  await page.route("**/api/v1/**", (route) => {
    const request = route.request();
    const { pathname } = new URL(request.url());
    const post = request.method() === "POST";
    const batchScope = pathname.includes("/check-batches/");

    // /projects — загрузка проектов фазы 2 (стаб в stubSession, session.ts).
    if (
      pathname.endsWith("/auth/refresh") ||
      pathname.endsWith("/auth/me") ||
      pathname.endsWith("/projects")
    ) {
      return route.fallback();
    }
    if (pathname === "/api/v1/check-batches") {
      return json(route, { items: [BATCH], nextCursor: null });
    }
    if (pathname === `/api/v1/check-batches/${BATCH_ID}`) {
      return json(route, BATCH);
    }
    if (pathname.endsWith("/verdict") && batchScope) {
      if (post) {
        return (
          options.onDecide?.(route) ??
          json(route, { ...verdict, managerVerdict: "NON_COMPLIANT", version: 4 })
        );
      }
      return json(route, verdict);
    }
    if (pathname.endsWith("/reports") && batchScope) {
      return options.onReport?.(route) ?? json(route, REPORT, 201);
    }
    if (pathname.endsWith("/download")) {
      return route.fulfill({
        status: 200,
        contentType:
          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers: { "content-disposition": 'attachment; filename="batch.xlsx"' },
        body: "PK",
      });
    }
    if (pathname.endsWith("/documents")) {
      return json(route, { requirement: [], fact: [] });
    }
    if (pathname.endsWith("/parameters")) {
      return json(route, { items: [], nextCursor: null });
    }
    if (pathname.endsWith("/verdict")) {
      return json(route, {
        id: "v-item",
        scope: "CHECK_ITEM",
        scopeId: ITEM_ID,
        computedVerdict: "NON_COMPLIANT",
        managerVerdict: "NON_COMPLIANT",
        basis: {},
        comment: null,
        approvedBy: "u-1",
        approvedAt: "2026-08-30T11:30:00Z",
        version: 2,
      });
    }
    return json(route, []);
  });

  await page.goto("/orders");
  await page.getByRole("button", { name: "Открыть" }).click();
  await expect(
    page.getByRole("heading", { name: "Вердикт заявки" }),
  ).toBeVisible();
}

test("панель показывает расчёт заявки и число сведённых позиций", async ({
  page,
}) => {
  await openBatch(page);

  // Внешняя панель списка заявок тоже section.panel и содержит вложенную —
  // берётся самая глубокая из совпавших.
  const panel = page
    .locator("section.panel")
    .filter({ hasText: "Позиций сведено" })
    .last();
  await expect(panel).toContainText("Не соответствует");
  await expect(panel).toContainText("Не подтверждён");
  await expect(panel).toContainText("Позиций сведено");
});

test("неготовая заявка объясняет, чего ждать, вместо кнопки", async ({ page }) => {
  await openBatch(page, {
    verdict: {
      computedVerdict: "PENDING",
      basis: { items: 2, PENDING: 1, COMPLIANT: 1 },
    },
  });

  await expect(
    page.getByText("по каждой позиции будет результат", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Утвердить заявку" }),
  ).toHaveCount(0);
});

test("подтверждение заявки уходит с версией и комментарием", async ({ page }) => {
  let sent: Record<string, unknown> | null = null;
  await openBatch(page, {
    onDecide: (route) => {
      sent = route.request().postDataJSON();
      return json(route, {
        ...VERDICT,
        managerVerdict: "PARTIAL",
        comment: "Отклонение согласовано",
        version: 4,
      });
    },
  });

  await page
    .getByRole("textbox", { name: "Комментарий" })
    .fill("Отклонение согласовано");
  await page.getByRole("button", { name: "Утвердить заявку" }).click();

  await expect.poll(() => sent).not.toBeNull();
  expect(sent).toMatchObject({
    expectedVersion: 3,
    comment: "Отклонение согласовано",
  });
});

test("сводный отчёт собирается и скачивается", async ({ page }) => {
  let called = false;
  await openBatch(page, {
    verdict: { managerVerdict: "NON_COMPLIANT", approvedAt: "2026-08-30T11:40:00Z" },
    onReport: (route) => {
      called = true;
      return json(route, REPORT, 201);
    },
  });

  await page.getByRole("button", { name: "Сводный отчёт" }).click();

  await expect.poll(() => called).toBe(true);
  await expect(page.getByText("batch-0a1b2c3d4e5f.xlsx")).toBeVisible();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Скачать" }).click();
  expect((await download).suggestedFilename()).toContain("batch");
});
