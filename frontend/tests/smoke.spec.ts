import { expect, test } from "@playwright/test";

import { json, stubApi, stubSession } from "./session";

/**
 * Смоук: каждый маршрут открывается без runtime-ошибок под вошедшим
 * пользователем.
 *
 * Сессия и ответы API подменяются на уровне сети (stubSession/stubApi),
 * поэтому смоук ловит именно падения отрисовки, а данные проверяют спеки
 * конкретных экранов. Список маршрутов — канон фазы 3; `/certificates`
 * исключён: в объединённом приложении живёт экран фазы 2, который ходит в
 * моковый контур (см. решения шага 08 в PROGRESS.md).
 */

test.use({ serviceWorkers: "block" });

// Только экраны, которые живут на перенесённом API (стабы сети дают им
// корректные пустые формы). Живые страницы прайс-контура (suppliers/goods/
// history) требуют реального бэка — они в live-smoke.spec.ts; /certificates —
// моковый контур фазы 2 (см. решения шага 08 в PROGRESS.md).
const ROUTES = [
  { path: "/dashboard", heading: "Дашборд" },
  { path: "/orders", heading: "Заявки (тендеры)" },
  { path: "/dictionary", heading: "Словарь" },
  { path: "/history", heading: "История проверок" },
  { path: "/profile", heading: "Профиль" },
] as const;

const BATCH = {
  id: "11111111-1111-1111-1111-111111111111",
  tenderId: null,
  title: "З-00142",
  customer: "Supplier EСтрой»",
  supplier: null,
  status: "QUEUED",
  finalVerdict: "PENDING",
  version: 1,
  author: { id: "u-1", displayName: "Менеджер" },
  items: [
    {
      id: "22222222-2222-2222-2222-222222222222",
      positionNo: 1,
      name: "Насос Н1",
      status: "QUEUED",
    },
  ],
  createdAt: "2026-08-18T10:00:00Z",
  updatedAt: "2026-08-18T11:00:00Z",
};

test("маршруты открываются без runtime-ошибок", async ({ page }, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));

  await stubApi(page);
  await stubSession(page);

  for (const { path, heading } of ROUTES) {
    await page.goto(path);
    // Оболочка отрисовалась — значит маршрут не упал и не увёл на вход.
    await expect(page.getByRole("navigation")).toBeVisible();
    await expect(page.getByRole("heading", { name: heading })).toBeVisible();
  }

  await page.screenshot({
    path: testInfo.outputPath("routes.png"),
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("карточка заявки раскрывается и вкладки контуров переключаются", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));

  await stubApi(page, {
    "/api/v1/check-batches": { items: [BATCH], nextCursor: null },
    [`/api/v1/check-batches/${BATCH.id}`]: BATCH,
  });
  await stubSession(page);

  await page.goto("/orders");
  await page.getByRole("button", { name: "Открыть" }).click();
  await expect(page.getByText("Заявка №З-00142")).toBeVisible();
  await expect(page.getByText("Позиция 1. Насос Н1")).toBeVisible();

  await page.getByRole("tab", { name: "Проверка цен" }).click();
  await expect(
    page.getByText("Нет позиций для подбора цен", { exact: false }),
  ).toBeVisible();

  await page.getByRole("tab", { name: "КП", exact: true }).click();
  await expect(
    page.getByText("КП появится после создания расчёта", { exact: false }),
  ).toBeVisible();

  expect(errors).toEqual([]);
});

test("роль manager не пускает в «Управление»", async ({ page, isMobile }) => {
  await stubApi(page);
  await stubSession(page, { roles: ["manager"] });

  await page.goto("/management");

  // ManagementRoute уводит на профиль. Пункт меню помечен aria-disabled;
  // на мобильном раскладка прячет боковое меню, поэтому проверяется только
  // редирект.
  await expect(page).toHaveURL(/\/profile$/);
  if (!isMobile) {
    await expect(
      page.getByRole("link", { name: "Управление" }),
    ).toHaveAttribute("aria-disabled", "true");
  }
});

test("администратор в «Управление» проходит", async ({ page }) => {
  await stubApi(page, {}, { roles: ["admin"] });
  await stubSession(page, { roles: ["admin"], displayName: "Администратор" });

  await page.goto("/management");

  await expect(page.getByRole("heading", { name: "Управление" })).toBeVisible();
});

test("без сессии защищённый маршрут уводит на вход", async ({ page }) => {
  await stubApi(page);
  // Refresh-cookie нет: сервер отвечает 401, и приложение обязано показать вход.
  await page.route("**/api/v1/auth/refresh", (route) =>
    json(route, { error: { code: "UNAUTHENTICATED", message: "Требуется вход" } }, 401),
  );

  await page.goto("/orders");

  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Вход" })).toBeVisible();
});



