import { App as AntApp } from 'antd';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import RuleEditorPage from './RuleEditorPage';

const api = vi.hoisted(() => ({
  activateParseRule: vi.fn(),
  autoDraftParseRule: vi.fn(),
  cloneParseRule: vi.fn(),
  deprecateParseRule: vi.fn(),
  exportParseRuleValidation: vi.fn(),
  extractApiError: vi.fn((error: unknown) => String(error)),
  fetchImportedPriceVersions: vi.fn(),
  fetchRuleMatrixPreview: vi.fn(),
  fetchParseRule: vi.fn(),
  rebindParseRuleMatrix: vi.fn(),
  updateParseRule: vi.fn(),
  validateParseRule: vi.fn(),
}));

vi.mock('../api/client', () => api);
vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({ isAdmin: true }) }));

const rule = {
  id: 'rule-1', project_id: 'original', supplier_id: 'supplier-1',
  supplier_name: 'Supplier F', source_import_id: 'import-1',
  name: 'Review: price_new.xlsx', loader: 'xlsx', header_hash: 'a',
  fingerprint: {}, recipe: { sheets: [] }, revision: 3, status: 'draft' as const,
  provenance: 'manual', created_from_version_id: 'version-1',
  source_version: {
    id: 'version-1', supplier_id: 'supplier-1', supplier_external_id: null,
    supplier_name: 'Supplier F', version_no: 1, status: 'validated', loader: 'xlsx',
    filename: 'price_list_sample.xlsx', item_count: 1385,
    warnings_count: 1, errors_count: 0, created_at: '2026-07-20T12:00:00Z', import_id: 'import-1',
  },
  validation: null, created_at: '2026-07-20T12:00:00Z', updated_at: '2026-07-20T12:00:00Z',
  usage: { price_versions: 1, database_items: 1385, source_rows: 894 },
};

describe('RuleEditorPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchParseRule.mockResolvedValue(rule);
    api.fetchImportedPriceVersions.mockResolvedValue({ total: 0, items: [] });
    api.fetchRuleMatrixPreview.mockResolvedValue({
      source_section_id: 'matrix-1', sheet_index: 2, sheet_name: 'Матрица',
      row_offset: 13, total_rows: 2, total_columns: 2,
      rows: [
        { row_index: 13, excel_row: 14, cells: [
          { column_index: 0, excel_column: 'A', value: 'Наименование' },
          { column_index: 1, excel_column: 'B', value: '100' },
        ] },
        { row_index: 14, excel_row: 15, cells: [
          { column_index: 0, excel_column: 'A', value: 'Тройник' },
          { column_index: 1, excel_column: 'B', value: '6469' },
        ] },
      ],
      total_items: 1,
      items: [{
        source_sheet: 'Матрица', source_row: 15, name: 'Тройник', article: 'ТФ', unit: null,
        category: 'Гидранты', attributes: [{ code: 'diameter', name: 'Диаметр', raw_value: '100', unit: 'mm' }],
        prices: [{ kind: 'other', amount: '6469.00', is_on_request: false, vat_mode: 'unknown', currency: 'RUB', source_column_index: 1 }],
      }],
    });
    api.updateParseRule.mockResolvedValue(rule);
  });

  const renderEditor = () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/rules/rule-1']}>
          <AntApp>
            <Routes>
              <Route path="/rules/:ruleId" element={<RuleEditorPage />} />
            </Routes>
          </AntApp>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  };

  it('shows supplier, usage and offers to reconstruct an empty recipe from the source file', async () => {
    api.autoDraftParseRule.mockResolvedValue({
      recipe: {
        sheets: [{
          match: { name_pattern: 'Прайс' }, header_row: 0,
          columns: [
            { index: 0, role: 'name', header: 'наименование' },
            { index: 1, role: 'price', header: 'цена', price_descriptor: { kind: 'retail', vat_mode: 'unknown' } },
          ],
        }],
        options: {},
      },
      metrics: { rows_accepted: 1385 },
      passed: true,
      sheet_count: 1,
    });
    const user = userEvent.setup();
    renderEditor();

    expect((await screen.findAllByText('Supplier F')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('1 385').length).toBeGreaterThan(0);
    expect(screen.getByText('В правиле нет листов')).toBeVisible();

    await user.click(screen.getByRole('button', { name: /Восстановить из файла/ }));

    expect(api.autoDraftParseRule).toHaveBeenCalledWith('rule-1');
    expect(await screen.findByDisplayValue('наименование')).toBeVisible();
    expect(screen.getByText(/Автоподбор: листов — 1/)).toBeVisible();
  }, 15_000);

  it('preselects the source file and validates it without a combobox', async () => {
    api.validateParseRule.mockResolvedValue({
      at: '2026-07-22T12:00:00Z', price_version_id: 'version-1', passed: true,
      metrics: { rows_accepted: 1385, blocking_errors: 0 },
      samples: [{
        source_sheet: 'Подст под гидрант', source_row: 23,
        name: 'Тройник фланцевый ТФ', article: 'ТФ', unit: null,
        category: 'Тройник фланцевый ТФ',
        attributes: [
          { code: 'outlet_diameter', name: 'Диаметр отвода', raw_value: '100', unit: 'mm' },
          { code: 'main_diameter', name: 'Магистральный диаметр', raw_value: '250', unit: 'mm' },
        ],
        prices: [{
          kind: 'other', amount: '6469.00', is_on_request: false,
          vat_mode: 'unknown', currency: 'RUB', source_column_index: 15,
        }],
      }],
    });
    renderEditor();

    expect(await screen.findByText('Исходный прайс правила')).toBeVisible();
    expect(screen.getAllByText('price_list_sample.xlsx').length).toBeGreaterThan(0);
    expect(screen.getAllByText('XLSX').length).toBeGreaterThan(0);
    expect(screen.queryByPlaceholderText('Выберите версию прайса для проверки')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /Выполнить dry-run/ }));
    expect(api.validateParseRule).toHaveBeenCalledWith('rule-1', 'version-1');
    expect(await screen.findByText('Примеры получившихся позиций')).toBeVisible();
    expect(screen.getByText('Тройник фланцевый ТФ')).toBeVisible();
    expect(screen.getByText(/Диаметр отвода: 100 mm/)).toBeVisible();
    expect(screen.getByText(/Магистральный диаметр: 250 mm/)).toBeVisible();
    expect(screen.getByText(/6469.00 RUB/)).toBeVisible();
    expect(screen.getByRole('button', { name: /Скачать все позиции в JSON/ })).toBeVisible();
    expect(screen.getByRole('button', { name: /Скачать проверочный XLSX/ })).toBeVisible();
    expect(screen.getByText(/категории и все позиции на одном листе; ширина колонок подбирается автоматически/i)).toBeVisible();
  }, 15_000);

  it('searches the paginated price list by filename on the server', async () => {
    api.fetchImportedPriceVersions.mockImplementation(async (params: { query?: string }) => ({
      total: params.query === 'огнетушители' ? 1 : 1000,
      items: params.query === 'огнетушители' ? [{
        id: 'version-2', supplier_id: 'supplier-1', supplier_external_id: null,
        supplier_name: 'Supplier F', version_no: 4, status: 'validated', loader: 'xlsx',
        filename: 'Огнетушители июль.xlsx', item_count: 173,
        warnings_count: 0, errors_count: 0, created_at: '2026-07-21T12:00:00Z', import_id: 'import-2',
      }] : [],
    }));
    const user = userEvent.setup();
    renderEditor();

    const search = await screen.findByPlaceholderText('Имя файла, поставщик или код');
    expect(await screen.findByText('1 000 прайсов')).toBeVisible();
    await user.type(search, 'огнетушители');
    await user.click(screen.getByRole('button', { name: 'Найти' }));

    await waitFor(() => expect(api.fetchImportedPriceVersions).toHaveBeenLastCalledWith({
      query: 'огнетушители', supplierId: 'supplier-1', hasItems: true, limit: 10, offset: 0,
    }));
    expect(await screen.findByText('Огнетушители июль.xlsx')).toBeVisible();
    expect((screen.getAllByText('Supplier F')).length).toBeGreaterThan(0);
  }, 15_000);

  it('groups identical sections and hides ignored columns by default', async () => {
    const groupedRule = {
      ...rule,
      recipe: {
        sheets: [
          {
            match: { name_pattern: 'Секция 1' }, header_row: 0,
            default_category_path: 'Секция 1',
            columns: [
              { index: 0, role: 'name', header: 'наименование' },
              { index: 1, role: 'ignore' },
              { index: 2, role: 'price', header: 'цена' },
              { index: 3, role: 'ignore' },
            ],
          },
          {
            match: { name_pattern: 'Секция 2' }, header_row: 0,
            default_category_path: 'Секция 2',
            columns: [
              { index: 0, role: 'name', header: 'наименование' },
              { index: 1, role: 'ignore' },
              { index: 2, role: 'price', header: 'цена' },
              { index: 3, role: 'ignore' },
              { index: 4, role: 'ignore' },
            ],
          },
        ],
      },
    };
    api.fetchParseRule.mockResolvedValue(groupedRule);
    const user = userEvent.setup();
    renderEditor();

    // Two identical sheets collapse into one group card.
    expect(await screen.findByText('Секции ×2')).toBeVisible();
    expect(screen.getByText('одинаковая структура')).toBeVisible();
    expect(screen.getAllByText('Секция 1').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Секция 2').length).toBeGreaterThan(0);
    expect(screen.queryByText('Лист / секция 1')).not.toBeInTheDocument();
    expect(screen.getAllByRole('checkbox', { name: /категория из имени листа/i })).toHaveLength(2);
    screen.getAllByRole('checkbox', { name: /категория из имени листа/i }).forEach((checkbox) => {
      expect(checkbox).toBeChecked();
    });
    expect(screen.getByText('Секция 1 → Секция 1')).toBeVisible();
    expect(screen.getByText('Секция 2 → Секция 2')).toBeVisible();

    // The ignore-role column is hidden until the toggle is switched on.
    expect(screen.getByText(/Скрыто игнорируемых колонок: 1/)).toBeVisible();
    expect(screen.queryByText('B')).not.toBeInTheDocument();
    await user.click(screen.getByRole('checkbox', { name: /игнорируемые колонки/ }));
    expect(screen.getAllByText('B').length).toBeGreaterThan(0);
  }, 15_000);

  it('shows repeated name columns as independent horizontal product panels', async () => {
    api.fetchParseRule.mockResolvedValue({
      ...rule,
      recipe: {
        sheets: [{
          match: { name_pattern: 'ЗапЧ к огнетуш' }, header_row: 3,
          columns: [
            { index: 0, role: 'name', header: 'наименование продукции' },
            { index: 1, role: 'unit', header: 'изм' },
            { index: 2, role: 'attribute', header: 'упак' },
            { index: 3, role: 'price', header: 'цена опт' },
            { index: 4, role: 'price', header: 'цена розница' },
            { index: 5, role: 'ignore' },
            { index: 6, role: 'name', header: 'наименование продукции' },
            { index: 7, role: 'unit', header: 'изм' },
            { index: 8, role: 'attribute', header: 'упак' },
            { index: 9, role: 'price', header: 'цена опт' },
            { index: 10, role: 'price', header: 'цена розница' },
          ],
        }],
      },
    });
    renderEditor();

    expect(await screen.findByText('Панель 1 · A:E')).toBeVisible();
    expect(screen.queryByText('2 независимые товарные панели')).not.toBeInTheDocument();
    expect(screen.getByText('A → цены D, E')).toBeVisible();
    expect(screen.getByText('Панель 2 · G:K')).toBeVisible();
    expect(screen.getByText('G → цены J, K')).toBeVisible();
    expect(screen.getByText(/F — разделитель/)).toBeVisible();
    expect(screen.queryByText(/одна строка листа создаёт до 2 независимых позиций/i)).not.toBeInTheDocument();
  }, 15_000);

  it('does not offer activation when dry-run loses source items', async () => {
    api.fetchParseRule.mockResolvedValue({
      ...rule,
      validation: {
        at: '2026-07-22T12:00:00Z', price_version_id: 'version-1', passed: true,
        metrics: { rows_accepted: 1324, items_with_price: 1324 },
      },
    });
    renderEditor();

    expect(await screen.findByText(/Dry-run разобрал 1 324 из 1 385 позиций/)).toBeVisible();
    expect(screen.getByText(/Потеряно 61/)).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Активировать' })).not.toBeInTheDocument();
  }, 15_000);

  it('groups the same column mapping while keeping each range and category explicit', async () => {
    api.fetchParseRule.mockResolvedValue({
      ...rule,
      recipe: {
        sheets: [
          {
            match: { name_pattern: 'Лист А' }, header_row: 2, start_row: 3, end_row: 15,
            default_category_path: 'Каталог / А',
            columns: [
              { index: 0, role: 'name', header: 'наименование' },
              { index: 1, role: 'price', header: 'цена' },
            ],
          },
          {
            match: { name_pattern: 'Лист Б' }, header_row: 20, start_row: 21, end_row: 40,
            default_category_path: 'Каталог / Б',
            columns: [
              { index: 0, role: 'name', header: 'наименование' },
              { index: 1, role: 'price', header: 'цена' },
            ],
          },
        ],
      },
    });
    renderEditor();

    expect(await screen.findByText('Секции ×2')).toBeVisible();
    expect(screen.getByRole('region', { name: 'Индивидуальные настройки секций группы' })).toBeVisible();
    expect(screen.getByDisplayValue('Каталог / А')).toBeVisible();
    expect(screen.getByDisplayValue('Каталог / Б')).toBeVisible();
    expect(screen.getByDisplayValue('15')).toBeVisible();
    expect(screen.getByDisplayValue('40')).toBeVisible();
  }, 15_000);

  it('keeps the grouped full column editor for ordinary workbook sections', async () => {
    api.fetchParseRule.mockResolvedValue({
      ...rule,
      validation: {
        at: '2026-07-22T12:00:00Z', price_version_id: 'version-1', passed: true,
        metrics: { rows_accepted: 1385, items_with_price: 1385, source_items: 1385, blocking_errors: 0 },
        samples: [
          {
            source_sheet: 'Подст под гидрант', source_row: 10,
            name: 'Пожарная подставка фланцевая ППФ', article: null, unit: null,
            category: 'Подставки под гидрант', attributes: [],
            prices: [{ kind: 'other', amount: '4766.00', is_on_request: false, vat_mode: 'unknown', currency: 'RUB', source_column_index: 3 }],
          },
          {
            source_sheet: 'Обслуживание', source_row: 5,
            name: 'Станция зарядная', article: null, unit: null,
            category: 'Обслуживание', attributes: [],
            prices: [{ kind: 'other', amount: '453718.00', is_on_request: false, vat_mode: 'unknown', currency: 'RUB', source_column_index: 7 }],
          },
        ],
      },
      recipe: {
        format: 'workbook_layout_v1',
        name: 'Прайс — параллельные панели и матрицы',
        profile: 'price_new_v1',
        ordinary_exclusions: ['Подст под гидрант', 'Обслуживание'],
        sheets: [
          {
            match: { name_pattern: 'ЗапЧ к огнетуш', or_index: 4 },
            header_row: 3,
            default_category_source: 'sheet_name',
            columns: [
              { index: 0, role: 'name', header: 'наименование продукции' },
              { index: 1, role: 'unit', header: 'изм' },
              { index: 2, role: 'attribute', header: 'упак' },
              { index: 3, role: 'price', header: 'цена опт', price_descriptor: { kind: 'wholesale', vat_mode: 'included' } },
              { index: 4, role: 'price', header: 'цена розница', price_descriptor: { kind: 'retail', vat_mode: 'included' } },
              { index: 5, role: 'ignore' },
              { index: 6, role: 'name', header: 'наименование продукции' },
              { index: 7, role: 'unit', header: 'изм' },
              { index: 8, role: 'attribute', header: 'упак' },
              { index: 9, role: 'price', header: 'цена опт', price_descriptor: { kind: 'wholesale', vat_mode: 'included' } },
              { index: 10, role: 'price', header: 'цена розница', price_descriptor: { kind: 'retail', vat_mode: 'included' } },
            ],
          },
          {
            match: { name_pattern: 'Такая же секция', or_index: 5 },
            header_row: 3,
            default_category_source: 'sheet_name',
            columns: [
              { index: 0, role: 'name', header: 'наименование продукции' },
              { index: 1, role: 'unit', header: 'изм' },
              { index: 2, role: 'attribute', header: 'упак' },
              { index: 3, role: 'price', header: 'цена опт', price_descriptor: { kind: 'wholesale', vat_mode: 'included' } },
              { index: 4, role: 'price', header: 'цена розница', price_descriptor: { kind: 'retail', vat_mode: 'included' } },
              { index: 5, role: 'ignore' },
              { index: 6, role: 'name', header: 'наименование продукции' },
              { index: 7, role: 'unit', header: 'изм' },
              { index: 8, role: 'attribute', header: 'упак' },
              { index: 9, role: 'price', header: 'цена опт', price_descriptor: { kind: 'wholesale', vat_mode: 'included' } },
              { index: 10, role: 'price', header: 'цена розница', price_descriptor: { kind: 'retail', vat_mode: 'included' } },
            ],
          },
        ],
        ordinary_sheets: [
          {
            sheet: 'ЗапЧ к огнетуш',
            sections: [{
              header_row: 3,
              panels: [
                { name: 0, unit: 1, prices: [3, 4] },
                { name: 6, unit: 7, prices: [9, 10] },
              ],
            }],
          },
          {
            sheet: 'Рукава ПТ',
            sections: [{
              header_row: 3,
              panels: [
                { name: 0, unit: 1, prices: [2, 3] },
                { name: 5, unit: 6, prices: [7, 8] },
              ],
            }],
          },
        ],
        hydrant_matrix: {
          sheet: 'Подст под гидрант',
          name: 2,
          unit: 6,
          outlet: 9,
          price_columns: [4, 5, 6, 11, 13, 14, 15, 16, 17, 18, 21, 23],
          simple_panel: { name: 2, unit: 6, prices: [8, 10] },
        },
        service_matrix: {
          sheet: 'Обслуживание',
          linear: { name: 0, unit: 6, price: 7 },
          sign: { name: 0, price_columns: [1, 2, 3, 4, 5] },
        },
      },
    });
    renderEditor();

    expect(await screen.findByText('Секции ×2')).toBeVisible();
    expect(screen.getByText('одинаковая структура')).toBeVisible();
    expect(screen.getAllByText('ЗапЧ к огнетуш').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Такая же секция').length).toBeGreaterThan(0);
    expect(screen.getByText('Панель 1 · A:E')).toBeVisible();
    expect(screen.getByText('A → цены D, E')).toBeVisible();
    expect(screen.getByText('Панель 2 · G:K')).toBeVisible();
    expect(screen.getByText('G → цены J, K')).toBeVisible();
    expect(screen.getAllByText('Тип цены / НДС').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Оптовая').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Розничная').length).toBeGreaterThan(0);
    expect(screen.getAllByText('С НДС').length).toBeGreaterThan(0);
    expect(screen.getByText('Специальные форматы листов')).toBeVisible();
    expect(
      screen.getByText('Секции ×2').compareDocumentPosition(screen.getByText('Специальные форматы листов'))
      & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(screen.getByText('Подставки под гидрант')).toBeVisible();
    expect(screen.getByText('Подст под гидрант')).toBeVisible();
    expect(screen.getAllByText('Что получится после разбора').length).toBe(2);
    expect(screen.getByText(/основная матрица и нижняя обычная таблица разбираются независимо/i)).toBeVisible();
    expect(screen.getByText('Составной лист: две независимые таблицы')).toBeVisible();
    expect(screen.getByText(/Это не матрица: каждая строка даёт позицию/)).toBeVisible();
    expect(screen.getByText(/два независимых набора позиций/i)).toBeVisible();
    const technicalSettings = screen.getAllByText('Техническая настройка колонок');
    expect(technicalSettings.length).toBe(2);
    expect(screen.queryByLabelText('Наименование матрицы гидрантов')).not.toBeInTheDocument();
    await userEvent.click(technicalSettings[0]);
    expect(screen.getAllByLabelText('Наименование матрицы гидрантов')[0]).toBeVisible();
    expect(screen.getAllByText('Пожарная подставка фланцевая ППФ').length).toBeGreaterThan(1);
    expect(screen.getAllByText(/4766.00 RUB/).length).toBeGreaterThan(1);
    const dryRunCard = screen.getByText('Проверка на прайсе (dry-run)').closest('.ant-card');
    const structureCard = screen.getByText('Структура правила').closest('.ant-card');
    if (!dryRunCard || !structureCard) throw new Error('Rule cards were not rendered');
    expect(structureCard.compareDocumentPosition(dryRunCard) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.queryByText('Все панели сохранены в правиле явно')).not.toBeInTheDocument();
    expect(screen.queryByText(/Каждая панель создаёт отдельную позицию/)).not.toBeInTheDocument();
    expect(screen.queryByText('В правиле нет листов')).not.toBeInTheDocument();

    fireEvent.change(screen.getAllByDisplayValue('цена опт')[0], { target: { value: 'цена дилер' } });
    fireEvent.click(screen.getByRole('button', { name: /Сохранить черновик/ }));
    await waitFor(() => expect(api.updateParseRule).toHaveBeenCalled());
    expect(api.updateParseRule.mock.calls[0][1].recipe.sheets[0].columns[3].header).toBe('цена дилер');
    expect(api.updateParseRule.mock.calls[0][1].recipe.sheets[1].columns[3].header).toBe('цена дилер');
  }, 30_000);

  it('explains generated matrix output and keeps source bindings available on demand', async () => {
    api.fetchParseRule.mockResolvedValue({
      ...rule,
      recipe: {
        format: 'workbook_layout_v1',
        profile: 'generated_matrix_v1',
        sheets: [],
        options: {},
        matrix_regions: [{
          format: 'matrix-unfold-v1',
          source_section_id: 'matrix-1',
          match: { name_pattern: 'Матрица', or_index: 2 },
          validated_measure_cells: 235,
          blocks: [{
            block_ref: 'b4',
            identity_mode: 'block',
            name: { row_index: 14, column_index: 2 },
            article: { row_index: 14, column_index: 6 },
            unit: null,
            category: { row_index: 13, column_index: 0 },
            row_axis_source: { row_index: 15, column_index: 9 },
            row_axis: { attribute_code: 'outlet_diameter', display_name: 'Диаметр отвода', canonical_unit: 'mm' },
            column_axis_row_index: 14,
            column_axis_columns: [13, 14, 15, 16],
            column_axis: { attribute_code: 'main_diameter', display_name: 'Магистральный диаметр', canonical_unit: 'mm' },
            data_row_indexes: [15, 16, 17],
            price_descriptor: { kind: 'other', vat_mode: 'unknown' },
          }],
        }],
      },
    });
    api.rebindParseRuleMatrix.mockResolvedValue({
      recipe: {
        format: 'workbook_layout_v1', profile: 'generated_matrix_v1', sheets: [], options: {},
        matrix_regions: [{ source_section_id: 'matrix-1', structure_signature: { version: 'matrix-structure-v1' } }],
      },
      metrics: { rows_accepted: 236 }, passed: true, sheet_count: 1,
    });
    renderEditor();

    expect(await screen.findByText('Специальные форматы листов')).toBeVisible();
    expect(screen.getByText('Матричная таблица: ячейка цены → отдельная позиция')).toBeVisible();
    expect(screen.getByText('Лист «Матрица»')).toBeVisible();
    expect(screen.getByText('Проверено ценовых ячеек')).toBeVisible();
    expect(screen.getByText('235')).toBeVisible();
    expect(screen.getByText(/Каждая заполненная ценовая ячейка станет отдельной/)).toBeVisible();
    expect(screen.queryByText('Блок 1')).not.toBeInTheDocument();
    await userEvent.click(screen.getByText('Как связаны поля и оси'));
    expect(screen.getByText('Блок 1')).toBeVisible();
    expect(screen.getByText('колонка C')).toBeVisible();
    expect(screen.getByText(/колонка J, mm/)).toBeVisible();
    expect(screen.getByText(/заголовки N, O, P, Q, mm/)).toBeVisible();
    expect(screen.getByText('Техническая настройка колонок')).toBeVisible();
    await userEvent.click(screen.getByText('Техническая настройка колонок'));
    expect(screen.getAllByLabelText('Название блока 1')[0]).toBeVisible();
    expect(screen.getAllByLabelText('Ценовые колонки блока 1')[0]).toBeVisible();
    expect(screen.getByRole('button', { name: /Просмотреть матрицу и результат/ })).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: /Просмотреть матрицу и результат/ }));
    expect(await screen.findByRole('heading', { name: 'Исходная матрица' })).toBeVisible();
    expect(screen.getByRole('region', { name: 'Исходная матрица с заголовками' })).toHaveTextContent('6469');
    expect(screen.getByRole('heading', { name: 'Результат разбора' })).toBeVisible();
    expect(screen.getByText('1 позиций')).toBeVisible();
    expect(screen.getAllByText('Тройник').length).toBeGreaterThan(0);
    expect(api.fetchRuleMatrixPreview).toHaveBeenCalledWith('rule-1', expect.objectContaining({
      priceVersionId: 'version-1', sourceSectionId: 'matrix-1',
    }));
    await userEvent.click(screen.getByRole('button', { name: /Перепривязать матрицу/ }));
    expect(api.rebindParseRuleMatrix).toHaveBeenCalledWith(
      'rule-1', 'version-1', expect.objectContaining({ profile: 'generated_matrix_v1' }),
    );
    expect(screen.queryByText('В правиле нет листов')).not.toBeInTheDocument();
  }, 30_000);

  it('offers matrix-only rebind for a hybrid workbook profile', async () => {
    api.fetchParseRule.mockResolvedValue({
      ...rule,
      recipe: {
        format: 'workbook_layout_v1',
        profile: 'price_new_v1',
        sheets: [],
        hydrant_matrix: {},
        service_matrix: {},
        matrix_regions: [{
          format: 'matrix-unfold-v1',
          source_section_id: 'matrix-1',
          match: { name_pattern: 'Матрица', or_index: 2 },
          blocks: [{}],
        }],
      },
    });
    api.rebindParseRuleMatrix.mockResolvedValue({
      recipe: {
        format: 'workbook_layout_v1', profile: 'price_new_v1', sheets: [],
        hydrant_matrix: {}, service_matrix: {}, matrix_regions: [{ source_section_id: 'matrix-1' }],
      },
      metrics: { rows_accepted: 1 }, passed: true, sheet_count: 1,
    });
    renderEditor();

    await userEvent.click(await screen.findByRole('button', { name: /Перепривязать матрицу/ }));

    expect(api.rebindParseRuleMatrix).toHaveBeenCalledWith(
      'rule-1', 'version-1', expect.objectContaining({ profile: 'price_new_v1' }),
    );
  }, 15_000);

  it('summarizes generated multi-table sheets as a special format without hiding their editors', async () => {
    api.fetchParseRule.mockResolvedValue({
      ...rule,
      recipe: {
        format: 'workbook_layout_v1', profile: 'generated_matrix_v1', matrix_regions: [], options: {},
        sheets: [
          {
            match: { name_pattern: 'Обслуживание', or_index: 16 }, header_row: 3, start_row: 4, end_row: 30,
            source_section_id: 'service-linear',
            columns: [{ index: 0, role: 'name' }, { index: 7, role: 'price' }],
          },
          {
            match: { name_pattern: 'Обслуживание', or_index: 16 }, header_row: 54, start_row: 55, end_row: 80,
            source_section_id: 'service-signs',
            columns: [{ index: 0, role: 'name' }, { index: 1, role: 'price' }],
          },
        ],
      },
    });
    renderEditor();

    expect(await screen.findByText('Специальные форматы листов')).toBeVisible();
    expect(screen.getByText('Составной лист: несколько форматов таблиц')).toBeVisible();
    expect(screen.getAllByText('Обслуживание').length).toBeGreaterThan(0);
    expect(screen.getByText(/2 шаблона разбора/)).toBeVisible();
    expect(screen.getByText(/Табличный шаблон 1: одна широкая таблица/)).toBeVisible();
    expect(screen.getByText(/Табличный шаблон 2: одна широкая таблица/)).toBeVisible();
    expect(screen.getByText('Табличный шаблон 1')).toBeVisible();
    expect(screen.getByText('Табличный шаблон 2')).toBeVisible();
  }, 15_000);

  it('renders a repeating category->header->positions sheet as one template card', async () => {
    api.fetchParseRule.mockResolvedValue({
      ...rule,
      recipe: {
        sheets: [{
          match: { name_pattern: 'Прайс' }, header_row: null, repeat: true, repeat_sections: 20,
          columns: [
            { index: 0, role: 'name', header: 'наименование' },
            { index: 1, role: 'price', header: 'цена', price_descriptor: { kind: 'retail', vat_mode: 'unknown' } },
          ],
        }],
        options: {},
      },
    });
    renderEditor();

    expect(await screen.findByText('повторяющаяся секция ×20')).toBeVisible();
    expect(screen.getByText('авто — находится в каждой секции')).toBeVisible();
    expect(screen.getByText(/На листе повторяется связка/)).toBeVisible();
    // One template card: no fixed header input and no misleading "no header" tag.
    expect(screen.queryByText('без шапки')).not.toBeInTheDocument();
    expect(screen.getByDisplayValue('наименование')).toBeVisible();
  }, 15_000);
});
