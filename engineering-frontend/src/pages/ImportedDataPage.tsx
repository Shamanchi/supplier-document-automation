import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { DownloadOutlined, ThunderboltOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Input,
  InputNumber,
  message,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useDeferredValue, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  assignItemCategory,
  autoClassifyItems,
  extractApiError,
  fetchGlobalCategoryPage,
  fetchImportedPriceItems,
  fetchPriceVersionOriginal,
} from '../api/client';
import type { AutoClassifySuggestion, ImportedPriceItem } from '../api/types';
import { useAuth } from '../auth/AuthContext';
import { formatPrice } from '../components/importFormat';
import SourceEvidencePanel from '../components/SourceEvidencePanel';

const PAGE_SIZE = 50;

export default function ImportedDataPage() {
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState('');
  const [searchParams, setSearchParams] = useSearchParams();
  const priceVersionId = searchParams.get('version') ?? '';
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const [categoryOptionQuery, setCategoryOptionQuery] = useState('');
  const deferredCategoryOptionQuery = useDeferredValue(categoryOptionQuery);
  const [autoClassifyOpen, setAutoClassifyOpen] = useState(false);
  const [minScore, setMinScore] = useState(0.5);
  const [suggestions, setSuggestions] = useState<AutoClassifySuggestion[] | null>(null);
  const [considered, setConsidered] = useState(0);
  const request = useQuery({
    queryKey: ['imported-price-items', priceVersionId, query, page],
    queryFn: () => fetchImportedPriceItems({
      query: query || undefined,
      priceVersionId: priceVersionId || undefined,
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
    }),
    placeholderData: keepPreviousData,
  });
  const categoryOptionsQuery = useQuery({
    queryKey: ['item-category-options', deferredCategoryOptionQuery],
    queryFn: () => fetchGlobalCategoryPage({
      query: deferredCategoryOptionQuery.trim() || undefined,
      limit: 100,
      offset: 0,
    }),
  });
  const categoryOptions = useMemo(
    () => (categoryOptionsQuery.data?.items ?? []).map((row) => ({ value: row.id, label: row.path })),
    [categoryOptionsQuery.data],
  );

  const invalidateItems = () =>
    queryClient.invalidateQueries({ queryKey: ['imported-price-items'] });

  const assignCategory = useMutation({
    mutationFn: ({ itemId, globalCategoryId }: { itemId: string; globalCategoryId: string | null }) =>
      assignItemCategory(itemId, globalCategoryId),
    onSuccess: async () => {
      await invalidateItems();
      void message.success('Категория назначена');
    },
    onError: (error) => void message.error(extractApiError(error)),
  });

  const autoClassify = useMutation({
    mutationFn: (apply: boolean) =>
      autoClassifyItems({ price_version_id: priceVersionId, apply, min_score: minScore }),
    onSuccess: async (result, apply) => {
      setConsidered(result.considered);
      setSuggestions(result.suggestions);
      if (apply) {
        await invalidateItems();
        void message.success(`Назначено категорий: ${result.applied}`);
      }
    },
    onError: (error) => void message.error(extractApiError(error)),
  });

  const downloadOriginal = async (row: ImportedPriceItem) => {
    const blob = await fetchPriceVersionOriginal(row.price_version_id);
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = row.filename;
    link.click();
    URL.revokeObjectURL(url);
  };

  const columns: ColumnsType<ImportedPriceItem> = [
    {
      title: 'Товар',
      dataIndex: 'source_name',
      key: 'source_name',
      width: 320,
      render: (value: string, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text strong style={{ whiteSpace: 'pre-line' }}>{value}</Typography.Text>
          <Typography.Text type="secondary">
            {row.article ?? 'без артикула'} · {row.unit ?? 'ед. не указана'}
          </Typography.Text>
        </Space>
      ),
    },
    {
      title: 'Поставщик и прайс',
      key: 'source_file',
      width: 280,
      render: (_, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text>{row.supplier_name}</Typography.Text>
          <Typography.Text type="secondary">{row.filename} · версия {row.version_no}</Typography.Text>
        </Space>
      ),
    },
    {
      title: 'Категория',
      key: 'category',
      width: 300,
      render: (_, row) => {
        const statusTag = row.category.mapping_status === 'manual' ? (
          <Tag color="blue">вручную</Tag>
        ) : row.category.mapping_status === 'mapped' ? (
          <Tag color="green">по прайсу</Tag>
        ) : (
          <Tag color="warning">не назначена</Tag>
        );
        if (!isAdmin) {
          return (
            <Space direction="vertical" size={2}>
              {row.category.source_path && <Typography.Text>{row.category.source_path}</Typography.Text>}
              {row.category.global_path ? <Tag color="green">{row.category.global_path}</Tag> : statusTag}
            </Space>
          );
        }
        return (
          <Space direction="vertical" size={2} style={{ width: '100%' }}>
            {row.category.source_path && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {row.category.source_path}
              </Typography.Text>
            )}
            <Select
              showSearch
              allowClear
              filterOption={false}
              size="small"
              style={{ width: '100%' }}
              placeholder="Назначить общую категорию"
              options={categoryOptions}
              value={row.category.global_category_id ?? undefined}
              loading={categoryOptionsQuery.isFetching
                || (assignCategory.isPending && assignCategory.variables?.itemId === row.id)}
              onSearch={setCategoryOptionQuery}
              onChange={(value) => assignCategory.mutate({ itemId: row.id, globalCategoryId: value ?? null })}
              notFoundContent={categoryOptionQuery ? 'Совпадений нет' : 'Введите название для поиска'}
            />
            {statusTag}
          </Space>
        );
      },
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
      title: 'Источник',
      key: 'coordinates',
      width: 180,
      render: (_, row) => (
        <Typography.Text type="secondary">
          {row.source_sheet}, строка {row.source_row}
          {row.source_panel_index != null ? `, панель ${row.source_panel_index + 1}` : ''}
        </Typography.Text>
      ),
    },
  ];

  return (
    <div>
      <Typography.Title level={3} style={{ margin: 0 }}>Импортированные данные</Typography.Title>
      <Typography.Paragraph type="secondary">
        Позиции из всех обработанных прайс-листов, включая импорты до появления истории заданий.
      </Typography.Paragraph>
      {priceVersionId && (
        <Alert
          type="info"
          showIcon
          message={request.data?.items[0]
            ? `${request.data.items[0].filename} · версия ${request.data.items[0].version_no}`
            : 'Показаны позиции выбранного прайса'}
          description={request.data?.items[0]?.supplier_name}
          action={(
            <Button
              size="small"
              onClick={() => {
                setSearchParams({});
                setPage(1);
              }}
            >
              Показать все
            </Button>
          )}
          style={{ marginBottom: 16 }}
        />
      )}
      <Space style={{ marginBottom: 16 }} wrap>
        <Input.Search
          allowClear
          enterButton="Найти"
          placeholder="Товар, артикул, поставщик или файл"
          style={{ width: 480 }}
          onSearch={(value) => {
            setQuery(value.trim());
            setPage(1);
          }}
        />
        {isAdmin && priceVersionId && (
          <Button
            icon={<ThunderboltOutlined />}
            onClick={() => {
              setSuggestions(null);
              setAutoClassifyOpen(true);
            }}
          >
            Автоклассификация
          </Button>
        )}
      </Space>
      {request.isError && (
        <Alert
          type="error"
          showIcon
          message="Не удалось загрузить импортированные данные"
          description={extractApiError(request.error)}
          style={{ marginBottom: 16 }}
        />
      )}
      <Table<ImportedPriceItem>
        rowKey="id"
        size="small"
        scroll={{ x: 1300 }}
        columns={columns}
        dataSource={request.data?.items ?? []}
        loading={request.isFetching}
        expandable={{
          expandedRowRender: (row) => (
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <SourceEvidencePanel item={row} />
              <Button
                size="small"
                icon={<DownloadOutlined />}
                onClick={() => downloadOriginal(row)}
              >
                Скачать оригинал файла
              </Button>
            </Space>
          ),
        }}
        pagination={{
          current: page,
          pageSize: PAGE_SIZE,
          total: request.data?.total ?? 0,
          showSizeChanger: false,
          showTotal: (total) => `Всего позиций: ${total.toLocaleString('ru-RU')}`,
          onChange: setPage,
        }}
        locale={{ emptyText: query ? 'По запросу ничего не найдено' : 'Импортированных позиций пока нет' }}
      />
      <Modal
        title="Автоклассификация позиций без категории"
        open={autoClassifyOpen}
        width={760}
        onCancel={() => setAutoClassifyOpen(false)}
        footer={(
          <Space>
            <Button onClick={() => setAutoClassifyOpen(false)}>Закрыть</Button>
            <Button
              loading={autoClassify.isPending && autoClassify.variables === false}
              onClick={() => autoClassify.mutate(false)}
            >
              Предложить
            </Button>
            <Button
              type="primary"
              disabled={!suggestions?.length}
              loading={autoClassify.isPending && autoClassify.variables === true}
              onClick={() => autoClassify.mutate(true)}
            >
              Применить все
            </Button>
          </Space>
        )}
      >
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
            Прототип подбирает общую категорию по совпадению слов в названии позиции.
            Рассматриваются только позиции выбранного прайса, у которых ещё нет категории.
          </Typography.Paragraph>
          <Space>
            <Typography.Text>Минимальная уверенность:</Typography.Text>
            <InputNumber
              min={0}
              max={1}
              step={0.1}
              value={minScore}
              onChange={(value) => setMinScore(typeof value === 'number' ? value : 0.5)}
            />
          </Space>
          {suggestions && (
            <Alert
              type="info"
              showIcon
              message={`Позиций без категории: ${considered}. Предложено: ${suggestions.length}.`}
            />
          )}
          {suggestions && (
            <Table<AutoClassifySuggestion>
              rowKey="item_id"
              size="small"
              dataSource={suggestions}
              pagination={{ pageSize: 10, showSizeChanger: false }}
              columns={[
                { title: 'Позиция', dataIndex: 'source_name', key: 'source_name', ellipsis: true },
                { title: 'Категория', dataIndex: 'suggested_path', key: 'suggested_path', ellipsis: true },
                {
                  title: 'Оценка',
                  dataIndex: 'score',
                  key: 'score',
                  width: 90,
                  render: (value: number) => `${Math.round(value * 100)}%`,
                },
              ]}
            />
          )}
        </Space>
      </Modal>
    </div>
  );
}
