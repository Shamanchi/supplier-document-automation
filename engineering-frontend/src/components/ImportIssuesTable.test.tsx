import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import ImportIssuesTable from './ImportIssuesTable';

const api = vi.hoisted(() => ({ fetchIssues: vi.fn() }));

vi.mock('../api/client', () => ({
  extractApiError: (error: unknown) => String(error),
  fetchPriceImportIssues: api.fetchIssues,
}));

describe('ImportIssuesTable', () => {
  it('shows raw evidence and routes a currency problem to review', async () => {
    api.fetchIssues.mockResolvedValue({
      import_id: 'import-1',
      status: 'review_required',
      total: 1,
      issues: [{
        id: 'issue-1',
        severity: 'error',
        code: 'PRICE_CURRENCY_UNRESOLVED',
        sheet: 'Гидранты',
        row_no: 6,
        column_ref: '3',
        source_value: '1245.888',
        message: 'Для денежного значения не доказана валюта',
      }],
    });
    const onOpenReview = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ImportIssuesTable importId="import-1" onOpenReview={onOpenReview} />
      </QueryClientProvider>,
    );

    expect(await screen.findByText('1245.888')).toBeVisible();
    expect(screen.getByText('лист «Гидранты», строка 6, колонка 3')).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Указать валюту' }));
    expect(onOpenReview).toHaveBeenCalledOnce();
  });
});
