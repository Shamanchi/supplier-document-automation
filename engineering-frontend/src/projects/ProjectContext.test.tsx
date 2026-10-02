import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ProjectProvider, useProject } from './ProjectContext';

const api = vi.hoisted(() => ({
  fetchProjects: vi.fn(),
  createProject: vi.fn(),
  setProjectId: vi.fn(),
  extractApiError: vi.fn((error: unknown) => String(error)),
}));

vi.mock('../api/client', () => api);

function Consumer() {
  const { project, selectProject } = useProject();
  return (
    <div>
      <span>{project.name}</span>
      <button onClick={() => selectProject('archive')}>Архив</button>
    </div>
  );
}

describe('ProjectProvider', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    api.fetchProjects.mockResolvedValue([
      { id: 'original', slug: 'original-prices', name: 'Оригинальные прайсы', is_default: true },
      { id: 'archive', slug: 'legacy-archive', name: 'Архив', is_default: false },
    ]);
  });

  it('selects the default project and switches the API boundary', async () => {
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ProjectProvider><Consumer /></ProjectProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText('Оригинальные прайсы')).toBeVisible();
    expect(api.setProjectId).toHaveBeenCalledWith('original');
    await user.click(screen.getByRole('button', { name: 'Архив' }));
    expect(await screen.findByText('Архив', { selector: 'span' })).toBeVisible();
    expect(api.setProjectId).toHaveBeenLastCalledWith('archive');
  });

  // Без обработки отказа провайдер оставался в состоянии «проекта нет» и
  // отдавал `null`: приложение показывало пустую страницу без причины.
  it('shows the reason and retries when the project list fails', async () => {
    const user = userEvent.setup();
    api.fetchProjects.mockRejectedValueOnce(new Error('Сервис недоступен'));
    render(
      <QueryClientProvider client={new QueryClient()}>
        <ProjectProvider><Consumer /></ProjectProvider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText('Не удалось загрузить список проектов')).toBeVisible();
    expect(screen.getByText(/Сервис недоступен/)).toBeVisible();

    await user.click(screen.getByRole('button', { name: 'Повторить' }));
    expect(await screen.findByText('Оригинальные прайсы')).toBeVisible();
  });
});
