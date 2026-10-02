import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { App } from 'antd';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import ApiAccessPage from './ApiAccessPage';

const api = vi.hoisted(() => ({
  fetchServiceClients: vi.fn(),
}));

vi.mock('../api/client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/client')>()),
  fetchServiceClients: api.fetchServiceClients,
}));

describe('ApiAccessPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchServiceClients.mockResolvedValue([]);
  });

  // nginx проксирует на backend только `/api/`, поэтому документация живёт под
  // этим префиксом; ссылка на `/docs` попадала в SPA-фолбэк, а не в Swagger.
  it('links to the served swagger and schema addresses', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <App>
          <MemoryRouter>
            <ApiAccessPage />
          </MemoryRouter>
        </App>
      </QueryClientProvider>,
    );

    const swagger = await screen.findByRole('link', { name: /Swagger UI/ });
    expect(swagger).toHaveAttribute('href', '/api/docs');
    expect(swagger).toHaveAttribute('target', '_blank');
    expect(screen.getByRole('link', { name: /OpenAPI JSON/ })).toHaveAttribute(
      'href',
      '/api/openapi.json',
    );
  });
});
