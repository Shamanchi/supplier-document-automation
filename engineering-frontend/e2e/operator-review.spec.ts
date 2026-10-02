import { expect, test, type Page } from '@playwright/test';

const actions = { can_edit: true, can_validate: true, can_apply: false, can_activate: false };
const queue = {
  total: 104,
  totals: { pending: 104, in_progress: 0, validated: 0, accepted: 0, rejected: 0 },
  items: [{
    id: 'queue-1', import_id: 'import-1', review_id: 'review-1', section_id: 'sec-1',
    supplier_name: 'Поставщик', filename: 'прайс.xlsx', sheet_name: 'Прайс',
    section_kind: 'price_rows', start_row_index: 1, end_row_index: 2,
    status: 'pending', attempt_status: 'rejected', reason_code: 'MAPPING_REQUIRED',
    revision: 1, review_status: 'draft', decision_count: 0, can_edit: true,
    updated_at: '2026-07-19T00:00:00Z',
  }],
};
const review = {
  import_id: 'import-1', review_id: 'review-1', revision: 1, status: 'draft',
  mapping: null, rule: null, section_decisions: [], currency_override: null,
  sections: [{ section_id: 'sec-1' }], attempts: [], validation: null, actions,
  updated_by: 'admin', updated_at: '2026-07-19T00:00:00Z',
};
const section = {
  import_id: 'import-1', review_id: 'review-1', section_id: 'sec-1', revision: 1,
  sheet_name: 'Прайс', section_kind: 'price_rows', header_row_index: 0,
  header_values: ['Наименование', 'Артикул', 'Цена'], horizontal_panels: [],
  mapping: null, decisions: [], attempt: { error_message: 'Нужно назначить колонки', validation: null },
  validation: null, actions,
  rows: [
    { row_index: 0, excel_row: 1, kind: 'header', cells: [
      { column_index: 0, excel_column: 'A', value: 'Наименование', is_bold: true, is_merged_fill: false },
      { column_index: 1, excel_column: 'B', value: 'Артикул', is_bold: true, is_merged_fill: false },
      { column_index: 2, excel_column: 'C', value: 'Цена', is_bold: true, is_merged_fill: false },
    ] },
    { row_index: 1, excel_row: 2, kind: 'source', cells: [
      { column_index: 0, excel_column: 'A', value: 'Насос циркуляционный', is_bold: false, is_merged_fill: false },
      { column_index: 1, excel_column: 'B', value: 'НЦ-25', is_bold: false, is_merged_fill: false },
      { column_index: 2, excel_column: 'C', value: 12500, is_bold: false, is_merged_fill: false },
    ] },
  ],
};

// Неучтённый запрос копится здесь, а тест падает на нём явно. Иначе смена пути
// в клиенте выглядит как «элемент не найден» — по такому сообщению непонятно,
// что сломался не рендер, а адрес.
async function mockApi(page: Page): Promise<string[]> {
  const unmocked: string[] = [];
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/auth/refresh')) {
      return route.fulfill({ json: { access_token: 'test', token_type: 'bearer', expires_in: 3600, user: { username: 'operator', role: 'admin' } } });
    }
    if (url.pathname.endsWith('/projects')) return route.fulfill({ json: [{ id: 'project-1', slug: 'original-prices', name: 'Оригинальные прайсы', description: null, is_default: true, is_archived: false, price_files_count: 1, items_count: 1, global_categories_count: 1 }] });
    if (url.pathname.endsWith('/review/queue')) return route.fulfill({ json: queue });
    if (url.pathname.endsWith('/sections/sec-1')) return route.fulfill({ json: section });
    if (url.pathname.endsWith('/review/import-1')) return route.fulfill({ json: review });
    unmocked.push(`${route.request().method()} ${url.pathname}`);
    return route.fulfill({ status: 404, json: { detail: 'not mocked' } });
  });
  return unmocked;
}

for (const viewport of [
  { name: 'desktop', width: 1366, height: 768 },
  { name: 'mobile', width: 390, height: 844 },
]) {
  test(`${viewport.name} operator workspace smoke`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const unmocked = await mockApi(page);
    await page.addInitScript(() => window.localStorage.setItem('price-loader-session-present', '1'));
    await page.goto('/review-queue');
    await expect(page.getByRole('heading', { name: 'Очередь ручной проверки' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Проверка' })).toHaveAttribute('aria-current', 'page');
    await expect(page.getByText('104').first()).toBeVisible();
    await expect(page.getByRole('region', { name: 'Исходная сетка Прайс' })).toBeVisible();
    await expect(page.getByRole('columnheader', { name: /A Наименование/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Проверить/ })).toBeVisible();

    const cards = page.locator('.review-queue-layout > .ant-card');
    const queueBox = await cards.nth(0).boundingBox();
    const workspaceBox = await cards.nth(1).boundingBox();
    expect(queueBox).not.toBeNull();
    expect(workspaceBox).not.toBeNull();
    if (viewport.name === 'desktop') {
      expect(workspaceBox!.x).toBeGreaterThan(queueBox!.x + queueBox!.width);
    } else {
      expect(workspaceBox!.y).toBeGreaterThanOrEqual(queueBox!.y + queueBox!.height);
    }
    await page.screenshot({ path: `/tmp/operator-review-${viewport.name}.png`, fullPage: true });
    expect(unmocked, 'клиент вызвал адрес, которого нет в моках').toEqual([]);
  });
}
