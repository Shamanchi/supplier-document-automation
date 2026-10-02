import { expect, test, type Page, type Route } from "@playwright/test";

import { json, stubSession } from "./session";

/**
 * Экран «Словарь» на реальном API: проекция `GET /dictionary` показывается как
 * есть, дерево материалов собирается клиентом из плоского списка.
 *
 * Бэкенд подменяется на уровне сети: поведение самого эндпоинта покрыто
 * backend/tests/test_catalogs.py, здесь проверяется проводка экрана.
 */

// Без этого MSW успевает зарегистрировать service worker и сам обрабатывает
// запросы: до перехвата Playwright они не доходят.
test.use({ serviceWorkers: "block" });

function dictionaryPayload() {
  return {
    catalogVersion: "catalog-1.0.0",
    parameters: [
      {
        id: "p-1",
        code: "FLOW_RATE",
        canonical: "Производительность",
        valueKind: "NUMBER",
        dimension: "VOLUMETRIC_FLOW",
        unit: "м³/ч",
        synonyms: ["Расход", "Q"],
      },
      {
        id: "p-2",
        code: "BODY_MATERIAL",
        canonical: "Материал корпуса",
        valueKind: "MATERIAL",
        dimension: null,
        unit: null,
        synonyms: [],
      },
    ],
    units: [
      {
        id: "u-1",
        code: "MPA",
        symbol: "МПа",
        dimension: "PRESSURE",
        synonyms: ["мегапаскаль"],
        baseUnit: null,
        scale: null,
        offset: null,
      },
      {
        id: "u-2",
        code: "BAR",
        symbol: "бар",
        dimension: "PRESSURE",
        synonyms: ["bar"],
        baseUnit: "МПа",
        scale: "0.1",
        offset: "0",
      },
    ],
    materials: [
      {
        id: "m-1",
        code: "STEEL",
        name: "Сталь",
        path: "STEEL",
        parentId: null,
      },
      {
        id: "m-2",
        code: "STAINLESS",
        name: "Нержавеющая сталь",
        path: "STEEL.STAINLESS",
        parentId: "m-1",
      },
      {
        id: "m-3",
        code: "AISI316L",
        name: "AISI 316L",
        path: "STEEL.STAINLESS.AISI316L",
        parentId: "m-2",
      },
    ],
  };
}

async function openDictionary(
  page: Page,
  options: {
    dictionary?: unknown;
    mySuggestions?: unknown;
    suggestHandler?: (route: Route) => unknown;
  } = {},
) {
  await stubSession(page);
  await page.route("**/api/v1/dictionary", (route) =>
    json(route, options.dictionary ?? dictionaryPayload()),
  );
  // Один обработчик на оба метода: glob-маска не различает GET и POST, а у
  // списка своих предложений и у отправки нового один и тот же путь.
  await page.route("**/api/v1/dictionary/synonyms", (route) => {
    if (route.request().method() === "GET") {
      return json(route, options.mySuggestions ?? []);
    }
    return options.suggestHandler?.(route) ?? json(route, {}, 201);
  });

  await page.goto("/dictionary");
  await expect(page.getByRole("heading", { name: "Словарь" })).toBeVisible();
}

test("номенклатура показывает параметры каталога с синонимами", async ({
  page,
}) => {
  await openDictionary(page);

  await expect(page.getByText("Опубликованная версия каталога: catalog-1.0.0")).toBeVisible();

  const row = page.getByRole("row").filter({ hasText: "Производительность" });
  await expect(row).toContainText("FLOW_RATE");
  // Машинный value_kind показывается подписью для менеджера.
  await expect(row).toContainText("Число");
  await expect(row).toContainText("м³/ч");
  await expect(row).toContainText("Расход, Q");

  // Параметр без единицы и без синонимов не оставляет пустых ячеек.
  const material = page.getByRole("row").filter({ hasText: "Материал корпуса" });
  await expect(material).toContainText("Материал");
});

test("единицы показывают перевод в базовую", async ({ page }) => {
  await openDictionary(page);
  await page.getByRole("tab", { name: "Единицы измерения" }).click();

  const bar = page.getByRole("row").filter({ hasText: "бар" });
  await expect(bar).toContainText("× 0.1");
  await expect(bar).toContainText("МПа");

  // У базовой единицы правила нет — это не пропуск данных.
  const mpa = page.getByRole("row").filter({ hasText: "мегапаскаль" });
  await expect(mpa).toContainText("базовая");
});

test("материалы собираются в дерево из плоского списка", async ({ page }) => {
  await openDictionary(page);
  await page.getByRole("tab", { name: "Материалы" }).click();

  const root = page.locator(".material-tree > .mt-node");
  await expect(root).toHaveCount(1);
  await expect(root).toContainText("Сталь");
  // Вложенность восстановлена по parentId: AISI 316L лежит внутри нержавейки.
  await expect(root.locator(".mt-node .mt-node")).toContainText("AISI 316L");
});

test("без опубликованного каталога экран объясняет пустоту", async ({
  page,
}) => {
  await openDictionary(page, {
    dictionary: {
      catalogVersion: null,
      parameters: [],
      units: [],
      materials: [],
    },
  });

  await expect(page.getByText("Каталог не опубликован")).toBeVisible();
  await expect(
    page.getByText("Ни одна версия каталога ещё не опубликована", {
      exact: false,
    }),
  ).toBeVisible();
});

test("поиск номенклатуры находит по синониму", async ({ page }) => {
  await openDictionary(page);
  // Менеджер ищет словом из документа, а оно чаще синоним, чем канон.
  await page.getByLabel("Поиск по названию, коду и синонимам").fill("Расход");

  await expect(page.getByRole("row", { name: /Производительность/ })).toBeVisible();
  await expect(page.getByRole("row", { name: /Материал корпуса/ })).toHaveCount(0);
  await expect(page.getByText("Показано 1 из 2")).toBeVisible();
});

test("поиск номенклатуры находит по коду", async ({ page }) => {
  await openDictionary(page);
  await page.getByLabel("Поиск по названию, коду и синонимам").fill("body_material");

  await expect(page.getByRole("row", { name: /Материал корпуса/ })).toBeVisible();
  await expect(page.getByText("Показано 1 из 2")).toBeVisible();
});

test("ничего не найдено — это не пустой каталог", async ({ page }) => {
  await openDictionary(page);
  await page.getByLabel("Поиск по названию, коду и синонимам").fill("несуществующее");

  await expect(page.getByText("Ничего не найдено", { exact: false })).toBeVisible();
  await expect(
    page.getByText("В опубликованной версии каталога нет параметров"),
  ).toHaveCount(0);
});

test("фильтр по наличию единицы", async ({ page }) => {
  await openDictionary(page);
  await page.getByLabel("Наличие единицы").click();
  await page.getByRole("option", { name: "Без единицы" }).click();

  await expect(page.getByRole("row", { name: /Материал корпуса/ })).toBeVisible();
  await expect(page.getByRole("row", { name: /Производительность/ })).toHaveCount(0);
});

test("сброс возвращает все параметры", async ({ page }) => {
  await openDictionary(page);
  await page.getByLabel("Поиск по названию, коду и синонимам").fill("Расход");
  await expect(page.getByText("Показано 1 из 2")).toBeVisible();

  await page.getByRole("button", { name: "Сбросить" }).click();

  await expect(page.getByText("Показано 2 из 2")).toBeVisible();
});

test("сортировка по названию переключается и отключается", async ({ page }) => {
  await openDictionary(page);
  const names = () =>
    page.locator("tbody tr td:first-child").allTextContents();

  await expect.poll(names).toEqual(["Производительность", "Материал корпуса"]);

  await page.getByRole("button", { name: "Каноническое название" }).click();
  await expect.poll(names).toEqual(["Материал корпуса", "Производительность"]);

  await page.getByRole("button", { name: "Каноническое название" }).click();
  await expect.poll(names).toEqual(["Производительность", "Материал корпуса"]);

  // Третье нажатие снимает сортировку — возвращается порядок каталога.
  await page.getByRole("button", { name: "Каноническое название" }).click();
  await expect.poll(names).toEqual(["Производительность", "Материал корпуса"]);
});

test("поиск единиц ищет по обозначению и размерности", async ({ page }) => {
  await openDictionary(page);
  await page.getByRole("tab", { name: "Единицы измерения" }).click();
  await page.getByLabel("Поиск по обозначению, коду и размерности").fill("бар");

  await expect(page.getByRole("row", { name: /бар/ })).toBeVisible();
  await expect(page.getByText("Показано 1 из 2")).toBeVisible();
});

test("размерности показываются по-русски, а не кодами", async ({ page }) => {
  await openDictionary(page);
  await page.getByRole("tab", { name: "Единицы измерения" }).click();

  await expect(page.getByRole("cell", { name: "Давление" }).first()).toBeVisible();
  await expect(page.getByText("PRESSURE")).toHaveCount(0);
});

test("поиск единиц понимает русскую подпись размерности", async ({ page }) => {
  await openDictionary(page);
  await page.getByRole("tab", { name: "Единицы измерения" }).click();
  // Менеджер набирает «давление», а не PRESSURE.
  await page.getByLabel("Поиск по обозначению, коду и размерности").fill("давление");

  await expect(page.getByText("Показано 2 из 2")).toBeVisible();
});

test("фильтр размерности предлагает подписи, а не коды", async ({ page }) => {
  await openDictionary(page);
  await page.getByLabel("Размерность").click();

  await expect(
    page.getByRole("option", { name: "Объёмный расход" }),
  ).toBeVisible();
  await expect(page.getByRole("option", { name: "VOLUMETRIC_FLOW" })).toHaveCount(
    0,
  );
});

test("незнакомая размерность показывается как есть", async ({ page }) => {
  await openDictionary(page, {
    dictionary: {
      ...dictionaryPayload(),
      units: [
        {
          id: "u-9",
          code: "CUSTOM",
          symbol: "усл.",
          dimension: "SOMETHING_NEW",
          synonyms: [],
          baseUnit: null,
          scale: null,
          offset: null,
        },
      ],
    },
  });
  await page.getByRole("tab", { name: "Единицы измерения" }).click();

  // Прятать незнакомый код за прочерком значило бы скрыть содержимое каталога.
  await expect(page.getByRole("cell", { name: "SOMETHING_NEW" })).toBeVisible();
});

test("поиск материала сохраняет предков найденного узла", async ({ page }) => {
  await openDictionary(page);
  await page.getByRole("tab", { name: "Материалы" }).click();
  await page.getByLabel("Поиск по названию и коду материала").fill("AISI");

  // Совпавший узел показывается вместе с ветвью: без «Стали» менеджер потерял
  // бы место материала в иерархии. Проверяется само дерево, а не подсказка
  // под ним — она тоже упоминает AISI 316L.
  const tree = page.locator(".material-tree");
  await expect(tree).toContainText("AISI 316L");
  await expect(tree).toContainText("Сталь");

  await page.getByLabel("Поиск по названию и коду материала").fill("вольфрам");
  await expect(page.getByText("Ничего не найдено", { exact: false })).toBeVisible();
});

test("«Мои предложения» показывают статус и решение", async ({ page }) => {
  await openDictionary(page, {
    mySuggestions: [
      {
        id: "s-1",
        canonical: "Производительность",
        synonym: "Подача",
        status: "accepted",
        createdAt: "2026-09-01T10:00:00Z",
        decidedAt: "2026-09-01T12:00:00Z",
      },
      {
        id: "s-2",
        canonical: "Материал корпуса",
        synonym: "Корпус",
        status: "pending",
        createdAt: "2026-09-01T11:00:00Z",
        decidedAt: null,
      },
    ],
  });
  await page.getByRole("tab", { name: "Мои предложения" }).click();

  // Проверяется таблица, а не подсказка над ней — там тоже есть «Принятый».
  const rows = page.locator("tbody");
  await expect(rows.getByRole("cell", { name: "Подача" })).toBeVisible();
  await expect(rows.getByText("Принят", { exact: true })).toBeVisible();
  await expect(rows.getByText("Ожидает решения")).toBeVisible();
});

test("без предложений раздел объясняет, где их делать", async ({ page }) => {
  await openDictionary(page);
  await page.getByRole("tab", { name: "Мои предложения" }).click();

  await expect(
    page.getByText("Вы ещё не предлагали синонимов", { exact: false }),
  ).toBeVisible();
});

test("предложение синонима уходит на сервер", async ({ page }) => {
  let sent: { canonical: string; synonym: string } | null = null;
  await openDictionary(page, {
    suggestHandler: (route) => {
      sent = route.request().postDataJSON();
      return json(
        route,
        {
          id: "s-1",
          canonical: "Производительность",
          synonym: "Подача",
          status: "pending",
          createdAt: "2026-08-18T10:00:00Z",
        },
        201,
      );
    },
  });

  await page
    .getByRole("row")
    .filter({ hasText: "Производительность" })
    .getByRole("button", { name: "Предложить" })
    .click();
  await page.getByLabel("Новый синоним").fill("Подача");
  await page.getByRole("button", { name: "Отправить" }).click();

  await expect(
    page.getByText("Синоним отправлен Администратору на подтверждение"),
  ).toBeVisible();
  // Уходит стабильный идентификатор параметра: отображаемое имя ключом не
  // является — оно меняется при переименовании и может совпадать у разных.
  expect(sent).toEqual({
    canonical: "Производительность",
    canonicalParameterId: "p-1",
    synonym: "Подача",
  });
});

test("отмена сбрасывает форму синонима", async ({ page }) => {
  await openDictionary(page);
  const propose = page
    .getByRole("row")
    .filter({ hasText: "Производительность" })
    .getByRole("button", { name: "Предложить" });

  await propose.click();
  await page.getByLabel("Новый синоним").fill("Черновик");
  await page.getByRole("button", { name: "Отмена" }).click();

  // Без сброса введённый текст всплывал при открытии диалога для другого
  // параметра — менеджер отправлял чужой синоним, не заметив.
  await propose.click();
  await expect(page.getByLabel("Новый синоним")).toHaveValue("");
});

test("отказ каталога показывается сообщением, а не тишиной", async ({
  page,
}) => {
  await openDictionary(page, {
    suggestHandler: (route) =>
      json(
        route,
        {
          error: {
            code: "ALIAS_ALREADY_EXISTS",
            message: "Такой синоним уже есть в каталоге",
          },
        },
        409,
      ),
  });

  await page
    .getByRole("row")
    .filter({ hasText: "Производительность" })
    .getByRole("button", { name: "Предложить" })
    .click();
  await page.getByLabel("Новый синоним").fill("Расход");
  await page.getByRole("button", { name: "Отправить" }).click();

  await expect(page.getByText("Такой синоним уже есть в каталоге")).toBeVisible();
});
