import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  testMatch: "**/*.spec.ts",
  // live-smoke.spec.ts — живой бэк: гоняется отдельным конфигом
  // playwright-live.config.ts (vite поднимается вручную, MSW выключен).
  testIgnore: "live-smoke.spec.ts",
  timeout: 60_000,
  expect: {
    // vite в dev-режиме компилирует страницы по требованию: первый заход
    // в экран может занять больше 8 секунд, и «heading visible» флейкует.
    timeout: 15_000,
  },
  fullyParallel: false,
  // Один воркер: параллельные браузеры на одной машине перегружают
  // dev-сервер и укладывают холодную компиляцию за таймаут.
  workers: 1,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:5174",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    // головной chromium установлен, headless-shell — нет (как в
    // playwright-live.config.ts шага 07; ничего не докачиваем).
    channel: "chromium",
    launchOptions: { headless: true },
  },
  projects: [
    {
      name: "desktop",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 1000 },
      },
    },
    {
      name: "mobile",
      use: {
        ...devices["Pixel 7"],
      },
    },
  ],
  webServer: {
    // Живой бэк через vite-прокси на docker-compose (8801), MSW выключен —
    // условие прогона шага 08. Спеки, которым нужны конкретные ответы,
    // подменяют API на уровне сети (page.route + serviceWorkers: "block").
    // MSW здесь нельзя: worker.start() в спеках с "block" падает до
    // монтирования приложения (await enableMocking() в main.tsx).
    command: "npm run dev -- --host 127.0.0.1 --port 5174",
    url: "http://127.0.0.1:5174",
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
