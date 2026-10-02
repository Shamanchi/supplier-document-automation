import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { Alert, Button, Select, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { extractApiError, fetchPriceImportIssues } from '../api/client';
import type { ImportIssue } from '../api/types';

const PAGE_SIZE = 50;

export default function ImportIssuesTable({
  importId,
  onOpenReview,
}: {
  importId: string;
  onOpenReview?: () => void;
}) {
  const [severity, setSeverity] = useState('');
  const [page, setPage] = useState(1);
  const query = useQuery({
    queryKey: ['price-import-issues', importId, severity, page],
    queryFn: () =>
      fetchPriceImportIssues(importId, {
        severity: severity || undefined,
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      }),
    placeholderData: keepPreviousData,
  });
  const columns: ColumnsType<ImportIssue> = [
    {
      title: 'Уровень',
      dataIndex: 'severity',
      key: 'severity',
      width: 130,
      render: (value: ImportIssue['severity']) => (
        <Tag color={value === 'error' ? 'red' : 'gold'}>
          {value === 'error' ? 'Ошибка' : 'Предупреждение'}
        </Tag>
      ),
    },
    {
      title: 'Код',
      dataIndex: 'code',
      key: 'code',
      width: 230,
      render: (value: string) => <Typography.Text code>{value}</Typography.Text>,
    },
    { title: 'Описание', dataIndex: 'message', key: 'message' },
    {
      title: 'Raw-значение',
      dataIndex: 'source_value',
      key: 'source_value',
      width: 180,
      render: (value: string | null) => value == null
        ? '—'
        : <Typography.Text code copyable>{value}</Typography.Text>,
    },
    {
      title: 'Источник',
      key: 'source',
      width: 240,
      render: (_, row) => [
        row.sheet ? `лист «${row.sheet}»` : null,
        row.row_no != null ? `строка ${row.row_no}` : null,
        row.column_ref ? `колонка ${row.column_ref}` : null,
      ].filter(Boolean).join(', ') || '—',
    },
    {
      title: 'Действие',
      key: 'action',
      width: 170,
      render: (_, row) => row.code === 'PRICE_CURRENCY_UNRESOLVED' && onOpenReview
        ? <Button size="small" onClick={onOpenReview}>Указать валюту</Button>
        : '—',
    },
  ];

  const hasUnresolvedCurrency = query.data?.issues.some(
    (issue) => issue.code === 'PRICE_CURRENCY_UNRESOLVED',
  );

  return (
    <div>
      <Space style={{ marginBottom: 16 }}>
        <span>Показать:</span>
        <Select
          value={severity}
          style={{ width: 210 }}
          options={[
            { value: '', label: 'Все замечания' },
            { value: 'error', label: 'Только ошибки' },
            { value: 'warning', label: 'Только предупреждения' },
          ]}
          onChange={(value) => {
            setSeverity(value);
            setPage(1);
          }}
        />
      </Space>
      {query.isError && (
        <Alert
          type="error"
          showIcon
          message="Замечания не загружены"
          description={extractApiError(query.error)}
          style={{ marginBottom: 16 }}
        />
      )}
      {hasUnresolvedCurrency && (
        <Alert
          type="warning"
          showIcon
          message="В исходном числе не удалось доказать валюту"
          description="Raw-значение сохранено без изменений. Откройте проверку, выберите валюту всего прайса, затем нажмите «Проверить» и «Применить»."
          action={onOpenReview ? <Button onClick={onOpenReview}>Перейти к исправлению</Button> : undefined}
          style={{ marginBottom: 16 }}
        />
      )}
      <Table<ImportIssue>
        rowKey="id"
        size="small"
        columns={columns}
        dataSource={query.data?.issues ?? []}
        loading={query.isFetching}
        pagination={{
          current: page,
          pageSize: PAGE_SIZE,
          total: query.data?.total ?? 0,
          showSizeChanger: false,
          showTotal: (total) => `Всего: ${total.toLocaleString('ru-RU')}`,
          onChange: setPage,
        }}
        locale={{ emptyText: 'Замечаний нет' }}
        scroll={{ x: 1180 }}
      />
    </div>
  );
}
