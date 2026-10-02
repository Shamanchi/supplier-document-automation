import { expect, test } from '@playwright/test';

const rule = {
  id: 'rule-1', supplier_id: null, supplier_name: null, source_import_id: null,
  name: 'Базовое XLSX', loader: 'xlsx', header_hash: 'a', fingerprint: {},
  recipe: { sheets: [{ match: { name_pattern: 'Прайс' }, header_row: 0,
    columns: [{ index: 0, header: 'Наименование', role: 'name' },
      { index: 1, header: 'Цена', role: 'price' }] }] },
  revision: 1, status: 'active', provenance: 'manual', created_from_version_id: null,
  source_version: null,
  validation: { passed: true }, created_at: '2026-07-19T00:00:00Z', updated_at: '2026-07-19T00:00:00Z',
  usage: { price_versions: 2, database_items: 125, source_rows: 120 },
};

test('category paging, active navigation, rules and API workspaces', async ({ page }) => {
  // Неучтённый запрос копится здесь и роняет тест явным сообщением: иначе смена
  // пути в клиенте выглядит как «элемент не найден».
  const unmocked: string[] = [];
  await page.addInitScript(() => window.localStorage.setItem('price-loader-session-present', '1'));
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/auth/refresh')) {
      return route.fulfill({ json: { access_token: 'test', token_type: 'bearer', expires_in: 3600, user: { username: 'operator', role: 'admin' } } });
    }
    if (url.pathname.endsWith('/projects')) return route.fulfill({ json: [{ id: 'project-1', slug: 'original-prices', name: 'Оригинальные прайсы', description: null, is_default: true, is_archived: false, price_files_count: 1, items_count: 1, global_categories_count: 1 }] });
    if (url.pathname.endsWith('/price-versions')) {
      return route.fulfill({ json: { total: 1, items: [{
        id: 'version-1', supplier_id: 'supplier-1', supplier_external_id: null,
        supplier_name: 'Старый импорт', version_no: 3, status: 'parsed',
        filename: 'legacy-price.xlsx', item_count: 1, warnings_count: 0, errors_count: 0,
        created_at: '2026-07-20T12:00:00Z', import_id: null,
      }] } });
    }
    if (url.pathname.endsWith('/price-items')) {
      return route.fulfill({ json: { total: 1, items: [{
        id: 'item-1', source_name: 'Огнетушитель ОП-5', normalized_name: 'огнетушитель оп 5',
        article: 'ОП-5', unit: 'шт', category_path: 'Пожарное оборудование', dimensions: null,
        description: null, attributes: [], source_sheet: 'Лист1', source_row: 7,
        source_section_id: null, source_panel_index: null, source_column_start: null,
        source_column_end: null, provenance: null, prices: [{
          kind: 'retail', amount: '1250.50', qualifier: 'exact', amount_from: null,
          amount_to: null, currency: 'RUB', currency_source: 'explicit', min_quantity: null,
          basis_quantity: null, basis_unit: null, raw_value: '1250.50', source_column_index: 3,
          vat_mode: 'unknown', is_on_request: false, source_label: 'Цена',
        }],
        category: { source_path: 'Пожарное оборудование', supplier_category_id: null,
          supplier_normalized_path: null, global_category_id: null, global_path: null,
          mapping_status: 'unmapped', matching_identity: 'Огнетушитель ОП-5', matching_scope: null },
        supplier_id: 'supplier-1', supplier_name: 'Старый импорт', price_version_id: 'version-1',
        version_no: 3, version_status: 'parsed', filename: 'legacy-price.xlsx',
        imported_at: '2026-07-20T12:00:00Z',
      }] } });
    }
    if (url.pathname.endsWith('/catalog/stats')) {
      return route.fulfill({ json: { supplier_categories_count: 800, mapped_count: 120, unmapped_count: 680, global_categories_count: 95 } });
    }
    if (url.pathname.endsWith('/catalog/suppliers')) return route.fulfill({ json: [] });
    if (url.pathname.endsWith('/catalog/supplier-categories')) {
      return route.fulfill({ json: { total: 800, items: [{
        id: 'source-1', supplier_id: 'supplier-1', supplier_name: 'Поставщик', parent_id: null,
        source_name: 'Огнетушители', source_path: 'Пожарное оборудование / Огнетушители',
        normalized_path: 'пожарное оборудование / огнетушители', aliases: [],
        global_category: null, mapping_source: null,
      }] } });
    }
    if (url.pathname.endsWith('/catalog/categories')) {
      return route.fulfill({ json: { total: 95, items: [{
        id: 'global-1', parent_id: null, name: 'Огнетушители', normalized_name: 'огнетушители',
        path: 'Огнетушители', normalized_path: 'огнетушители', status: 'active', supplier_mappings_count: 12,
      }] } });
    }
    if (url.pathname.endsWith('/parse-rules')) {
      return route.fulfill({ json: { total: 1, items: [rule] } });
    }
    if (url.pathname.endsWith('/parse-rules/rule-1')) return route.fulfill({ json: rule });
    if (url.pathname.endsWith('/service-clients')) return route.fulfill({ json: [] });
    unmocked.push(`${route.request().method()} ${url.pathname}`);
    return route.fulfill({ status: 404, json: { detail: 'not mocked' } });
  });

  await page.goto('/categories');
  await expect(page.getByRole('link', { name: 'Категории' })).toHaveAttribute('aria-current', 'page');
  await expect(page.getByRole('heading', { name: 'Категории товаров' })).toBeVisible();
  await expect(page.getByText('Всего: 800')).toBeVisible();
  await expect(page.getByText('Пожарное оборудование / Огнетушители')).toBeVisible();

  await page.getByRole('link', { name: 'Позиции' }).click();
  await expect(page.getByRole('heading', { name: 'Импортированные данные' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Позиции' })).toHaveAttribute('aria-current', 'page');
  await expect(page.getByText('Огнетушитель ОП-5')).toBeVisible();
  await expect(page.getByText('legacy-price.xlsx · версия 3')).toBeVisible();

  await page.getByRole('link', { name: 'Правила' }).click();
  await expect(page.getByRole('heading', { name: 'Правила разбора' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Правила' })).toHaveAttribute('aria-current', 'page');
  await expect(page.getByText('Базовое XLSX')).toBeVisible();
  await expect(page.getByRole('cell', { name: '120' })).toBeVisible();
  // Редактор правила — отдельная страница, а не модальное окно: спека ждала
  // модалку и не замечала, что переход по «Открыть» ведёт на /rules/:id.
  await page.getByRole('button', { name: /Открыть/ }).click();
  await expect(page).toHaveURL(/\/rules\/rule-1$/);
  await expect(page.getByRole('textbox', { name: 'Название правила' })).toHaveValue('Базовое XLSX');
  await expect(page.getByRole('cell', { name: 'Позиций в БД' })).toBeVisible();
  await page.getByRole('link', { name: 'Правила' }).click();

  await page.getByRole('link', { name: 'API', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'API и интеграции' })).toBeVisible();
  await expect(page.getByRole('link', { name: 'API', exact: true })).toHaveAttribute('aria-current', 'page');

  expect(unmocked, 'клиент вызвал адрес, которого нет в моках').toEqual([]);
});
