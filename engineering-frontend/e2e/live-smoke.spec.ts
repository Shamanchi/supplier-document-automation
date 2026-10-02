import { expect, test } from '@playwright/test';

// Прогон интерфейса против живого backend, без единого мока: только так видно,
// что клиент и сервер сошлись на одних адресах. Моки такое не ловят — они
// отвечают на любой путь, который в них записан.
//
//   LIVE_API=http://127.0.0.1:8801 LIVE_USER=admin LIVE_PASSWORD=… \
//     npx playwright test e2e/live-smoke.spec.ts
const api = process.env.LIVE_API;
const username = process.env.LIVE_USER ?? 'admin';
const password = process.env.LIVE_PASSWORD ?? '';

test.skip(!api, 'нужен запущенный backend в LIVE_API');

test('операторские экраны работают против живого API', async ({ page }) => {
  const failures: string[] = [];
  const seen: string[] = [];

  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    await route.continue({ url: `${api}${url.pathname}${url.search}` });
  });
  page.on('response', (response) => {
    const url = new URL(response.url());
    if (!url.pathname.startsWith('/api/v1/')) return;
    const line = `${response.status()} ${response.request().method()} ${url.pathname}`;
    seen.push(line);
    // 401 до входа — часть штатного сценария: клиент пробует обновить сессию.
    const expected = response.status() < 400
      || (response.status() === 401 && url.pathname.endsWith('/auth/refresh'));
    if (!expected) failures.push(line);
  });
  page.on('pageerror', (error) => failures.push(`pageerror: ${error.message}`));

  await page.goto('/');
  await page.getByLabel('Логин').fill(username);
  await page.getByLabel('Пароль').fill(password);
  await page.getByRole('button', { name: 'Войти' }).click();

  await expect(page.getByRole('heading', { name: 'DocPrice · Импорт прайсов' })).toBeVisible();

  for (const [link, heading] of [
    ['Позиции', 'Импортированные данные'],
    ['Проверка', 'Очередь ручной проверки'],
    ['Категории', 'Категории товаров'],
    ['Правила', 'Правила разбора'],
    ['API', 'API и интеграции'],
  ] as const) {
    await page.getByRole('link', { name: link, exact: true }).click();
    await expect(page.getByRole('heading', { name: heading })).toBeVisible();
    await expect(page.getByRole('link', { name: link, exact: true })).toHaveAttribute(
      'aria-current',
      'page',
    );
  }

  await page.getByRole('link', { name: 'Прайсы', exact: true }).click();
  await expect(page.getByRole('link', { name: 'Прайсы', exact: true })).toHaveAttribute(
    'aria-current',
    'page',
  );

  expect(seen.length, 'интерфейс не сходил в API ни разу').toBeGreaterThan(5);
  expect(failures, 'запросы интерфейса, которые живой backend отверг').toEqual([]);
});
