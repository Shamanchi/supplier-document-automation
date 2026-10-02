import { App as AntApp } from 'antd';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import RulesPage from './RulesPage';
import RuleEditorPage from './RuleEditorPage';

const api = vi.hoisted(() => ({
  activateParseRule: vi.fn(),
  cloneParseRule: vi.fn(),
  deprecateParseRule: vi.fn(),
  extractApiError: vi.fn((error: unknown) => String(error)),
  fetchImportedPriceVersions: vi.fn(),
  fetchParseRule: vi.fn(),
  fetchParseRulePage: vi.fn(),
  fetchRuleProposalOptions: vi.fn(),
  fetchRuleProposals: vi.fn(),
  fetchRuleDraftProgress: vi.fn(),
  startRuleProposals: vi.fn(),
  createRuleDraftFromProposals: vi.fn(),
  updateParseRule: vi.fn(),
  validateParseRule: vi.fn(),
}));

vi.mock('../api/client', () => api);
vi.mock('../auth/AuthContext', () => ({ useAuth: () => ({ isAdmin: true }) }));

const rule = {
  id: 'rule-1', project_id: 'original', supplier_id: 'supplier-1', supplier_name: 'Поставщик правил',
  source_import_id: 'import-1', name: 'Правило XLSX', loader: 'xlsx', header_hash: 'a',
  fingerprint: {}, revision: 1, status: 'draft' as const, provenance: 'manual',
  created_from_version_id: 'version-1', validation: { passed: true },
  created_at: '2026-07-20T12:00:00Z', updated_at: '2026-07-20T12:00:00Z',
  usage: { price_versions: 2, database_items: 125, source_rows: 120 },
  recipe: {
    sheets: [{
      match: { name_pattern: 'Прайс' }, header_row: 0,
      columns: [
        { index: 0, header: 'Наименование', role: 'name' },
        { index: 1, header: 'Цена', role: 'price', price_descriptor: { kind: 'retail', vat_mode: 'included' } },
      ],
    }],
  },
};

describe('RulesPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchParseRulePage.mockResolvedValue({ total: 1, items: [rule] });
    api.fetchParseRule.mockResolvedValue(rule);
    api.fetchImportedPriceVersions.mockResolvedValue({ total: 0, items: [] });
    api.fetchRuleProposalOptions.mockResolvedValue({
      providers: [{
        provider: 'deepseek', label: 'DeepSeek', enabled: true,
        models: ['deepseek-chat'], default_model: 'deepseek-chat', supports_thinking: true,
      }],
      defaults: { provider: 'deepseek' },
    });
    api.fetchRuleProposals.mockResolvedValue([]);
    api.fetchRuleDraftProgress.mockResolvedValue({
      status: 'idle', phase: 'idle', percent: 0, completed_sections: 0, total_sections: 0,
      message: null, parse_rule_id: null, error_message: null, started_at: null, updated_at: null,
    });
    api.updateParseRule.mockResolvedValue({ ...rule, name: 'Правило XLSX v2', revision: 2 });
  });

  it('lists rules and opens a full-page structured editor', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/rules']}>
          <AntApp>
            <Routes>
              <Route path="/rules" element={<RulesPage />} />
              <Route path="/rules/:ruleId" element={<RuleEditorPage />} />
            </Routes>
          </AntApp>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(await screen.findByText('Правило XLSX')).toBeVisible();
    expect(screen.getByText('120')).toBeVisible();
    expect(screen.getByText('125')).toBeVisible();

    await user.click(screen.getByRole('button', { name: /Открыть/ }));

    // Full-page editor renders the structured rule form.
    expect(await screen.findByText('Структура правила')).toBeVisible();
    expect(screen.getByText('Табличный шаблон 1')).toBeVisible();
    expect(screen.getByDisplayValue('Наименование')).toBeVisible();
    expect(screen.getByDisplayValue('Цена')).toBeVisible();

    const nameInput = screen.getByLabelText('Название правила');
    fireEvent.change(nameInput, { target: { value: 'Правило XLSX v2' } });
    await user.click(screen.getByRole('button', { name: /Сохранить черновик/ }));

    expect(api.updateParseRule).toHaveBeenCalledWith('rule-1', {
      name: 'Правило XLSX v2',
      recipe: rule.recipe,
    });
  }, 25_000);

  it('starts rule creation from a source file and derives its metadata before model settings', async () => {
    const source = {
      id: 'version-source', supplier_id: 'supplier-1', supplier_external_id: null,
      supplier_name: 'Supplier F', version_no: 1, status: 'validated', loader: 'xlsx',
      filename: 'price_new.xlsx', item_count: 1385, warnings_count: 0, errors_count: 0,
      created_at: '2026-07-20T12:00:00Z', import_id: 'import-1',
    };
    api.fetchImportedPriceVersions.mockResolvedValue({ total: 1, items: [source] });
    api.startRuleProposals.mockResolvedValue({
      batch_id: 'batch-1', job_ids: ['job-1'], total_sections: 2, page_size: 24,
      pages_count: 1, attempts: [],
    });
    api.fetchRuleProposals.mockResolvedValue([
      { id: 'attempt-1', section_id: 'sheet:0:table:0', status: 'proposed', provider: 'deepseek', model: 'deepseek-chat' },
      { id: 'attempt-2', section_id: 'sheet:1:matrix:0', status: 'proposed', provider: 'deepseek', model: 'deepseek-chat' },
    ]);
    api.createRuleDraftFromProposals.mockResolvedValue({ ...rule, id: 'generated-rule' });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/rules']}>
          <AntApp><Routes><Route path="/rules" element={<RulesPage />} /><Route path="/rules/:ruleId" element={<div>Редактор созданного правила</div>} /></Routes></AntApp>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.queryByRole('button', { name: /Ручной JSON/ })).not.toBeInTheDocument();
    await user.click(await screen.findByRole('button', { name: /Создать правило/ }));
    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(within(dialog).getByText('1. Выберите исходный прайс')).toBeVisible());
    expect(within(dialog).queryByLabelText('LLM-провайдер')).not.toBeInTheDocument();
    expect(await within(dialog).findByText('price_new.xlsx')).toBeVisible();
    await user.click(within(dialog).getByRole('button', { name: 'Выбрать' }));
    expect(within(dialog).getByText('2. Подготовьте черновик')).toBeVisible();
    expect(within(dialog).getByText('Supplier F')).toBeVisible();
    expect(within(dialog).getByText('XLSX')).toBeVisible();
    expect(within(dialog).getByText('v1')).toBeVisible();
    expect(within(dialog).getByText('1 385')).toBeVisible();
    expect(within(dialog).getByRole('button', { name: /Выбрать другой файл/ })).toBeVisible();
    await user.click(within(dialog).getByRole('button', { name: /Запустить DeepSeek/ }));
    expect(api.startRuleProposals).toHaveBeenCalledWith('version-source', {
      provider: 'deepseek', model: 'deepseek-chat', thinking: 'disabled', reasoning_effort: 'high',
    });
    expect(await within(dialog).findByText('2 из 2 секций готовы')).toBeVisible();
    await user.click(within(dialog).getByRole('button', { name: /Создать черновик/ }));
    expect(api.createRuleDraftFromProposals).toHaveBeenCalledWith('version-source', 'batch-1');
    expect(await screen.findByText('Редактор созданного правила')).toBeVisible();
  }, 25_000);

  it('keeps analysis inside the viewport and shows completed sections first with readable metrics', async () => {
    const source = {
      id: 'version-source', supplier_id: 'supplier-1', supplier_external_id: null,
      supplier_name: 'Supplier F', version_no: 1, status: 'validated', loader: 'xlsx',
      filename: 'price_new.xlsx', item_count: 1385, warnings_count: 0, errors_count: 0,
      created_at: '2026-07-20T12:00:00Z', import_id: 'import-1',
    };
    api.fetchImportedPriceVersions.mockResolvedValue({ total: 1, items: [source] });
    api.startRuleProposals.mockResolvedValue({
      batch_id: 'batch-1', job_ids: ['job-1'], total_sections: 2, page_size: 24,
      pages_count: 1, attempts: [],
    });
    api.fetchRuleProposals.mockResolvedValue([
      {
        id: 'attempt-1', section_id: 'sec_queued_technical_id', status: 'queued', provider: 'deepseek', model: 'deepseek-chat',
        input_context: { sheet_name: 'ОВЭ ОВП ОПС ОП(г)', section_kind: 'price_rows', section_start_row_index: 5, section_end_row_index: 12 },
        updated_at: '2026-07-23T10:00:00Z',
      },
      {
        id: 'attempt-2', section_id: 'sec_finished_technical_id', status: 'proposed', provider: 'deepseek', model: 'deepseek-chat',
        input_context: { sheet_name: 'ОУ ОП', section_kind: 'price_rows', section_start_row_index: 6, section_end_row_index: 18 },
        validation: {
          model_usage: { total_tokens: 24500 },
          llm_telemetry_events: [{ duration_ms: 3800 }, { duration_ms: 1200 }],
        },
        updated_at: '2026-07-23T10:01:00Z',
      },
    ]);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/rules']}>
          <AntApp><Routes><Route path="/rules" element={<RulesPage />} /></Routes></AntApp>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await user.click(await screen.findByRole('button', { name: /Создать правило/ }));
    const dialog = await screen.findByRole('dialog');
    await user.click(await within(dialog).findByRole('button', { name: 'Выбрать' }));
    await user.click(within(dialog).getByRole('button', { name: /Запустить DeepSeek/ }));

    expect(await within(dialog).findByText('1 из 2 секций готовы')).toBeVisible();
    const attemptsRegion = within(dialog).getByTestId('rule-attempt-scroll');
    expect(attemptsRegion).toHaveStyle({ overflowY: 'auto' });
    expect(within(attemptsRegion).getByText('ОУ ОП')).toBeVisible();
    expect(within(attemptsRegion).getByText('Товарные строки · строки 7–19')).toBeVisible();
    expect(within(attemptsRegion).getByText('5,0 с')).toBeVisible();
    expect(within(attemptsRegion).getByText('24 500')).toBeVisible();
    expect(within(attemptsRegion).queryByText('sec_finished_technical_id')).not.toBeInTheDocument();
    expect(within(attemptsRegion).queryByText('Диагностика')).not.toBeInTheDocument();
    const dataRows = attemptsRegion.querySelectorAll('tr[data-row-key]');
    expect(dataRows[0]).toHaveTextContent('ОУ ОП');
    expect(dataRows[1]).toHaveTextContent('ОВЭ ОВП ОПС ОП(г)');
  }, 25_000);

  it('shows actual server-side progress while the verified sections are assembled into a draft', async () => {
    const source = {
      id: 'version-source', supplier_id: 'supplier-1', supplier_external_id: null,
      supplier_name: 'Supplier F', version_no: 1, status: 'validated', loader: 'xlsx',
      filename: 'price_new.xlsx', item_count: 1385, warnings_count: 0, errors_count: 0,
      created_at: '2026-07-20T12:00:00Z', import_id: 'import-1',
    };
    api.fetchImportedPriceVersions.mockResolvedValue({ total: 1, items: [source] });
    api.startRuleProposals.mockResolvedValue({
      batch_id: 'batch-1', job_ids: ['job-1'], total_sections: 52, page_size: 24,
      pages_count: 3, attempts: [],
    });
    api.fetchRuleProposals.mockResolvedValue(Array.from({ length: 52 }, (_, index) => ({
      id: `attempt-${index}`, section_id: `section-${index}`, status: 'proposed',
      provider: 'deepseek', model: 'deepseek-chat',
    })));
    api.fetchRuleDraftProgress.mockResolvedValue({
      status: 'running', phase: 'verifying_sections', percent: 46,
      completed_sections: 24, total_sections: 52,
      message: 'Проверяем привязку секций к исходному файлу', parse_rule_id: null,
      error_message: null, started_at: '2026-07-23T12:00:00Z', updated_at: '2026-07-23T12:00:10Z',
    });
    let finishDraft!: (value: typeof rule) => void;
    api.createRuleDraftFromProposals.mockReturnValue(new Promise((resolve) => { finishDraft = resolve; }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/rules']}>
          <AntApp><Routes><Route path="/rules" element={<RulesPage />} /><Route path="/rules/:ruleId" element={<div>Редактор созданного правила</div>} /></Routes></AntApp>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    await user.click(await screen.findByRole('button', { name: /Создать правило/ }));
    const dialog = await screen.findByRole('dialog');
    await user.click(await within(dialog).findByRole('button', { name: 'Выбрать' }));
    await user.click(within(dialog).getByRole('button', { name: /Запустить DeepSeek/ }));
    await user.click(await within(dialog).findByRole('button', { name: /Создать черновик/ }));

    expect(await within(dialog).findByText('Собираем черновик')).toBeVisible();
    expect(within(dialog).getByText('Проверяем привязку секций к исходному файлу')).toBeVisible();
    expect(within(dialog).getByText('24 из 52 секций')).toBeVisible();
    expect(within(dialog).getByText('46%')).toBeVisible();
    expect(api.fetchRuleDraftProgress).toHaveBeenCalledWith('version-source', 'batch-1');

    finishDraft({ ...rule, id: 'generated-rule' });
    expect(await screen.findByText('Редактор созданного правила')).toBeVisible();
  }, 25_000);
});
