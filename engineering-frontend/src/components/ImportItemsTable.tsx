import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { Alert, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { extractApiError, fetchPriceImportItems } from '../api/client';
import type { ImportItem, ImportItemAttribute } from '../api/types';
import { formatPrice } from './importFormat';

const PAGE_SIZE = 50;

function attributeLabel(attribute: ImportItemAttribute): string {
  const value = attribute.value == null ? attribute.raw_value : String(attribute.value);
  return attribute.unit ? `${value} ${attribute.unit}` : value;
}

export default function ImportItemsTable({ importId }: { importId: string }) {
  const [page, setPage] = useState(1);
  const query = useQuery({
    queryKey: ['price-import-items', importId, page],
    queryFn: () =>
      fetchPriceImportItems(importId, {
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      }),
    placeholderData: keepPreviousData,
  });
  const columns: ColumnsType<ImportItem> = [
    {
      title: 'Наименование',
      dataIndex: 'source_name',
      key: 'source_name',
      width: 300,
      render: (value: string) => <span style={{ whiteSpace: 'pre-line' }}>{value}</span>,
    },
    {
      title: 'Артикул / ед.',
      key: 'identity',
      width: 130,
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <span>{row.article ?? '—'}</span>
          <Typography.Text type="secondary">{row.unit ?? 'ед. не указана'}</Typography.Text>
        </Space>
      ),
    },
    {
      title: 'Категория',
      key: 'category',
      width: 280,
      render: (_, row) => row.category.source_path ? (
        <Space direction="vertical" size={2}>
          <Typography.Text>{row.category.source_path}</Typography.Text>
          {row.category.global_path ? (
            <Tag color="green">Общая: {row.category.global_path}</Tag>
          ) : (
            <Tag color="warning">Не сопоставлена с общей</Tag>
          )}
        </Space>
      ) : (
        <Tag color={row.category.matching_identity === row.article ? 'red' : 'default'}>
          категория не определена
        </Tag>
      ),
    },
    {
      title: 'Цены',
      key: 'prices',
      width: 260,
      render: (_, row) => row.prices.length ? (
        <Space direction="vertical" size={2}>
          {row.prices.map((price, index) => (
            <span key={`${price.source_column_index}-${index}`}>
              {price.source_label && <Typography.Text type="secondary">{price.source_label}: </Typography.Text>}
              {formatPrice(price)}
            </span>
          ))}
        </Space>
      ) : <Tag color="red">цена отсутствует</Tag>,
    },
    {
      title: 'Характеристики',
      dataIndex: 'attributes',
      key: 'attributes',
      width: 260,
      render: (attributes: ImportItemAttribute[]) => attributes.length ? (
        <Space size={[4, 4]} wrap>
          {attributes.map((attribute) => (
            <Tag key={attribute.id} color={attribute.parse_status === 'invalid' ? 'warning' : undefined}>
              {attribute.name}: {attributeLabel(attribute)}
            </Tag>
          ))}
        </Space>
      ) : '—',
    },
    {
      title: 'Источник',
      key: 'source',
      width: 190,
      render: (_, row) => (
        <Typography.Text type="secondary">
          {row.source_sheet}, строка {row.source_row}
          {row.source_panel_index != null ? `, панель ${row.source_panel_index + 1}` : ''}
        </Typography.Text>
      ),
    },
  ];

  return query.isError ? (
    <Alert type="error" showIcon message="Позиции не загружены" description={extractApiError(query.error)} />
  ) : (
    <Table<ImportItem>
      rowKey="id"
      size="small"
      scroll={{ x: 1380 }}
      columns={columns}
      dataSource={query.data?.items ?? []}
      loading={query.isFetching}
      pagination={{
        current: page,
        pageSize: PAGE_SIZE,
        total: query.data?.total ?? 0,
        showSizeChanger: false,
        showTotal: (total) => `Всего позиций: ${total.toLocaleString('ru-RU')}`,
        onChange: setPage,
      }}
      locale={{ emptyText: 'Позиции не созданы' }}
    />
  );
}
