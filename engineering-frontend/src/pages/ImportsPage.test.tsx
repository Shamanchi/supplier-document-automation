import { App as AntApp } from 'antd';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ImportsPage from './ImportsPage';

const api = vi.hoisted(() => ({
  fetchImportedPriceVersions: vi.fn(),
  fetchPriceImports: vi.fn(),
  activatePriceVersion: vi.fn(),
}));

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  fetchImportedPriceVersions: api.fetchImportedPriceVersions,
  fetchPriceImports: api.fetchPriceImports,
  activatePriceVersion: api.activatePriceVersion,
}));

const auth = vi.hoisted(() => ({ isAdmin: false }));

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({ isAdmin: auth.isAdmin }),
}));

function version(overrides: Record<string, unknown> = {}) {
  return {
    id: 'version-3',
    supplier_id: 'supplier-1',
    supplier_external_id: null,
    supplier_name: 'Старый поставщик',
    version_no: 3,
    status: 'validated',
    document_type: 'PRICE_LIST',
    check_batch_id: null,
    loader: null,
    filename: 'legacy-price.xlsx',
    item_count: 42,
    warnings_count: 0,
    errors_count: 0,
    created_at: '2026-07-20T12:00:00Z',
    activated_at: null,
    import_id: null,
    ...overrides,
  };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <AntApp>
        <MemoryRouter initialEntries={['/']}>
          <Routes>
            <Route path="/" element={<ImportsPage />} />
            <Route path="/data" element={<div>Позиции выбранного прайса</div>} />
          </Routes>
        </MemoryRouter>
      </AntApp>
    </QueryClientProvider>,
  );
}

// Антд-таблицы в jsdom рендерятся медленно: каждый тест занимает ~5с,
// дефолтного таймаута vitest не хватает.
const SLOW_TEST_TIMEOUT = 30_000;

describe('ImportsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    auth.isAdmin = false;
    api.fetchImportedPriceVersions.mockResolvedValue({
      total: 1,
      items: [version()],
    });
    api.fetchPriceImports.mockResolvedValue({ total: 0, items: [] });
  });

  it('shows parsed price versions even when job history is empty and opens their positions', async () => {
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText('legacy-price.xlsx')).toBeVisible();
    expect(screen.getByText('Старый поставщик')).toBeVisible();
    expect(screen.getByText('42')).toBeVisible();
    expect(api.fetchPriceImports).not.toHaveBeenCalled();

    await user.click(screen.getByText('legacy-price.xlsx'));
    expect(await screen.findByText('Позиции выбранного прайса')).toBeVisible();
  }, SLOW_TEST_TIMEOUT);

  it('hides the activate action from non-admin users', async () => {
    renderPage();

    expect(await screen.findByText('legacy-price.xlsx')).toBeVisible();
    expect(screen.queryByText('Активировать')).not.toBeInTheDocument();
  }, SLOW_TEST_TIMEOUT);

  it('activates a validated price list on behalf of an admin', async () => {
    auth.isAdmin = true;
    api.activatePriceVersion.mockResolvedValue({ id: 'version-3', status: 'active' });
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText('требуется активация')).toBeVisible();
    // Открыть подтверждение и подтвердить: у кнопки-триггера и кнопки в
    // Popconfirm одинаковый текст, подтверждение — последняя в документе.
    await user.click(screen.getByRole('button', { name: 'Активировать' }));
    const buttons = screen.getAllByRole('button', { name: 'Активировать' });
    await user.click(buttons[buttons.length - 1]);

    await waitForApiCall();
    expect(api.activatePriceVersion).toHaveBeenCalledWith('version-3');
  }, SLOW_TEST_TIMEOUT);

  it('does not offer activation for supplier quotes and non-validated versions', async () => {
    auth.isAdmin = true;
    api.fetchImportedPriceVersions.mockResolvedValue({
      total: 2,
      items: [
        version({ id: 'quote-1', status: 'validated', document_type: 'SUPPLIER_QUOTE', filename: 'invoice.xlsx' }),
        version({ id: 'draft-1', status: 'validation_failed', filename: 'broken.xlsx' }),
      ],
    });
    renderPage();

    expect(await screen.findByText('invoice.xlsx')).toBeVisible();
    expect(screen.getByText(/счёт\/КП заявки/)).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Активировать' })).not.toBeInTheDocument();
    expect(screen.getByText('broken.xlsx')).toBeVisible();
  }, SLOW_TEST_TIMEOUT);
});

// Poll-до-появления вызова: Popconfirm закрывается асинхронно.
async function waitForApiCall() {
  for (let attempt = 0; attempt < 20 && api.activatePriceVersion.mock.calls.length === 0; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}
