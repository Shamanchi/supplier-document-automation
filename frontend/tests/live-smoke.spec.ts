import { expect, test } from "@playwright/test";

// Живой smoke: реальный бэк (docker-compose 8801 через vite-прокси),
// vite поднят вручную на 5199, MSW выключен. Логин — admin фазы 2.
// Прогон — только через playwright-live.config.ts (не попадает в основной
// набор: там webServer поднимает MSW-профиль 5174).
test("страница заявки: вкладки контуров не роняют приложение", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));

  await page.goto("/login");
  await page.getByLabel("Логин").fill("admin@docprice.internal");
  await page.getByLabel("Пароль").fill("admin");
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).not.toHaveURL(/login/);

  await page.goto("/orders");
  await page.getByRole("button", { name: "Открыть" }).first().click();
  await expect(page.locator(".order-detail-cell")).toBeVisible();

  // Вкладка «Проверка цен» (P2-контур): живая, не падение. У заявки с
  // позициями рендерится таблица подбора (пикер), без позиций — пустое
  // состояние.
  await page.getByRole("tab", { name: "Проверка цен" }).click();
  await expect(
    page.getByText(/Таблица цен|Нет позиций для подбора цен/).first(),
  ).toBeVisible();

  // Вкладка «КП» (P2-контур): пустое состояние
  await page.getByRole("tab", { name: "КП", exact: true }).click();
  await expect(page.getByText(/КП появится после создания расчёта/)).toBeVisible();

  // Вкладка позиций остаётся рабочей (конвейер фазы 3)
  await page.getByRole("tab", { name: /Позиции и документы/ }).click();
  await expect(
    page.locator(".order-detail-cell").getByText(/Позиция \d|В заявке нет позиций/).first(),
  ).toBeVisible();

  expect(errors).toEqual([]);
});

test("страницы P2 (прайс-контур) не пострадали", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));

  await page.goto("/login");
  await page.getByLabel("Логин").fill("admin@docprice.internal");
  await page.getByLabel("Пароль").fill("admin");
  await page.getByRole("button", { name: "Войти" }).click();
  await expect(page).not.toHaveURL(/login/);

  const routes: Array<[string, RegExp]> = [
    ["/suppliers", /Поставщики|Импорт прайс/],
    ["/goods", /Товары|Номенклатура/],
    ["/dashboard", /Дашборд|Сводка/],
    ["/history", /История проверок/],
    ["/management", /Управление/],
  ];
  for (const [route, heading] of routes) {
    await page.goto(route);
    await expect(page.getByRole("heading", { name: heading }).first()).toBeVisible();
  }
  expect(errors).toEqual([]);
});

