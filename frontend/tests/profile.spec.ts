import { expect, test, type Page, type Route } from "@playwright/test";

import { json, stubSession } from "./session";

/**
 * Экран профиля объединённого приложения — версия фазы 2 (решение шага 06:
 * auth-модель P2 сохранена). Редактор прав виден только sysadmin, набор прав
 * в нём фиксирован (первые три — обязательные для всех ролей), а вот текущие
 * выделения, «доступные действия роли» и сохранение идут через серверный
 * контракт `GET/PATCH /profile` (роутер модуля profile фазы 3, шаг 03).
 *
 * Бэкенд подменяется на уровне сети: тест проверяет проводку экрана, а
 * поведение самих эндпоинтов покрыто backend-тестами модуля profile.
 */

// Без этого MSW успевает зарегистрировать service worker и сам обрабатывает
// запросы: до перехвата Playwright они не доходят, и подмена API не работает.
test.use({ serviceWorkers: "block" });

/** Обязательные права — первые три позиции списка экрана. */
const MANDATORY = [
  "Создание заявок",
  "Загрузка файлов",
  "Ручной ввод параметров",
];

const MANAGER_PERMISSIONS = [
  "Создание заявок",
  "Загрузка файлов",
  "Ручной ввод параметров",
  "Предложение синонимов",
];

function profilePayload() {
  return {
    name: "Системный администратор",
    email: "sysadmin@docprice.internal",
    initials: "СА",
    role: "sysadmin",
    roles: ["sysadmin"],
    notifications: [],
    permissions: {
      manager: MANAGER_PERMISSIONS,
      admin: ["Управление поставщиками"],
      sysadmin: MANDATORY,
    },
    permissionCatalog: [],
    mandatoryPermissions: {
      manager: MANDATORY,
      admin: ["Управление поставщиками"],
      sysadmin: MANDATORY,
    },
  };
}

/** Аутентифицированная сессия sysadmin и профиль из подменённого API. */
async function openProfile(page: Page, patchHandler?: (route: Route) => unknown) {
  await stubSession(page, {
    roles: ["sysadmin"],
    displayName: "Системный администратор",
    email: "sysadmin@docprice.internal",
  });
  await page.route("**/api/v1/profile", (route) => {
    if (route.request().method() === "GET") {
      return json(route, profilePayload());
    }
    return route.fallback();
  });
  if (patchHandler) {
    await page.route("**/api/v1/profile/permissions", patchHandler);
  }

  await page.goto("/profile");
  await expect(page.getByRole("heading", { name: "Профиль" })).toBeVisible();
}

test("sysadmin видит редактор прав, менеджер — нет", async ({ page }) => {
  await openProfile(page);
  await expect(page.getByText("Управление правами")).toBeVisible();

  // Тот же экран под менеджером: редактор не предлагается.
  await stubSession(page, { roles: ["manager"] });
  await page.goto("/profile");
  await expect(page.getByText("Управление правами")).toHaveCount(0);
});

test("доступные действия роли приходят с сервера", async ({ page }) => {
  await openProfile(page);

  // Текущая роль — sysadmin: список строится по permissions.sysadmin.
  const actions = page
    .locator("section")
    .filter({ hasText: "Доступные действия для роли" })
    .last();
  await expect(actions).toContainText("Создание заявок");
  await expect(actions).toContainText("Ручной ввод параметров");
  // Прав другой роли на экран не подтягиваются.
  await expect(actions).not.toContainText("Управление поставщиками");
});

test("обязательные права заблокированы для всех ролей", async ({ page }) => {
  await openProfile(page);

  const mandatoryRow = page.locator("label").filter({ hasText: "Создание заявок" });
  await expect(mandatoryRow.getByRole("checkbox")).toBeDisabled();
  await expect(mandatoryRow).toContainText("все роли");

  const optionalRow = page
    .locator("label")
    .filter({ hasText: "Управление товарами" });
  await expect(optionalRow.getByRole("checkbox")).toBeEnabled();
});

test("сохранение отправляет выбранную роль и набор прав", async ({ page }) => {
  let sent: { role: string; permissions: string[] } | null = null;
  await openProfile(page, (route) => {
    sent = route.request().postDataJSON();
    return json(route, profilePayload());
  });

  // На странице два combobox — селектор проекта в оболочке и выбор роли
  // в редакторе: берётся именно редактор.
  const editor = page.locator("section").filter({ hasText: "Управление правами" });
  await editor.getByRole("combobox").click();
  await page.getByRole("option", { name: "Менеджер" }).click();

  // Галочки предвыставлены по permissions.manager с сервера.
  const synonymRow = page
    .locator("label")
    .filter({ hasText: "Предложение синонимов" });
  await expect(synonymRow.getByRole("checkbox")).toBeChecked();

  await page
    .locator("label")
    .filter({ hasText: "Управление товарами" })
    .getByRole("checkbox")
    .click();
  await page.getByRole("button", { name: "Сохранить" }).click();

  await expect(page.getByText("Настройки прав сохранены")).toBeVisible();
  expect(sent?.role).toBe("manager");
  // Выбор менеджера ушёл вместе с серверными правами роли.
  expect(sent?.permissions).toContain("Предложение синонимов");
  expect(sent?.permissions).toContain("Управление товарами");
});
