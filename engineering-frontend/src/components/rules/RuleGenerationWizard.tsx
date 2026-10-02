import { CheckCircleOutlined, LeftOutlined, RobotOutlined, SearchOutlined } from '@ant-design/icons';
import { keepPreviousData, useMutation, useQuery } from '@tanstack/react-query';
import { Alert, App, Button, Descriptions, Input, Modal, Progress, Select, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useEffect, useMemo, useState } from 'react';
import {
  createRuleDraftFromProposals,
  extractApiError,
  fetchImportedPriceVersions,
  fetchRuleDraftProgress,
  fetchRuleProposalOptions,
  fetchRuleProposals,
  startRuleProposals,
} from '../../api/client';
import type { ImportedPriceVersion, ParseRule, RuleProposalAttempt } from '../../api/types';
import { formatDate } from '../importFormat';

const PAGE_SIZE = 10;
const RULE_GENERATION_LOADERS = new Set(['xlsx', 'xls', 'docx', 'doc', 'txt']);
const READY = new Set(['proposed', 'catalog_without_price', 'skipped_non_price']);
const TERMINAL = new Set([...READY, 'insufficient_evidence', 'rejected', 'failed', 'draft_created']);

const STATUS_LABELS: Record<string, string> = {
  queued: 'В очереди', generating: 'DeepSeek анализирует', proposed: 'Готово',
  catalog_without_price: 'Каталог без цены', skipped_non_price: 'Не товарная секция',
  insufficient_evidence: 'Недостаточно данных', rejected: 'Отклонено проверкой',
  failed: 'Ошибка', draft_created: 'Добавлено в черновик',
};

const SECTION_KIND_LABELS: Record<string, string> = {
  price_rows: 'Товарные строки',
  name_rows_without_price: 'Строки без цен',
  ambiguous_tabular_rows: 'Табличная секция',
  mixed_rows: 'Смешанная секция',
};

const TOKEN_FORMATTER = new Intl.NumberFormat('ru-RU');
const SECONDS_FORMATTER = new Intl.NumberFormat('ru-RU', {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
});

interface Props {
  open: boolean;
  onClose: () => void;
  onCreated: (rule: ParseRule) => void;
}

function detectedLoader(version: ImportedPriceVersion): string {
  return (version.loader || version.filename.split('.').pop() || '').toLowerCase();
}

function loaderLabel(version: ImportedPriceVersion): string {
  return detectedLoader(version).toUpperCase() || '—';
}

function supportsRuleGeneration(version: ImportedPriceVersion): boolean {
  return RULE_GENERATION_LOADERS.has(detectedLoader(version));
}

function sectionPresentation(attempt: RuleProposalAttempt): { title: string; detail: string } {
  const context = attempt.input_context ?? {};
  const title = context.sheet_name
    || (typeof context.sheet_index === 'number' ? `Лист ${context.sheet_index + 1}` : 'Секция файла');
  const isMatrix = attempt.section_id.startsWith('matrix_') || context.proposal_contract === 'matrix-unfold-v1';
  const kind = isMatrix ? 'Матричная секция' : SECTION_KIND_LABELS[context.section_kind ?? ''] ?? 'Табличная секция';
  const start = context.section_start_row_index ?? context.region_start_row_index;
  const end = context.section_end_row_index ?? context.region_end_row_index;
  if (typeof start !== 'number') return { title, detail: kind };
  const firstRow = start + 1;
  const lastRow = typeof end === 'number' ? end + 1 : firstRow;
  const range = firstRow === lastRow ? `строка ${firstRow}` : `строки ${firstRow}–${lastRow}`;
  return { title, detail: `${kind} · ${range}` };
}

function attemptDurationMs(attempt: RuleProposalAttempt): number | null {
  const events = attempt.validation?.llm_telemetry_events;
  if (Array.isArray(events) && events.length > 0) {
    return events.reduce((total, event) => total + (typeof event.duration_ms === 'number' ? event.duration_ms : 0), 0);
  }
  const duration = attempt.validation?.llm_telemetry?.duration_ms;
  return typeof duration === 'number' ? duration : null;
}

function attemptDurationLabel(attempt: RuleProposalAttempt): string {
  if (attempt.status === 'generating') return 'идёт';
  const duration = attemptDurationMs(attempt);
  return duration === null ? '—' : `${SECONDS_FORMATTER.format(duration / 1000)} с`;
}

function attemptTokensLabel(attempt: RuleProposalAttempt): string {
  const total = attempt.validation?.model_usage?.total_tokens;
  return typeof total === 'number' ? TOKEN_FORMATTER.format(total) : '—';
}

function attemptStatusRank(status: string): number {
  if (TERMINAL.has(status)) return 0;
  if (status === 'generating') return 1;
  return 2;
}

export default function RuleGenerationWizard({ open, onClose, onCreated }: Props) {
  const { message } = App.useApp();
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<ImportedPriceVersion>();
  const [provider, setProvider] = useState('');
  const [model, setModel] = useState('');
  const [batchId, setBatchId] = useState('');
  const [creatingDraft, setCreatingDraft] = useState(false);
  const [draftStartedAt, setDraftStartedAt] = useState<number>();
  const [draftElapsedSeconds, setDraftElapsedSeconds] = useState(0);

  const options = useQuery({
    queryKey: ['rule-proposal-options'],
    queryFn: fetchRuleProposalOptions,
    enabled: open,
  });
  const versions = useQuery({
    queryKey: ['rule-generation-versions', search, page],
    queryFn: () => fetchImportedPriceVersions({
      query: search || undefined, limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE,
    }),
    enabled: open && !batchId,
    placeholderData: keepPreviousData,
  });
  const selectedProvider = options.data?.providers.find((item) => item.provider === provider);

  useEffect(() => {
    if (provider || !options.data) return;
    const next = options.data.providers.find((item) => item.enabled && item.provider === 'deepseek')
      ?? options.data.providers.find((item) => item.enabled);
    if (next) {
      setProvider(next.provider);
      setModel(next.default_model);
    }
  }, [options.data, provider]);

  const attempts = useQuery({
    queryKey: ['rule-proposals', selected?.id, batchId],
    queryFn: () => fetchRuleProposals(selected!.id, batchId),
    enabled: Boolean(open && selected && batchId),
    refetchInterval: (query) => {
      const rows = query.state.data ?? [];
      return rows.length > 0 && rows.every((attempt) => TERMINAL.has(attempt.status)) ? false : 1500;
    },
  });
  const draftProgress = useQuery({
    queryKey: ['rule-draft-progress', selected?.id, batchId],
    queryFn: () => fetchRuleDraftProgress(selected!.id, batchId),
    enabled: Boolean(open && selected && batchId && creatingDraft),
    refetchInterval: creatingDraft ? 800 : false,
  });
  useEffect(() => {
    if (!creatingDraft || !draftStartedAt) return undefined;
    const update = () => setDraftElapsedSeconds(Math.max(0, Math.floor((Date.now() - draftStartedAt) / 1000)));
    update();
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, [creatingDraft, draftStartedAt]);
  const rows = attempts.data ?? [];
  const displayRows = useMemo(() => [...rows].sort((left, right) => {
    const statusDifference = attemptStatusRank(left.status) - attemptStatusRank(right.status);
    if (statusDifference !== 0) return statusDifference;
    const leftUpdated = left.updated_at ? Date.parse(left.updated_at) : 0;
    const rightUpdated = right.updated_at ? Date.parse(right.updated_at) : 0;
    return rightUpdated - leftUpdated;
  }), [rows]);
  const completed = rows.filter((attempt) => TERMINAL.has(attempt.status)).length;
  const allReady = rows.length > 0 && rows.every((attempt) => READY.has(attempt.status) || attempt.status === 'draft_created');
  const hasFailure = rows.some((attempt) => !READY.has(attempt.status) && TERMINAL.has(attempt.status) && attempt.status !== 'draft_created');

  const start = useMutation({
    mutationFn: () => startRuleProposals(selected!.id, {
      provider, model, thinking: 'disabled', reasoning_effort: 'high',
    }),
    onSuccess: (batch) => setBatchId(batch.batch_id),
    onError: (error) => void message.error(extractApiError(error)),
  });
  const createDraft = useMutation({
    mutationFn: () => createRuleDraftFromProposals(selected!.id, batchId),
    onMutate: () => {
      setCreatingDraft(true);
      setDraftStartedAt(Date.now());
      setDraftElapsedSeconds(0);
    },
    onSuccess: (rule) => {
      setCreatingDraft(false);
      void message.success('Черновик создан: откройте его, проверьте секции и выполните dry-run');
      setSearchInput(''); setSearch(''); setPage(1); setSelected(undefined); setBatchId('');
      onCreated(rule);
    },
    onError: (error) => {
      setCreatingDraft(false);
      void message.error(extractApiError(error));
    },
  });

  const resetAndClose = () => {
    if (creatingDraft) return;
    setSearchInput(''); setSearch(''); setPage(1); setSelected(undefined); setBatchId('');
    onClose();
  };
  const priceColumns = useMemo<ColumnsType<ImportedPriceVersion>>(() => [
    { title: 'Файл', dataIndex: 'filename', width: 300, render: (value: string) => <Typography.Text strong>{value}</Typography.Text> },
    { title: 'Поставщик', dataIndex: 'supplier_name', width: 180 },
    { title: 'Тип', width: 120, render: (_, version) => <Tag color={supportsRuleGeneration(version) ? 'blue' : 'default'}>{loaderLabel(version)}</Tag> },
    { title: 'Статус', dataIndex: 'status', width: 120, render: (value: string) => <Tag>{value}</Tag> },
    { title: 'Загружен', dataIndex: 'created_at', width: 165, render: formatDate },
    { title: 'Позиции', dataIndex: 'item_count', align: 'right', width: 100, render: (value: number) => value.toLocaleString('ru-RU') },
    { title: '', width: 135, fixed: 'right', render: (_, version) => (
      <Button size="small" disabled={!supportsRuleGeneration(version)} type={selected?.id === version.id ? 'primary' : 'default'} onClick={() => setSelected(version)}>
        {!supportsRuleGeneration(version) ? 'Не поддерживается' : selected?.id === version.id ? 'Выбран' : 'Выбрать'}
      </Button>
    ) },
  ], [selected]);
  const hasDiagnostics = rows.some((attempt) => Boolean(attempt.error_message));
  const attemptColumns: ColumnsType<RuleProposalAttempt> = [
    { title: 'Секция', key: 'section', width: 420, render: (_, attempt) => {
      const presentation = sectionPresentation(attempt);
      return (
        <Space direction="vertical" size={0} title={attempt.section_id}>
          <Typography.Text strong>{presentation.title}</Typography.Text>
          <Typography.Text type="secondary">{presentation.detail}</Typography.Text>
        </Space>
      );
    } },
    { title: 'Результат', dataIndex: 'status', width: 190, render: (value: string) => (
      <Tag color={READY.has(value) || value === 'draft_created' ? 'green' : TERMINAL.has(value) ? 'red' : 'blue'}>{STATUS_LABELS[value] ?? value}</Tag>
    ) },
    { title: 'Время LLM', key: 'duration', width: 110, render: (_, attempt) => attemptDurationLabel(attempt) },
    { title: 'Токены', key: 'tokens', width: 120, align: 'right', render: (_, attempt) => attemptTokensLabel(attempt) },
    ...(hasDiagnostics ? [{
      title: 'Диагностика', dataIndex: 'error_message', width: 260,
      render: (value?: string | null) => value ? <Typography.Text type="danger">{value}</Typography.Text> : '—',
    } satisfies ColumnsType<RuleProposalAttempt>[number]] : []),
  ];

  return (
    <Modal title="Новое правило разбора" open={open} onCancel={resetAndClose} closable={!creatingDraft} maskClosable={!creatingDraft} keyboard={!creatingDraft} footer={null} width={1050} styles={{ body: { maxHeight: 'calc(100vh - 150px)', overflow: 'hidden' } }} destroyOnHidden>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {!selected && !batchId && (
          <>
            <div>
              <Typography.Title level={5} style={{ margin: 0 }}>1. Выберите исходный прайс</Typography.Title>
              <Typography.Text type="secondary">По файлу автоматически определим формат, поставщика и исходные данные. Ничего вводить вручную на этом шаге не нужно.</Typography.Text>
            </div>
            <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
              <Input.Search aria-label="Поиск прайса для генерации" prefix={<SearchOutlined />} value={searchInput} placeholder="Имя файла, поставщик или код" enterButton="Найти" onChange={(event) => setSearchInput(event.target.value)} onSearch={(value) => { setSearch(value.trim()); setPage(1); }} style={{ width: 520, maxWidth: '100%' }} />
              <Typography.Text type="secondary">{(versions.data?.total ?? 0).toLocaleString('ru-RU')} прайсов</Typography.Text>
            </Space>
            {versions.isError && <Alert showIcon type="error" message="Не удалось загрузить список прайсов" description={extractApiError(versions.error)} />}
            <Table<ImportedPriceVersion> rowKey="id" size="small" columns={priceColumns} dataSource={versions.data?.items ?? []} loading={versions.isFetching} scroll={{ x: 1050 }} pagination={{ current: page, pageSize: PAGE_SIZE, total: versions.data?.total ?? 0, showSizeChanger: false, onChange: setPage }} locale={{ emptyText: search ? 'По запросу ничего не найдено' : 'Прайсы не найдены' }} />
          </>
        )}
        {selected && !batchId && (
          <>
            <div>
              <Typography.Title level={5} style={{ margin: 0 }}>2. Подготовьте черновик</Typography.Title>
              <Typography.Text type="secondary">Источник уже определил основные параметры. После генерации все листы, секции, названия и роли колонок можно изменить в редакторе правила.</Typography.Text>
            </div>
            <Descriptions bordered size="small" column={{ xs: 1, sm: 2, lg: 3 }}>
              <Descriptions.Item label="Файл" span={2}><Typography.Text strong>{selected.filename}</Typography.Text></Descriptions.Item>
              <Descriptions.Item label="Формат"><Tag>{loaderLabel(selected)}</Tag></Descriptions.Item>
              <Descriptions.Item label="Поставщик">{selected.supplier_name || 'Не определён'}</Descriptions.Item>
              <Descriptions.Item label="Версия">v{selected.version_no}</Descriptions.Item>
              <Descriptions.Item label="Позиции">{selected.item_count.toLocaleString('ru-RU')}</Descriptions.Item>
              <Descriptions.Item label="Загружен" span={3}>{formatDate(selected.created_at)}</Descriptions.Item>
            </Descriptions>
            <Button icon={<LeftOutlined />} onClick={() => setSelected(undefined)}>Выбрать другой файл</Button>
            <Alert showIcon type="info" message="DeepSeek определит структуру, но не будет генерировать товары и цены" description="Модель получит компактные проекции отдельных секций. Backend привяжет ответ к реальным координатам файла, проверит результат и создаст неактивный черновик." />
            {options.isError && <Alert showIcon type="error" message="Настройки модели недоступны" description={extractApiError(options.error)} />}
            <Typography.Text strong>Способ подготовки</Typography.Text>
            <Space wrap>
              <Select aria-label="LLM-провайдер" value={provider || undefined} style={{ width: 180 }} onChange={(value) => { setProvider(value); const next = options.data?.providers.find((item) => item.provider === value); setModel(next?.default_model ?? ''); }} options={(options.data?.providers ?? []).map((item) => ({ value: item.provider, label: item.label, disabled: !item.enabled }))} />
              <Select aria-label="Модель" value={model || undefined} style={{ width: 230 }} onChange={setModel} options={(selectedProvider?.models ?? []).map((value) => ({ value, label: value }))} />
              <Button type="primary" icon={<RobotOutlined />} disabled={!selected || !provider || !model} loading={start.isPending} onClick={() => start.mutate()}>
                Запустить {selectedProvider?.label ?? 'модель'}
              </Button>
            </Space>
          </>
        )}
        {batchId && (
          <>
            <div>
              <Typography.Title level={5} style={{ margin: 0 }}>3. Проверьте результат анализа</Typography.Title>
              <Typography.Text type="secondary">DeepSeek анализирует секции файла «{selected?.filename}». Черновик появится только после проверки всех секций.</Typography.Text>
            </div>
            <Progress percent={rows.length ? Math.round((completed / rows.length) * 100) : 0} status={hasFailure ? 'exception' : allReady ? 'success' : 'active'} format={() => rows.length ? `${completed} из ${rows.length} секций готовы` : 'Подготовка секций'} />
            <div
              data-testid="rule-attempt-scroll"
              role="region"
              aria-label="Ход анализа секций"
              style={{ maxHeight: 'clamp(180px, calc(100vh - 390px), 520px)', overflowY: 'auto' }}
            >
              <Table<RuleProposalAttempt>
                rowKey="id"
                size="small"
                sticky
                tableLayout="fixed"
                columns={attemptColumns}
                dataSource={displayRows}
                loading={attempts.isFetching && rows.length === 0}
                scroll={{ x: hasDiagnostics ? 1100 : 840 }}
                pagination={false}
              />
            </div>
            {hasFailure && <Alert showIcon type="warning" message="Черновик пока нельзя создать" description="Откройте диагностику проблемной секции и запустите новый batch после исправления. Непроверенный ответ модели в правило не попадёт." />}
            {creatingDraft && (
              <Alert
                showIcon
                type="info"
                message="Собираем черновик"
                description={(
                  <Space direction="vertical" size={6} style={{ width: '100%' }}>
                    <Typography.Text>
                      {draftProgress.data?.message ?? 'Сервер проверяет исходный файл и объединяет секции'}
                    </Typography.Text>
                    <Progress percent={draftProgress.data?.percent ?? 1} status="active" size="small" />
                    <Space wrap size="middle">
                      <Typography.Text strong>
                        {(draftProgress.data?.completed_sections ?? 0).toLocaleString('ru-RU')} из {(draftProgress.data?.total_sections || rows.length).toLocaleString('ru-RU')} секций
                      </Typography.Text>
                      <Typography.Text type="secondary">прошло {draftElapsedSeconds} с</Typography.Text>
                    </Space>
                  </Space>
                )}
              />
            )}
            <Space>
              <Button type="primary" icon={<CheckCircleOutlined />} disabled={!allReady} loading={createDraft.isPending} onClick={() => createDraft.mutate()}>Создать черновик</Button>
              <Button disabled={creatingDraft} onClick={resetAndClose}>Закрыть</Button>
            </Space>
          </>
        )}
      </Space>
    </Modal>
  );
}
