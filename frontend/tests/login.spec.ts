import { expect, test } from "@playwright/test";

/**
 * Экран входа фазы 2 под рукой с бэком объединённого рантайма: поле объявлено
 * как `type="email" required`, поэтому на значении `admin` браузер молча
 * отменял отправку — запрос на /auth/login не уходил вовсе, и экран выглядел
 * неработающим. Спеки проверяют, что форма не глотает ни невалидный, ни
 * валидный ввод.
 *
 * Бэкенд для двух первых проверок не нужен: запрос до него не доходит.
 */

async function openLogin(page: import("@playwright/test").Page) {
  await page.goto("/login");
  // Экран входа фазы 2: заголовок «Вход», поле подписано «Логин».
  await expect(page.getByRole("heading", { name: "Вход" })).toBeVisible();
}

test("логин без домена объясняет, что нужен email целиком", async ({ page }) => {
  const loginAttempts: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/auth/login")) {
      loginAttempts.push(request.url());
    }
  });

  await openLogin(page);
  await page.getByLabel("Логин").fill("admin");
  await page.getByLabel("Пароль").fill("admin");
  await page.getByRole("button", { name: "Войти" }).click();

  // Поле — `type="email" required` (экран фазы 2): браузер молча отменяет
  // отправку, поэтому запрос на сервер не уходит вовсе. Сообщение клиентской
  // валидации не подменяет ответ сервера.
  await expect(page).toHaveURL(/\/login$/);
  expect(loginAttempts).toEqual([]);
});

test("пустой пароль не отправляет форму", async ({ page }) => {
  await openLogin(page);
  await page.getByLabel("Логин").fill("admin@docprice.internal");
  await page.getByRole("button", { name: "Войти" }).click();

  // `required` на поле пароля держит форму на месте.
  await expect(page).toHaveURL(/\/login$/);
});

test("корректная пара уходит на сервер", async ({ page }) => {
  const loginAttempt = page.waitForRequest((request) =>
    request.url().includes("/auth/login"),
  );

  await openLogin(page);
  // Bootstrap-аккаунт фазы 2 (шаг 02): admin с паролем из .env.
  await page.getByLabel("Логин").fill("admin@docprice.internal");
  await page.getByLabel("Пароль").fill("admin");
  await page.getByRole("button", { name: "Войти" }).click();

  expect((await loginAttempt).method()).toBe("POST");
});

