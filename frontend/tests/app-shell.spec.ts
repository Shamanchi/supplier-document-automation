import { expect, test, type Page } from "@playwright/test";

import { json, stubSession } from "./session";

/**
 * Регрессия: «Профиль» в боковом меню был отдан в `TooltipTrigger asChild`
 * напрямую. Radix склеивает свой className с дочерним строкой, а NavLink
 * принимает функцию — она превращалась в текст, класс `nav-item` не
 * применялся, и пункт оставался тёмным на тёмном (цвет брался из
 * `a { color: inherit }`), а активное состояние не подсвечивалось.
 */

test.use({ serviceWorkers: "block" });

async function openApp(page: Page, path = "/dictionary") {
  await stubSession(page);
  await page.route("**/api/v1/dictionary", (route) =>
    json(route, {
      catalogVersion: null,
      parameters: [],
      units: [],
      materials: [],
    }),
  );
  await page.goto(path);
}

// Меню на мобильном сворачивается в нижнюю панель, а `sidebar-footer` там
// скрыт — проверка про цвет пункта относится к десктопной раскладке.
test.describe("боковое меню", () => {
  test.skip(
    ({ isMobile }) => Boolean(isMobile),
    "в мобильной раскладке подвал меню скрыт",
  );

  test("«Профиль» оформлен как остальные пункты меню", async ({ page }) => {
    await openApp(page);

    const profile = page.getByRole("link", { name: "Профиль" });
    // Сравнение именно с неактивным пунктом: активный подсвечен белым, и на
    // экране словаря «Словарь» дал бы ложное расхождение.
    const dashboard = page.getByRole("link", { name: "Дашборд" });

    // Класс, а не строка с исходником функции.
    await expect(profile).toHaveClass(/(^|\s)nav-item(\s|$)/);

    const [profileColor, dashboardColor] = await Promise.all([
      profile.evaluate((el) => getComputedStyle(el).color),
      dashboard.evaluate((el) => getComputedStyle(el).color),
    ]);
    expect(profileColor).toBe(dashboardColor);
  });

  test("активный «Профиль» подсвечивается", async ({ page }) => {
    await page.route("**/api/v1/profile", (route) =>
      json(route, {
        name: "Менеджер",
        email: "manager@docprice.internal",
        initials: "МН",
        role: "manager",
        roles: ["manager"],
        notifications: [],
        permissions: { manager: [] },
        permissionCatalog: [],
        mandatoryPermissions: { manager: [] },
      }),
    );
    await openApp(page, "/profile");

    await expect(page.getByRole("link", { name: "Профиль" })).toHaveClass(
      /(^|\s)active(\s|$)/,
    );
  });
});
