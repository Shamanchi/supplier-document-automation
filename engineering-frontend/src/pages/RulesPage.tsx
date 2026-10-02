import { CheckCircleOutlined, EyeOutlined, PlusOutlined, SearchOutlined, StopOutlined } from '@ant-design/icons';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, App, Button, Input, Select, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useDeferredValue, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  activateParseRule,
  deprecateParseRule,
  extractApiError,
  fetchParseRulePage,
} from '../api/client';
import type { ParseRule } from '../api/types';
import { useAuth } from '../auth/AuthContext';
import { formatDate } from '../components/importFormat';
import RuleGenerationWizard from '../components/rules/RuleGenerationWizard';

const PAGE_SIZE = 25;

const STATUS_LABELS: Record<ParseRule['status'], string> = {
  draft: 'Черновик',
  active: 'Активно',
  deprecated: 'Архив',
};

function validationPassed(rule: ParseRule): boolean {
  return rule.validation?.passed === true;
}

function usage(rule: ParseRule) {
  return rule.usage ?? { price_versions: 0, database_items: 0, source_rows: 0 };
}

export default function RulesPage() {
  const { message } = App.useApp();
  const { isAdmin } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState('');
  const [loader, setLoader] = useState('');
  const [search, setSearch] = useState('');
  const [generateOpen, setGenerateOpen] = useState(false);
  const deferredSearch = useDeferredValue(search);

  const query = useQuery({
    queryKey: ['parse-rules-page', status, loader, deferredSearch, page],
    queryFn: () => fetchParseRulePage({
      query: deferredSearch || undefined,
      status: status || undefined,
      loader: loader || undefined,
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
    }),
    placeholderData: keepPreviousData,
  });

  const lifecycle = useMutation({
    mutationFn: ({ rule, action }: { rule: ParseRule; action: 'activate' | 'deprecate' }) =>
      action === 'activate' ? activateParseRule(rule.id) : deprecateParseRule(rule.id),
    onSuccess: async (_, variables) => {
      await queryClient.invalidateQueries({ queryKey: ['parse-rules-page'] });
      void message.success(variables.action === 'activate' ? 'Правило активировано' : 'Правило отправлено в архив');
    },
    onError: (error) => void message.error(extractApiError(error)),
  });

  const columns: ColumnsType<ParseRule> = [
    {
      title: 'Правило',
      key: 'name',
      render: (_, row) => (
        <Space direction="vertical" size={1}>
          <Typography.Text strong>{row.name}</Typography.Text>
          <Typography.Text type="secondary">{row.supplier_name ?? 'Общее для всех поставщиков'}</Typography.Text>
        </Space>
      ),
    },
    { title: 'Формат', dataIndex: 'loader', key: 'loader', width: 110, render: (value: string) => <Tag>{value.toUpperCase()}</Tag> },
    {
      title: 'Статус', dataIndex: 'status', key: 'status', width: 130,
      render: (value: ParseRule['status']) => <Tag color={value === 'active' ? 'green' : value === 'draft' ? 'gold' : 'default'}>{STATUS_LABELS[value]}</Tag>,
    },
    { title: 'Ревизия', dataIndex: 'revision', key: 'revision', width: 90, align: 'right' },
    {
      title: 'Строк в БД', key: 'source_rows', width: 115, align: 'right',
      render: (_, row) => usage(row).source_rows.toLocaleString('ru-RU'),
    },
    {
      title: 'Позиций', key: 'database_items', width: 105, align: 'right',
      render: (_, row) => usage(row).database_items.toLocaleString('ru-RU'),
    },
    {
      title: 'Проверка', key: 'validation', width: 150,
      render: (_, row) => validationPassed(row)
        ? <Tag color="green">Пройдена</Tag>
        : <Tag color="warning">Нужна проверка</Tag>,
    },
    { title: 'Изменено', dataIndex: 'updated_at', key: 'updated_at', width: 170, render: formatDate },
    {
      title: 'Действия', key: 'actions', width: 260,
      render: (_, row) => (
        <Space wrap>
          <Button size="small" icon={<EyeOutlined />} onClick={() => navigate(`/rules/${row.id}`)}>Открыть</Button>
          {row.source_import_id && <Link to={`/imports/${row.source_import_id}`}><Button size="small">Исходный импорт</Button></Link>}
          {isAdmin && row.status === 'draft' && validationPassed(row) && (
            <Button size="small" type="primary" icon={<CheckCircleOutlined />} onClick={() => lifecycle.mutate({ rule: row, action: 'activate' })}>Активировать</Button>
          )}
          {isAdmin && row.status === 'active' && (
            <Button size="small" danger icon={<StopOutlined />} onClick={() => lifecycle.mutate({ rule: row, action: 'deprecate' })}>В архив</Button>
          )}
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 12 }}>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>Правила разбора</Typography.Title>
          <Typography.Text type="secondary">Правило запоминает структуру прайса: листы, заголовки и роли колонок.</Typography.Text>
        </div>
        {isAdmin && (
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setGenerateOpen(true)}>Создать правило</Button>
        )}
      </div>
      <Space wrap>
        <Input
          allowClear prefix={<SearchOutlined />} placeholder="Название правила или поставщик"
          value={search} onChange={(event) => { setSearch(event.target.value); setPage(1); }}
          style={{ width: 340, maxWidth: '100%' }}
        />
        <Select value={status} style={{ width: 170 }} onChange={(value) => { setStatus(value); setPage(1); }} options={[
          { value: '', label: 'Все статусы' },
          { value: 'draft', label: 'Черновики' },
          { value: 'active', label: 'Активные' },
          { value: 'deprecated', label: 'Архив' },
        ]} />
        <Select value={loader} style={{ width: 150 }} onChange={(value) => { setLoader(value); setPage(1); }} options={[
          { value: '', label: 'Все форматы' },
          { value: 'xlsx', label: 'XLSX' },
          { value: 'xls', label: 'XLS' },
          { value: 'pdf_text', label: 'PDF' },
          { value: 'docx', label: 'DOCX' },
        ]} />
      </Space>
      {query.isError && <Alert type="error" showIcon message="Правила не загружены" description={extractApiError(query.error)} />}
      <Table<ParseRule>
        rowKey="id" columns={columns} dataSource={query.data?.items ?? []} loading={query.isFetching}
        scroll={{ x: 1180 }}
        pagination={{
          current: page, pageSize: PAGE_SIZE, total: query.data?.total ?? 0, showSizeChanger: false,
          showTotal: (total) => `Всего правил: ${total.toLocaleString('ru-RU')}`, onChange: setPage,
        }}
        locale={{ emptyText: 'Правил в этом фильтре нет' }}
      />

      <RuleGenerationWizard
        open={generateOpen}
        onClose={() => setGenerateOpen(false)}
        onCreated={(rule) => {
          setGenerateOpen(false);
          void queryClient.invalidateQueries({ queryKey: ['parse-rules-page'] });
          navigate(`/rules/${rule.id}`);
        }}
      />
    </Space>
  );
}
