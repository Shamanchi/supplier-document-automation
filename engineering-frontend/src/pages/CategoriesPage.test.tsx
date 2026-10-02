import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { App as AntApp, ConfigProvider } from 'antd';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { GlobalCategorySummary } from '../api/types';
import CategoriesPage from './CategoriesPage';

const api = vi.hoisted(() => ({
  fetchCategoryDirectoryStats: vi.fn(),
  fetchCategorySuppliers: vi.fn(),
  fetchSupplierCategoryPage: vi.fn(),
  fetchGlobalCategoryRoots: vi.fn(),
  fetchGlobalCategoryChildren: vi.fn(),
  fetchGlobalCategoryPage: vi.fn(),
  fetchGlobalCategorySources: vi.fn(),
  fetchPriceVersionOriginal: vi.fn(),
  fetchPriceVersionPreview: vi.fn(),
}));

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  ...api,
}));
vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({ isAdmin: false }) }));

const category = (
  id: string,
  name: string,
  options: Partial<GlobalCategorySummary> = {},
): GlobalCategorySummary => ({
  id,
  parent_id: null,
  name,
  normalized_name: name.toLocaleLowerCase('ru-RU'),
  path: name,
  normalized_path: name.toLocaleLowerCase('ru-RU'),
  status: 'active',
  depth: 0,
  children_count: 0,
  direct_supplier_mappings_count: 0,
  supplier_mappings_count: 0,
  direct_product_count: 0,
  product_count: 0,
  ...options,
});

describe('CategoriesPage global tree', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchCategoryDirectoryStats.mockResolvedValue({
      supplier_categories_count: 3,
      mapped_count: 3,
      unmapped_count: 0,
      global_categories_count: 3,
    });
    api.fetchCategorySuppliers.mockResolvedValue([]);
    api.fetchSupplierCategoryPage.mockResolvedValue({ total: 0, items: [] });
    api.fetchGlobalCategoryPage.mockResolvedValue({ total: 0, items: [] });
    api.fetchGlobalCategoryRoots.mockResolvedValue({
      total: 2,
      items: [
        category('root-1', 'Вентиляционное оборудование', {
          children_count: 1,
          supplier_mappings_count: 1,
          product_count: 6,
        }),
        category('root-2', 'Пожарное оборудование'),
      ],
    });
    api.fetchGlobalCategoryChildren.mockResolvedValue({
      total: 1,
      items: [category('child-1', 'Фильтры', {
        parent_id: 'root-1',
        path: 'Вентиляционное оборудование / Фильтры',
        normalized_path: 'вентиляционное оборудование / фильтры',
        depth: 1,
        direct_supplier_mappings_count: 1,
        supplier_mappings_count: 1,
        direct_product_count: 6,
        product_count: 6,
      })],
    });
    api.fetchGlobalCategorySources.mockResolvedValue({
      global_category: category('root-1', 'Вентиляционное оборудование'),
      total: 1,
      items: [{
        supplier_category_id: 'source-1',
        supplier_id: 'supplier-1',
        supplier_code: '001-consol',
        supplier_name: '001_Consol',
        mapping_source: 'import',
        raw_name: 'Вентиляционное оборудование',
        raw_path: 'Каталог / Вентиляционное оборудование',
        normalized_path: 'каталог / вентиляционное оборудование',
        product_count: 6,
        first_seen: {
          id: 'version-1',
          version_no: 3,
          filename: '001_Consol.xlsx',
          imported_at: '2026-07-20T00:00:00Z',
        },
        last_seen: {
          id: 'version-1',
          version_no: 3,
          filename: '001_Consol.xlsx',
          imported_at: '2026-07-20T00:00:00Z',
        },
        aliases: [],
        item_samples: [{
          source_name: 'Фильтр кассетный',
          source_article: 'ФК-1',
          source_sheet: 'Прайс',
          source_row: 42,
          price: {
            id: 'version-1',
            version_no: 3,
            filename: '001_Consol.xlsx',
            imported_at: '2026-07-20T00:00:00Z',
          },
        }],
      }],
    });
    api.fetchPriceVersionOriginal.mockResolvedValue(new Blob(['price'], {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    }));
    api.fetchPriceVersionPreview.mockResolvedValue({
      version_id: 'version-1',
      filename: '001_Consol.xlsx',
      sheets: [
        { index: 0, name: 'Прайс', row_count: 2, column_count: 3 },
        { index: 1, name: 'Условия', row_count: 1, column_count: 2 },
      ],
      selected_sheet_index: 0,
      selected_sheet_name: 'Прайс',
      total_rows: 2,
      total_columns: 3,
      row_offset: 0,
      row_limit: 50,
      rows: [{
        row_index: 0,
        excel_row: 1,
        kind: 'source',
        cells: [
          { column_index: 0, excel_column: 'A', value: 'Артикул', is_bold: true, is_merged_fill: false },
          { column_index: 1, excel_column: 'B', value: 'Название', is_bold: true, is_merged_fill: false },
          { column_index: 2, excel_column: 'C', value: 'Цена', is_bold: true, is_merged_fill: false },
        ],
      }],
    });
  });

  it('loads only roots initially and fetches a branch once on expand', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    const createObjectURL = vi.fn(() => 'blob:price-file');
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
    // delay: null убирает искусственные паузы user-event между событиями, а
    // отключённая анимация antd — ожидание CSS-переходов Drawer и Modal: в
    // jsdom и то и другое стоит секунд, а проверяется здесь не анимация.
    const user = userEvent.setup({ delay: null });
    render(
      <QueryClientProvider client={queryClient}>
        <ConfigProvider theme={{ token: { motion: false } }}>
          <AntApp><CategoriesPage /></AntApp>
        </ConfigProvider>
      </QueryClientProvider>,
    );

    await user.click(await screen.findByRole('tab', { name: /Общий справочник/ }));
    expect(await screen.findByText('Вентиляционное оборудование')).toBeVisible();
    expect(screen.getByText('Пожарное оборудование')).toBeVisible();
    expect(api.fetchGlobalCategoryRoots).toHaveBeenCalledWith({
      includeDeprecated: false,
      limit: 50,
      offset: 0,
    });
    expect(api.fetchGlobalCategoryChildren).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Источник Вентиляционное оборудование' }));
    expect(await screen.findByText('Каталог / Вентиляционное оборудование')).toBeInTheDocument();
    expect(screen.getByText('001_Consol.xlsx · версия 3')).toBeInTheDocument();
    expect(screen.getByText(/Прайс!42: ФК-1/)).toBeInTheDocument();
    expect(api.fetchGlobalCategorySources).toHaveBeenCalledWith('root-1');
    await user.click(screen.getByRole('button', { name: 'Открыть прайс 001_Consol.xlsx' }));
    expect(await screen.findByText('Просмотр прайса «001_Consol.xlsx»')).toBeInTheDocument();
    expect(await screen.findByText('Артикул')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Лист прайса' })).toBeInTheDocument();
    expect(api.fetchPriceVersionPreview).toHaveBeenCalledWith('version-1', {
      sheetIndex: 0,
      rowOffset: 0,
      rowLimit: 50,
    });
    expect(api.fetchPriceVersionOriginal).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Скачать прайс 001_Consol.xlsx' }));
    await waitFor(() => expect(api.fetchPriceVersionOriginal).toHaveBeenCalledTimes(1));
    expect(anchorClick).toHaveBeenCalledTimes(1);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    const closeButtons = screen.getAllByRole('button', { name: 'Close' });
    await user.click(closeButtons[closeButtons.length - 1]);
    await user.click(screen.getByRole('button', { name: 'Close' }));

    await user.click(screen.getByRole('button', { name: 'Раскрыть Вентиляционное оборудование' }));
    expect(await screen.findByText('Фильтры')).toBeVisible();
    expect(api.fetchGlobalCategoryChildren).toHaveBeenCalledWith('root-1', {
      includeDeprecated: false,
      limit: 500,
      offset: 0,
    });

    await user.click(screen.getByRole('button', { name: 'Свернуть Вентиляционное оборудование' }));
    await user.click(screen.getByRole('button', { name: 'Раскрыть Вентиляционное оборудование' }));
    await waitFor(() => expect(api.fetchGlobalCategoryChildren).toHaveBeenCalledTimes(1));
    // Один сценарий прогоняет двенадцать взаимодействий с тяжёлой страницей
    // (вкладки, дерево, ящик источников, модалка просмотра прайса). В jsdom
    // это двадцать с лишним секунд, и при параллельном прогоне всего набора
    // сорока секунд переставало хватать — тест падал по таймауту, хотя ничего
    // не сломалось.
  }, 120_000);
});
