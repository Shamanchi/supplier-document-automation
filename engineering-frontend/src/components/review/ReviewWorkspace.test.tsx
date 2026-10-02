import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ReviewWorkspace from './ReviewWorkspace';

const api = vi.hoisted(() => ({
  fetchReview: vi.fn(),
  fetchSection: vi.fn(),
  updateDecisions: vi.fn(),
  updateMapping: vi.fn(),
  updateCurrency: vi.fn(),
  validate: vi.fn(),
  apply: vi.fn(),
  activate: vi.fn(),
}));

vi.mock('../../auth/AuthContext', () => ({ useAuth: () => ({ isAdmin: true }) }));
vi.mock('../../api/client', async () => {
  // Разбор конверта берём настоящий: именно он превращает отказ гарда
  // в список заблокированных ячеек.
  const actual = await vi.importActual<typeof import('../../api/client')>('../../api/client');
  return {
    extractApiError: actual.extractApiError,
    extractApiErrorEnvelope: actual.extractApiErrorEnvelope,
    extractApiErrorDetails: actual.extractApiErrorDetails,
    fetchPriceImportReview: api.fetchReview,
    fetchPriceImportReviewSection: api.fetchSection,
    updatePriceImportReviewDecisions: api.updateDecisions,
    updatePriceImportReviewMapping: api.updateMapping,
    updatePriceImportReviewCurrency: api.updateCurrency,
    validatePriceImportReview: api.validate,
    applyPriceImportReview: api.apply,
    activatePriceImportReviewRule: api.activate,
  };
});

const actions = { can_edit: true, can_validate: true, can_apply: true, can_activate: true };
const review = {
  import_id: 'import-1', review_id: 'review-1', revision: 1, status: 'validated',
  mapping: null, rule: null, section_decisions: [], currency_override: null,
  sections: [{ section_id: 'sec-1' }], attempts: [], validation: null, actions,
  updated_by: 'admin', updated_at: '2026-07-19T00:00:00Z',
};
const section = {
  import_id: 'import-1', review_id: 'review-1', section_id: 'sec-1', revision: 1,
  sheet_name: 'Прайс', section_kind: 'price_rows', header_row_index: 0,
  header_values: ['Наименование', 'Цена'], horizontal_panels: [], mapping: null,
  decisions: [], attempt: null, validation: null, actions,
  rows: [{ row_index: 1, excel_row: 2, kind: 'source', cells: [
    { column_index: 0, excel_column: 'A', value: 'Товар', is_bold: false, is_merged_fill: false },
    { column_index: 1, excel_column: 'B', value: 100, is_bold: false, is_merged_fill: false },
  ] }],
};

/** Отказ гарда в том виде, в каком его отдаёт контур Фазы 3. */
function guardRefusal() {
  return {
    isAxiosError: true,
    message: 'Request failed with status code 422',
    response: {
      status: 422,
      data: {
        error: {
          code: 'UNSAFE_NON_PRICE_DECISION',
          message: 'Решение скрывает строку с ценовым признаком',
          details: {
            sheet: 'Прайс',
            row: 1,
            column: 3,
            value: '7707083893',
            evidence: [{ sheet: 'Прайс', row: 1, column: 3, value: '7707083893' }],
          },
        },
      },
    },
  };
}

function renderWorkspace() {  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}><ReviewWorkspace importId="import-1" sectionId="sec-1" /></QueryClientProvider>);
}

describe('ReviewWorkspace', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchReview.mockResolvedValue(review);
    api.fetchSection.mockResolvedValue(section);
    api.updateDecisions.mockResolvedValue(review);
    api.updateMapping.mockResolvedValue(review);
    api.updateCurrency.mockResolvedValue({ ...review, revision: 2, currency_override: 'RUB' });
    api.validate.mockResolvedValue({ passed: true });
    api.apply.mockResolvedValue({ status: 'applying' });
    api.activate.mockResolvedValue({ status: 'active' });
  });

  it('runs validate/apply actions and saves a rejected row decision', async () => {
    const user = userEvent.setup();
    renderWorkspace();
    expect(await screen.findByText('Прайс')).toBeVisible();
    await user.click(screen.getByRole('button', { name: /Проверить/ }));
    await waitFor(() => expect(api.validate).toHaveBeenCalledWith('import-1', 1));
    await user.click(screen.getByRole('button', { name: /Применить/ }));
    await waitFor(() => expect(api.apply).toHaveBeenCalledWith('import-1', 1));
    await user.click(screen.getByRole('button', { name: 'Отклонить строку 2' }));
    const comment = await screen.findByPlaceholderText('Почему эта строка не должна создавать позицию');
    fireEvent.change(comment, { target: { value: 'Это техническое примечание' } });
    await user.click(screen.getByRole('button', { name: 'Сохранить решение' }));
    await waitFor(() => expect(api.updateDecisions).toHaveBeenCalled());
  }, 15_000);

  it('lets an operator set a workbook currency before validation', async () => {
    const user = userEvent.setup();
    renderWorkspace();
    expect(await screen.findByText('Валюта денежных значений')).toBeVisible();
    await user.click(screen.getByRole('combobox', { name: 'Валюта прайса' }));
    await user.click(await screen.findByText('RUB — российский рубль'));
    await user.click(screen.getByRole('button', { name: /Сохранить валюту/ }));
    await waitFor(() => expect(api.updateCurrency).toHaveBeenCalledWith('import-1', 1, 'RUB'));
  });

  it('turns a guard refusal into an explicit, reasoned override', async () => {
    const user = userEvent.setup();
    api.updateDecisions
      .mockRejectedValueOnce(guardRefusal())
      .mockResolvedValueOnce(review);
    renderWorkspace();
    await user.click(await screen.findByRole('button', { name: 'Отклонить строку 2' }));
    const comment = await screen.findByPlaceholderText('Почему эта строка не должна создавать позицию');
    fireEvent.change(comment, { target: { value: 'Шапка счёта: реквизиты поставщика' } });
    await user.click(screen.getByRole('button', { name: 'Сохранить решение' }));

    // Отказ гарда показывается координатами, а не «Ошибкой запроса».
    expect(await screen.findByText('Решение скрывает ценовой признак')).toBeVisible();
    expect(screen.getByText(/строка 1, колонка 3: 7707083893/)).toBeVisible();
    // Причина по умолчанию («Проверено оператором») обход не подтверждает.
    const overrideButton = screen.getByRole('button', { name: 'Обойти защиту и отклонить' });
    expect(overrideButton).toBeDisabled();

    await user.click(screen.getByRole('combobox', { name: 'Код причины' }));
    await user.click(await screen.findByText('Карточка поставщика'));
    await waitFor(() => expect(overrideButton).toBeEnabled());
    await user.click(overrideButton);

    await waitFor(() => expect(api.updateDecisions).toHaveBeenCalledTimes(2));
    const [, , decisions] = api.updateDecisions.mock.calls[1];
    expect(decisions[0]).toMatchObject({
      sheet: 'Прайс',
      row_start: 2,
      reason_code: 'supplier_card',
      override_guard: true,
    });
  }, 60_000);
});
