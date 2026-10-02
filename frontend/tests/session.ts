import type { Page, Route } from "@playwright/test";

/**
 * Общие заглушки сессии и API для e2e.
 *
 * Каждому тесту нужен вошедший пользователь: экраны за `AuthenticatedRoute`
 * без него уводят на `/login`. Раскладывать одни и те же маршруты авторизации
 * по спекам смысла нет — здесь единственное место, где они описаны.
 */

export function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

export interface SessionOptions {
  roles?: string[];
  displayName?: string;
  email?: string;
}

/**
 * Проект прайс-контура фазы 2: `ProjectProvider` при старте запрашивает
 * `/api/v1/projects` и до непустого ответа блокирует рендер всего приложения
 * (экран «Не удалось загрузить проекты»). В фазе 3 этого запроса не было,
 * поэтому стаб обязан жить здесь, в общем хелпере сессии.
 */
export const PROJECTS = [
  {
    id: "11111111-aaaa-4aaa-8aaa-111111111111",
    slug: "e2e",
    name: "E2E-проект",
    description: null,
    is_default: true,
    is_archived: false,
    price_files_count: 0,
    items_count: 0,
    global_categories_count: 0,
  },
];

/** Восстановление сессии по refresh-cookie и профиль текущего пользователя. */
export async function stubSession(page: Page, options: SessionOptions = {}) {
  const roles = options.roles ?? ["manager"];
  const displayName = options.displayName ?? "Менеджер";
  const email = options.email ?? "manager@docprice.internal";

  await page.route("**/api/v1/auth/refresh", (route) =>
    json(route, {
      accessToken: "test-token",
      tokenType: "bearer",
      expiresIn: 900,
    }),
  );
  await page.route("**/api/v1/auth/me", (route) =>
    json(route, {
      id: "00000000-0000-0000-0000-000000000001",
      email,
      displayName,
      initials: displayName.slice(0, 2).toUpperCase(),
      roles,
      role: roles[0],
      permissions: Object.fromEntries(roles.map((role) => [role, []])),
    }),
  );
  // Загрузку проектов фазы 2 подменяем вместе с сессией: без проекта
  // приложение не отрисует ни один защищённый экран.
  await page.route("**/api/v1/projects", (route) => json(route, PROJECTS));
}

export function profilePayload(options: SessionOptions = {}) {
  const roles = options.roles ?? ["manager"];
  return {
    name: options.displayName ?? "Менеджер",
    email: options.email ?? "manager@docprice.internal",
    initials: "МН",
    role: roles[0],
    roles,
    notifications: [],
    permissions: Object.fromEntries(roles.map((role) => [role, []])),
    permissionCatalog: [],
    mandatoryPermissions: Object.fromEntries(roles.map((role) => [role, []])),
  };
}

/**
 * Пустые, но правильные по форме ответы экранов.
 *
 * Смоук проверяет, что маршруты открываются без runtime-ошибок, а не что в
 * них есть данные: наполнение проверяют спеки конкретных экранов.
 */
function emptyBodies(options: SessionOptions) {
  return {
    // Проект обязателен: без него ProjectProvider фазы 2 блокирует рендер.
    "/api/v1/projects": PROJECTS,
    "/api/v1/dashboard": {
      period: { from: "2026-08-01", to: "2026-08-18", days: 18 },
      kpis: [],
      recentCheckBatches: [],
    },
    "/api/v1/check-batches": { items: [], nextCursor: null },
    "/api/v1/dictionary": {
      catalogVersion: null,
      parameters: [],
      units: [],
      materials: [],
    },
    "/api/v1/profile": profilePayload(options),
    "/api/v1/certificates": { items: [], nextCursor: null },
    "/api/v1/admin": {
      settings: { vatRate: 20, confidenceHigh: 90, confidenceMedium: 70 },
      synonyms: [],
      templates: [],
    },
    // Экран поставщиков открывает карточку первого в списке и без него
    // показывает ошибку, поэтому один поставщик нужен даже смоуку.
    "/api/v1/suppliers": [
      {
        id: "s-1",
        name: "ООО «Поставщик»",
        products: [],
        files: [],
        pricing: {
          product: "—",
          purchasePrice: 0,
          retailPrice: 0,
          categoryDiscount: 0,
          markup: 0,
          priceUploadedAt: "—",
          certificateUpdatedAt: "—",
        },
        discountRules: [],
        certificates: [],
      },
    ],
  } as Record<string, unknown>;
}

/**
 * Ловит все запросы к API. Регистрируется первым: у Playwright выигрывает
 * маршрут, объявленный последним, поэтому точечные заглушки теста перекрывают
 * общую.
 */
export async function stubApi(
  page: Page,
  overrides: Record<string, unknown> = {},
  options: SessionOptions = {},
) {
  const bodies = { ...emptyBodies(options), ...overrides };
  await page.route("**/api/v1/**", (route) => {
    const { pathname } = new URL(route.request().url());
    // Незнакомый путь отвечает пустым списком: экраны на моках рисуют пустую
    // таблицу, а не ошибку, и смоук остаётся про отрисовку.
    return json(route, bodies[pathname] ?? []);
  });
}
