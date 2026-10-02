import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ImportedDataPage from './ImportedDataPage';

const api = vi.hoisted(() => ({
  fetchImportedPriceItems: vi.fn(),
}));

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  fetchImportedPriceItems: api.fetchImportedPriceItems,
}));
vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({ isAdmin: false }) }));

describe('ImportedDataPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchImportedPriceItems.mockResolvedValue({
      total: 1,
      items: [
        {
          id: 'item-1',
          source_name: 'Огнетушитель ОП-5',
          normalized_name: 'огнетушитель оп 5',
          article: 'ОП-5',
          unit: 'шт',
          category_path: 'Пожарное оборудование',
          category: {
            source_path: 'Пожарное оборудование',
            supplier_category_id: null,
            supplier_normalized_path: null,
            global_category_id: null,
            global_path: null,
            mapping_status: 'unmapped',
            matching_identity: 'Огнетушитель ОП-5',
            matching_scope: null,
          },
          dimensions: null,
          description: null,
          attributes: [],
          source_sheet: 'Лист1',
          source_row: 7,
          source_section_id: null,
          source_panel_index: null,
          source_column_start: null,
          source_column_end: null,
          provenance: null,
          prices: [{
            kind: 'retail', amount: '1250.50', qualifier: 'exact', amount_from: null,
            amount_to: null, currency: 'RUB', currency_source: 'explicit', min_quantity: null,
            max_quantity: null,
            basis_quantity: null, basis_unit: null, raw_value: '1 250,50', source_column_index: 3,
            vat_mode: 'unknown', is_on_request: false, source_label: 'Цена',
          }],
          supplier_id: 'supplier-1',
          supplier_name: 'Старый импорт',
          price_version_id: 'version-1',
          version_no: 3,
          version_status: 'parsed',
          filename: 'legacy-price.xlsx',
          imported_at: '2026-07-20T12:00:00Z',
        },
      ],
    });
  });

  it('shows imported items independently of import-job history and supports search', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ImportedDataPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole('heading', { name: 'Импортированные данные' })).toBeVisible();
    expect(await screen.findByText('Огнетушитель ОП-5')).toBeVisible();
    expect(screen.getByText('Старый импорт')).toBeVisible();
    expect(screen.getByText('legacy-price.xlsx · версия 3')).toBeVisible();

    await user.type(screen.getByPlaceholderText('Товар, артикул, поставщик или файл'), 'ОП-5');
    await user.click(screen.getByRole('button', { name: 'Найти' }));

    expect(api.fetchImportedPriceItems).toHaveBeenLastCalledWith({
      query: 'ОП-5',
      priceVersionId: undefined,
      limit: 50,
      offset: 0,
    });
  });

  it('filters positions by the selected price version from the URL', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/data?version=version-1']}>
          <ImportedDataPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect((await screen.findAllByText('legacy-price.xlsx · версия 3'))[0]).toBeVisible();
    expect(api.fetchImportedPriceItems).toHaveBeenCalledWith({
      query: undefined,
      priceVersionId: 'version-1',
      limit: 50,
      offset: 0,
    });
  });
});
