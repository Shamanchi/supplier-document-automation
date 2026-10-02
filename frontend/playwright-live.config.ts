import { defineConfig, devices } from "@playwright/test";

// Живой smoke шагов 06–07: сервер vite поднимается вручную (npm run dev),
// бэкенд — docker-compose (8801), MSW выключен.
export default defineConfig({
  testMatch: "live-smoke.spec.ts",
  timeout: 60_000,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:5199",
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 1000 },
    // головной chromium установлен, headless-shell — нет (устанавливать
    // ничего не будем: прогон разовый).
    channel: "chromium",
    launchOptions: { headless: true },
  },
});
