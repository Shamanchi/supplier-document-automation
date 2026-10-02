import { PlusOutlined } from '@ant-design/icons';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, App, Button, Input, Popconfirm, Select, Space, Table, Tabs, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import type { MouseEvent } from 'react';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  activatePriceVersion,
  extractApiError,
  fetchImportedPriceVersions,
  fetchPriceImports,
} from '../api/client';
import { TERMINAL_IMPORT_STATUSES } from '../api/types';
import type {
  ImportedPriceVersion,
  PriceImportStatus,
  PriceImportStatusName,
} from '../api/types';
import { useAuth } from '../auth/AuthContext';
import ImportStatusTag, { importStatusLabel } from '../components/ImportStatusTag';
import UploadImportModal from '../components/UploadImportModal';
import { formatDate, metric } from '../components/importFormat';

const PAGE_SIZE = 25;
const STATUSES: PriceImportStatusName[] = [
  'queued',
  'parsing',
  'generating_rule',
  'compiling_rule',
  'reparsing',
  'quality_check',
  'completed',
  'review_required',
  'needs_ocr',
  'failed',
];

const VERSION_STATUS: Record<string, { label: string; color?: string }> = {
  uploaded: { label: 'Загружен', color: 'blue' },
  parsing: { label: 'Разбирается', color: 'processing' },
  needs_mapping: { label: 'Нужен маппинг', color: 'gold' },
  complex: { label: 'Нужна проверка', color: 'gold' },
  validation_failed: { label: 'Ошибка проверки', color: 'red' },
  validated: { label: 'Разобран', color: 'green' },
  active: { label: 'Активен', color: 'green' },
  superseded: { label: 'Заменён' },
  withdrawn: { label: 'Отозван' },
  failed: { label: 'Ошибка', color: 'red' },
  needs_ocr: { label: 'Нужен OCR', color: 'orange' },
  unsupported_format: { label: 'Формат не поддержан', color: 'red' },
};

export default function ImportsPage() {
  const { message } = App.useApp();
  const { isAdmin } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState('prices');
  const [pricePage, setPricePage] = useState(1);
  const [priceQuery, setPriceQuery] = useState('');
  const [priceScope, setPriceScope] = useState('with-items');
  const [jobPage, setJobPage] = useState(1);
  const [status, setStatus] = useState<string>('');
  const [uploadOpen, setUploadOpen] = useState(false);

  const pricesQuery = useQuery({
    queryKey: ['imported-price-versions', priceScope, priceQuery, pricePage],
    queryFn: () => fetchImportedPriceVersions({
      query: priceQuery || undefined,
      hasItems: priceScope === 'all' ? undefined : priceScope === 'with-items',
      limit: PAGE_SIZE,
      offset: (pricePage - 1) * PAGE_SIZE,
    }),
    placeholderData: keepPreviousData,
    enabled: activeTab === 'prices',
  });
  const jobsQuery = useQuery({
    queryKey: ['price-imports', status, jobPage],
    queryFn: () => fetchPriceImports({
      status: status || undefined,
      limit: PAGE_SIZE,
      offset: (jobPage - 1) * PAGE_SIZE,
    }),
    placeholderData: keepPreviousData,
    enabled: activeTab === 'jobs',
    refetchInterval: (result) => (
      result.state.data?.items.some((row) => !TERMINAL_IMPORT_STATUSES.has(row.status))
        ? 2_000
        : false
    ),
  });

  const activateVersion = useMutation({
    mutationFn: (versionId: string) => activatePriceVersion(versionId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['imported-price-versions'] });
      void message.success('Версия стала действующим прайсом поставщика');
    },
    onError: (error) => void message.error(extractApiError(error)),
  });

  const priceColumns: ColumnsType<ImportedPriceVersion> = [
    {
      title: 'Поставщик',
      key: 'supplier',
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{row.supplier_name}</Typography.Text>
          <Typography.Text type="secondary">{row.supplier_external_id ?? '—'}</Typography.Text>
        </Space>
      ),
    },
    {
      title: 'Прайс',
      dataIndex: 'filename',
      key: 'filename',
      ellipsis: true,
      render: (value: string, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text>{value}</Typography.Text>
          <Typography.Text type="secondary">
            версия {row.version_no}
            {row.document_type === 'SUPPLIER_QUOTE' ? ' · счёт/КП заявки' : ''}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: 'Статус',
      dataIndex: 'status',
      key: 'status',
      width: 190,
      render: (value: string) => {
        const statusInfo = VERSION_STATUS[value] ?? { label: value };
        return (
          <Space direction="vertical" size={0}>
            <Tag color={statusInfo.color}>{statusInfo.label}</Tag>
            {value === 'validated' ? (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                требуется активация
              </Typography.Text>
            ) : null}
          </Space>
        );
      },
    },
    {
      title: 'Позиций',
      dataIndex: 'item_count',
      key: 'item_count',
      width: 110,
      align: 'right',
      render: (value: number) => value.toLocaleString('ru-RU'),
    },
    {
      title: 'Ошибки / предупреждения',
      key: 'quality',
      width: 190,
      align: 'right',
      render: (_, row) => `${row.errors_count.toLocaleString('ru-RU')} / ${row.warnings_count.toLocaleString('ru-RU')}`,
    },
    {
      title: 'Загружен',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 170,
      render: formatDate,
    },
    ...(isAdmin
      ? [
          {
            title: 'Действия',
            key: 'actions',
            width: 130,
            render: (_: unknown, row: ImportedPriceVersion) => {
              // Разовый счёт/КП заявки активации не подлежит: он участвует
              // в подборе своей заявки уже в статусе validated.
              const activatable =
                row.status === 'validated' && row.document_type === 'PRICE_LIST';
              if (!activatable) return null;
              const stop = (event: MouseEvent) => event.stopPropagation();
              return (
                <Popconfirm
                  title="Сделать действующим прайсом поставщика?"
                  description="Текущий действующий прайс этого поставщика будет заменён."
                  okText="Активировать"
                  cancelText="Отмена"
                  onConfirm={() => activateVersion.mutate(row.id)}
                >
                  <Button
                    size="small"
                    type="primary"
                    loading={activateVersion.isPending && activateVersion.variables === row.id}
                    onClick={stop}
                  >
                    Активировать
                  </Button>
                </Popconfirm>
              );
            },
          } satisfies ColumnsType<ImportedPriceVersion>[number],
        ]
      : []),
  ];

  const jobColumns: ColumnsType<PriceImportStatus> = [
    {
      title: 'Поставщик',
      key: 'supplier',
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong>{row.supplier_name}</Typography.Text>
          <Typography.Text type="secondary">{row.supplier_external_id ?? '—'}</Typography.Text>
        </Space>
      ),
    },
    { title: 'Файл', dataIndex: 'filename', key: 'filename', ellipsis: true },
    {
      title: 'Статус',
      dataIndex: 'status',
      key: 'status',
      width: 180,
      render: (value: PriceImportStatusName) => <ImportStatusTag status={value} />,
    },
    {
      title: 'Позиций', dataIndex: 'item_count', key: 'item_count', width: 100, align: 'right',
      render: (value: number) => value.toLocaleString('ru-RU'),
    },
    {
      title: 'Токенов', key: 'tokens', width: 120, align: 'right',
      render: (_, row) => metric(row.telemetry, 'total_tokens').toLocaleString('ru-RU'),
    },
    { title: 'Создан', dataIndex: 'created_at', key: 'created_at', width: 170, render: formatDate },
  ];

  const prices = (
    <>
      <Space wrap style={{ marginBottom: 16 }}>
        <Input.Search
          allowClear
          enterButton="Найти"
          placeholder="Поставщик или имя файла"
          style={{ width: 480, maxWidth: '100%' }}
          onSearch={(value) => {
            setPriceQuery(value.trim());
            setPricePage(1);
          }}
        />
        <Select
          value={priceScope}
          style={{ width: 210 }}
          onChange={(value) => {
            setPriceScope(value);
            setPricePage(1);
          }}
          options={[
            { value: 'with-items', label: 'С разобранными позициями' },
            { value: 'without-items', label: 'Без позиций' },
            { value: 'all', label: 'Все прайсы' },
          ]}
        />
      </Space>
      {pricesQuery.isError && (
        <Alert
          type="error"
          showIcon
          message="Не удалось загрузить прайсы"
          description={extractApiError(pricesQuery.error)}
          style={{ marginBottom: 16 }}
        />
      )}
      <Table<ImportedPriceVersion>
        rowKey="id"
        columns={priceColumns}
        dataSource={pricesQuery.data?.items ?? []}
        loading={pricesQuery.isFetching}
        onRow={(row) => ({
          onClick: () => {
            if (row.item_count > 0) navigate(`/data?version=${row.id}`);
          },
          style: { cursor: row.item_count > 0 ? 'pointer' : 'default' },
        })}
        pagination={{
          current: pricePage,
          pageSize: PAGE_SIZE,
          total: pricesQuery.data?.total ?? 0,
          showSizeChanger: false,
          showTotal: (total) => `Всего прайсов: ${total.toLocaleString('ru-RU')}`,
          onChange: setPricePage,
        }}
        locale={{ emptyText: priceQuery ? 'По запросу ничего не найдено' : 'Прайсов пока нет' }}
      />
    </>
  );

  const jobs = (
    <>
      <Select
        value={status}
        style={{ width: 240, marginBottom: 16 }}
        onChange={(value) => {
          setStatus(value);
          setJobPage(1);
        }}
        options={[
          { value: '', label: 'Все статусы' },
          ...STATUSES.map((value) => ({ value, label: importStatusLabel(value) })),
        ]}
      />
      {jobsQuery.isError && (
        <Alert
          type="error"
          showIcon
          message="Не удалось загрузить историю заданий"
          description={extractApiError(jobsQuery.error)}
          style={{ marginBottom: 16 }}
        />
      )}
      <Table<PriceImportStatus>
        rowKey="id"
        columns={jobColumns}
        dataSource={jobsQuery.data?.items ?? []}
        loading={jobsQuery.isFetching}
        onRow={(row) => ({
          onClick: () => navigate(`/imports/${row.id}`),
          style: { cursor: 'pointer' },
        })}
        pagination={{
          current: jobPage,
          pageSize: PAGE_SIZE,
          total: jobsQuery.data?.total ?? 0,
          showSizeChanger: false,
          showTotal: (total) => `Всего заданий: ${total.toLocaleString('ru-RU')}`,
          onChange: setJobPage,
        }}
        locale={{ emptyText: 'Заданий импорта пока нет' }}
      />
    </>
  );

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 8 }} wrap>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>Прайсы</Typography.Title>
          <Typography.Text type="secondary">
            Все загруженные версии и созданные из них позиции
          </Typography.Text>
        </div>
        {isAdmin && (
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setUploadOpen(true)}>
            Загрузить прайс
          </Button>
        )}
      </Space>
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={[
          { key: 'prices', label: 'Прайсы и позиции', children: prices },
          { key: 'jobs', label: 'История заданий', children: jobs },
        ]}
      />
      {isAdmin && (
        <UploadImportModal
          open={uploadOpen}
          onClose={() => setUploadOpen(false)}
          onCreated={(importId) => navigate(`/imports/${importId}`)}
        />
      )}
    </div>
  );
}
