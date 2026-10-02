import {
  ApartmentOutlined,
  DownloadOutlined,
  DownOutlined,
  EyeOutlined,
  InfoCircleOutlined,
  LoadingOutlined,
  MergeCellsOutlined,
  PlusOutlined,
  RightOutlined,
  SearchOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Descriptions,
  Input,
  Modal,
  Popconfirm,
  Progress,
  Row,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useDeferredValue, useMemo, useState } from 'react';
import {
  bootstrapCategoryDirectory,
  createGlobalCategory,
  extractApiError,
  fetchCategoryDirectoryStats,
  fetchCategorySuppliers,
  fetchGlobalCategoryChildren,
  fetchGlobalCategoryPage,
  fetchGlobalCategoryRoots,
  fetchGlobalCategorySources,
  fetchPriceVersionOriginal,
  fetchPriceVersionPreview,
  fetchSupplierCategoryPage,
  mapSupplierCategory,
  mergeGlobalCategory,
} from '../api/client';
import type {
  CategoryPriceVersion,
  GlobalCategorySummary,
  ReviewSourceRow,
  SupplierCategory,
} from '../api/types';
import { useAuth } from '../auth/AuthContext';

const PAGE_SIZE = 50;
const PREVIEW_PAGE_SIZE = 50;

export default function CategoriesPage() {
  const { message } = App.useApp();
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const [tab, setTab] = useState('sources');
  const [supplierId, setSupplierId] = useState('');
  const [mappingFilter, setMappingFilter] = useState('all');
  const [sourceQuery, setSourceQuery] = useState('');
  const [sourcePage, setSourcePage] = useState(1);
  const [globalQuery, setGlobalQuery] = useState('');
  const [globalPage, setGlobalPage] = useState(1);
  const [globalOptionQuery, setGlobalOptionQuery] = useState('');
  const [includeDeprecated, setIncludeDeprecated] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [categoryName, setCategoryName] = useState('');
  const [parentId, setParentId] = useState<string | null>(null);
  const [mergeSource, setMergeSource] = useState<GlobalCategorySummary>();
  const [provenanceCategory, setProvenanceCategory] = useState<GlobalCategorySummary>();
  const [downloadingPriceVersionId, setDownloadingPriceVersionId] = useState<string>();
  const [previewPrice, setPreviewPrice] = useState<CategoryPriceVersion>();
  const [previewSheetIndex, setPreviewSheetIndex] = useState(0);
  const [previewPage, setPreviewPage] = useState(1);
  const [mergeTargetId, setMergeTargetId] = useState<string>();
  const [globalChildren, setGlobalChildren] = useState<Record<string, GlobalCategorySummary[]>>({});
  const [expandedGlobalRows, setExpandedGlobalRows] = useState<string[]>([]);
  const [loadingGlobalChildren, setLoadingGlobalChildren] = useState<Set<string>>(new Set());
  const deferredSourceQuery = useDeferredValue(sourceQuery);
  const deferredGlobalQuery = useDeferredValue(globalQuery);
  const deferredOptionQuery = useDeferredValue(globalOptionQuery);

  const stats = useQuery({ queryKey: ['category-stats'], queryFn: fetchCategoryDirectoryStats });
  const suppliers = useQuery({ queryKey: ['category-suppliers'], queryFn: fetchCategorySuppliers });
  const sourceCategories = useQuery({
    queryKey: ['supplier-category-page', supplierId, mappingFilter, deferredSourceQuery, sourcePage],
    queryFn: () => fetchSupplierCategoryPage({
      supplierId: supplierId || undefined,
      mapped: mappingFilter === 'all' ? undefined : mappingFilter === 'mapped',
      query: deferredSourceQuery || undefined,
      limit: PAGE_SIZE,
      offset: (sourcePage - 1) * PAGE_SIZE,
    }),
    placeholderData: keepPreviousData,
  });
  const isGlobalSearch = Boolean(deferredGlobalQuery.trim());
  const globalRoots = useQuery({
    queryKey: ['global-category-roots', includeDeprecated, globalPage],
    queryFn: () => fetchGlobalCategoryRoots({
      includeDeprecated,
      limit: PAGE_SIZE,
      offset: (globalPage - 1) * PAGE_SIZE,
    }),
    enabled: !isGlobalSearch,
    placeholderData: keepPreviousData,
  });
  const globalSearchResults = useQuery({
    queryKey: ['global-category-page', deferredGlobalQuery, includeDeprecated, globalPage],
    queryFn: () => fetchGlobalCategoryPage({
      query: deferredGlobalQuery.trim(),
      includeDeprecated,
      limit: PAGE_SIZE,
      offset: (globalPage - 1) * PAGE_SIZE,
    }),
    enabled: isGlobalSearch,
    placeholderData: keepPreviousData,
  });
  const globalOptionsQuery = useQuery({
    queryKey: ['global-category-options', deferredOptionQuery],
    queryFn: () => fetchGlobalCategoryPage({
      query: deferredOptionQuery || undefined,
      limit: 100,
      offset: 0,
    }),
  });
  const provenance = useQuery({
    queryKey: ['global-category-sources', provenanceCategory?.id],
    queryFn: () => fetchGlobalCategorySources(provenanceCategory!.id),
    enabled: Boolean(provenanceCategory),
  });
  const pricePreview = useQuery({
    queryKey: ['price-version-preview', previewPrice?.id, previewSheetIndex, previewPage],
    queryFn: () => fetchPriceVersionPreview(previewPrice!.id, {
      sheetIndex: previewSheetIndex,
      rowOffset: (previewPage - 1) * PREVIEW_PAGE_SIZE,
      rowLimit: PREVIEW_PAGE_SIZE,
    }),
    enabled: Boolean(previewPrice),
    placeholderData: keepPreviousData,
  });

  const refreshDirectory = async () => {
    setGlobalChildren({});
    setExpandedGlobalRows([]);
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['category-stats'] }),
      queryClient.invalidateQueries({ queryKey: ['category-suppliers'] }),
      queryClient.invalidateQueries({ queryKey: ['supplier-category-page'] }),
      queryClient.invalidateQueries({ queryKey: ['global-category-page'] }),
      queryClient.invalidateQueries({ queryKey: ['global-category-roots'] }),
      queryClient.invalidateQueries({ queryKey: ['global-category-children'] }),
      queryClient.invalidateQueries({ queryKey: ['global-category-options'] }),
      queryClient.invalidateQueries({ queryKey: ['price-import-items'] }),
    ]);
  };
  const mapping = useMutation({
    mutationFn: ({ id, globalId }: { id: string; globalId: string | null }) =>
      mapSupplierCategory(id, globalId),
    onSuccess: async () => {
      await refreshDirectory();
      void message.success('Сопоставление сохранено');
    },
    onError: (error) => void message.error(extractApiError(error)),
  });
  const bootstrap = useMutation({
    mutationFn: bootstrapCategoryDirectory,
    onSuccess: async (result) => {
      await refreshDirectory();
      void message.success(
        `Создано общих категорий: ${result.created_global_categories}; связей: ${result.created_mappings}`,
      );
    },
    onError: (error) => void message.error(extractApiError(error)),
  });
  const createCategory = useMutation({
    mutationFn: () => createGlobalCategory({ name: categoryName.trim(), parentId }),
    onSuccess: async () => {
      setCategoryName('');
      setParentId(null);
      setCreateOpen(false);
      await refreshDirectory();
      void message.success('Общая категория создана');
    },
    onError: (error) => void message.error(extractApiError(error)),
  });
  const merge = useMutation({
    mutationFn: () => mergeGlobalCategory(mergeSource!.id, mergeTargetId!),
    onSuccess: async (result) => {
      setMergeSource(undefined);
      setMergeTargetId(undefined);
      await refreshDirectory();
      void message.success(`Перенесено связей: ${result.moved_mappings}`);
    },
    onError: (error) => void message.error(extractApiError(error)),
  });

  const handlePriceDownload = async (price: CategoryPriceVersion) => {
    setDownloadingPriceVersionId(price.id);
    try {
      const blob = await fetchPriceVersionOriginal(price.id);
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = price.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
    } catch (error) {
      void message.error(extractApiError(error));
    } finally {
      setDownloadingPriceVersionId(undefined);
    }
  };

  const openPricePreview = (price: CategoryPriceVersion) => {
    setPreviewSheetIndex(0);
    setPreviewPage(1);
    setPreviewPrice(price);
  };

  const priceVersionLine = (price: CategoryPriceVersion, prefix?: string) => (
    <Space size={4} wrap>
      <Typography.Text strong={!prefix} type={prefix ? 'secondary' : undefined}>
        {prefix}{price.filename} · версия {price.version_no}
      </Typography.Text>
      <Tooltip title="Открыть исходный прайс">
        <Button
          type="text"
          size="small"
          shape="circle"
          icon={<EyeOutlined />}
          aria-label={`Открыть прайс ${price.filename}`}
          onClick={() => openPricePreview(price)}
        />
      </Tooltip>
      <Tooltip title="Скачать исходный прайс">
        <Button
          type="text"
          size="small"
          shape="circle"
          icon={<DownloadOutlined />}
          aria-label={`Скачать прайс ${price.filename}`}
          loading={downloadingPriceVersionId === price.id}
          onClick={() => void handlePriceDownload(price)}
        />
      </Tooltip>
    </Space>
  );

  const globalOptions = useMemo(() => {
    const values = new Map<string, string>();
    for (const row of globalOptionsQuery.data?.items ?? []) values.set(row.id, row.path);
    for (const row of sourceCategories.data?.items ?? []) {
      if (row.global_category) values.set(row.global_category.id, row.global_category.path);
    }
    return [...values].map(([value, label]) => ({ value, label }));
  }, [globalOptionsQuery.data, sourceCategories.data]);

  const previewColumns = useMemo<ColumnsType<ReviewSourceRow>>(() => {
    const columns: ColumnsType<ReviewSourceRow> = [{
      title: '#',
      dataIndex: 'excel_row',
      key: 'excel_row',
      fixed: 'left',
      width: 72,
    }];
    for (let index = 0; index < (pricePreview.data?.total_columns ?? 0); index += 1) {
      const excelColumn = pricePreview.data?.rows[0]?.cells[index]?.excel_column ?? String(index + 1);
      columns.push({
        title: excelColumn,
        key: `column-${index}`,
        width: 180,
        render: (_value, row) => {
          const value = row.cells[index]?.value;
          const text = value === null || value === undefined ? '' : String(value);
          return (
            <Typography.Text
              title={text}
              strong={row.cells[index]?.is_bold}
              style={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
            >
              {text}
            </Typography.Text>
          );
        },
      });
    }
    return columns;
  }, [pricePreview.data?.rows, pricePreview.data?.total_columns]);

  const globalTreeRows = useMemo<GlobalCategorySummary[]>(() => {
    if (isGlobalSearch) return globalSearchResults.data?.items ?? [];
    const result: GlobalCategorySummary[] = [];
    const append = (rows: GlobalCategorySummary[]) => {
      for (const row of rows) {
        result.push(row);
        if (expandedGlobalRows.includes(row.id)) append(globalChildren[row.id] ?? []);
      }
    };
    append(globalRoots.data?.items ?? []);
    return result;
  }, [
    expandedGlobalRows,
    globalChildren,
    globalRoots.data,
    globalSearchResults.data,
    isGlobalSearch,
  ]);

  const loadGlobalChildren = async (row: GlobalCategorySummary) => {
    if (
      row.children_count === 0
      || globalChildren[row.id] !== undefined
      || loadingGlobalChildren.has(row.id)
    ) return;
    setLoadingGlobalChildren((current) => new Set(current).add(row.id));
    try {
      const result = await queryClient.fetchQuery({
        queryKey: ['global-category-children', row.id, includeDeprecated],
        queryFn: async () => {
          const items: GlobalCategorySummary[] = [];
          let total = 0;
          do {
            const page = await fetchGlobalCategoryChildren(row.id, {
              includeDeprecated,
              limit: 500,
              offset: items.length,
            });
            total = page.total;
            items.push(...page.items);
            if (page.items.length === 0) break;
          } while (items.length < total);
          return { total, items };
        },
      });
      setGlobalChildren((current) => ({ ...current, [row.id]: result.items }));
    } catch (error) {
      setExpandedGlobalRows((current) => current.filter((id) => id !== row.id));
      void message.error(extractApiError(error));
    } finally {
      setLoadingGlobalChildren((current) => {
        const next = new Set(current);
        next.delete(row.id);
        return next;
      });
    }
  };

  const sourceColumns: ColumnsType<SupplierCategory> = [
    {
      title: 'Категория поставщика',
      key: 'source',
      width: 420,
      render: (_, row) => (
        <Space direction="vertical" size={2}>
          <Typography.Text strong>{row.source_path}</Typography.Text>
          <Typography.Text type="secondary">{row.supplier_name}</Typography.Text>
        </Space>
      ),
    },
    {
      title: 'Варианты написания',
      key: 'aliases',
      width: 320,
      render: (_, row) => (
        <Space size={[4, 4]} wrap>
          {row.aliases.slice(0, 3).map((alias) => <Tag key={alias.source_path}>{alias.source_path}</Tag>)}
          {row.aliases.length > 3 && <Tag>+{row.aliases.length - 3}</Tag>}
        </Space>
      ),
    },
    {
      title: 'Общая категория',
      key: 'global',
      width: 380,
      render: (_, row) => isAdmin ? (
        <Select
          showSearch
          allowClear
          filterOption={false}
          style={{ width: '100%' }}
          placeholder="Найдите и выберите общую категорию"
          options={globalOptions}
          value={row.global_category?.id}
          loading={globalOptionsQuery.isFetching || (mapping.isPending && mapping.variables?.id === row.id)}
          onSearch={setGlobalOptionQuery}
          onChange={(value) => mapping.mutate({ id: row.id, globalId: value ?? null })}
          notFoundContent={globalOptionQuery ? 'Совпадений нет' : 'Введите название для поиска'}
        />
      ) : row.global_category ? (
        <Tag color="green">{row.global_category.path}</Tag>
      ) : (
        <Tag color="warning">Не сопоставлена</Tag>
      ),
    },
  ];
  const globalColumns: ColumnsType<GlobalCategorySummary> = [
    {
      title: 'Иерархия',
      key: 'path',
      render: (_: unknown, row: GlobalCategorySummary) => (
        <Space
          direction="vertical"
          size={1}
          style={{ paddingLeft: isGlobalSearch ? 0 : row.depth * 20 }}
        >
          <Space size={4}>
            {!isGlobalSearch && row.children_count > 0 ? (
              <Button
                type="text"
                size="small"
                aria-label={`${expandedGlobalRows.includes(row.id) ? 'Свернуть' : 'Раскрыть'} ${row.name}`}
                icon={loadingGlobalChildren.has(row.id)
                  ? <LoadingOutlined spin />
                  : expandedGlobalRows.includes(row.id)
                    ? <DownOutlined />
                    : <RightOutlined />}
                onClick={() => {
                  const expanded = !expandedGlobalRows.includes(row.id);
                  setExpandedGlobalRows((current) => (
                    expanded
                      ? [...new Set([...current, row.id])]
                      : current.filter((id) => id !== row.id)
                  ));
                  if (expanded) void loadGlobalChildren(row);
                }}
              />
            ) : <span style={{ display: 'inline-block', width: 24 }} />}
            <Tag color={row.depth === 0 ? 'blue' : 'default'}>
              {row.depth === 0 ? 'Корень' : `Уровень ${row.depth + 1}`}
            </Tag>
            <Typography.Text strong>{row.name}</Typography.Text>
            {row.children_count > 0 && <Tag>{row.children_count} дочерних</Tag>}
          </Space>
          {row.depth > 0 && <Typography.Text type="secondary">{row.path}</Typography.Text>}
        </Space>
      ),
    },
    {
      title: 'Товаров',
      key: 'products',
      width: 170,
      align: 'right',
      render: (_: unknown, row: GlobalCategorySummary) => (
        <Space direction="vertical" size={0}>
          <Typography.Text>{row.product_count.toLocaleString('ru-RU')}</Typography.Text>
          {row.product_count !== row.direct_product_count && (
            <Typography.Text type="secondary">
              напрямую {row.direct_product_count.toLocaleString('ru-RU')}
            </Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: 'Категорий поставщиков',
      dataIndex: 'supplier_mappings_count',
      key: 'supplier_mappings_count',
      width: 210,
      align: 'right',
      render: (value: number, row) => (
        <Space direction="vertical" size={0}>
          <Typography.Text>{value.toLocaleString('ru-RU')}</Typography.Text>
          {value !== row.direct_supplier_mappings_count && (
            <Typography.Text type="secondary">
              напрямую {row.direct_supplier_mappings_count.toLocaleString('ru-RU')}
            </Typography.Text>
          )}
        </Space>
      ),
    },
    {
      title: 'Статус',
      dataIndex: 'status',
      key: 'status',
      width: 130,
      render: (value: string) => <Tag color={value === 'active' ? 'green' : 'default'}>{value === 'active' ? 'Активна' : 'Объединена'}</Tag>,
    },
    {
      title: 'Действия',
      key: 'actions',
      width: isAdmin ? 260 : 150,
      render: (_: unknown, row: GlobalCategorySummary) => (
        <Space wrap>
          <Button
            size="small"
            icon={<InfoCircleOutlined />}
            aria-label={`Источник ${row.name}`}
            onClick={() => setProvenanceCategory(row)}
          >
            Источник
          </Button>
          {isAdmin && row.status === 'active' && (
            <Button
              size="small"
              icon={<MergeCellsOutlined />}
              onClick={() => setMergeSource(row)}
            >
              Объединить
            </Button>
          )}
        </Space>
      ),
    },
  ];

  const error = stats.error
    ?? suppliers.error
    ?? sourceCategories.error
    ?? globalRoots.error
    ?? globalSearchResults.error;
  const mappedPercent = stats.data?.supplier_categories_count
    ? Math.round((stats.data.mapped_count / stats.data.supplier_categories_count) * 100)
    : 0;

  const sourceTable = (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Alert
        showIcon
        type="info"
        message="Как работать"
        description="Выберите поставщика или смотрите все категории сразу. Найдите исходное название и назначьте ему общую категорию. Одинаковые точные названия разных поставщиков базовая загрузка связывает автоматически."
      />
      <Card size="small">
        <Space wrap>
          <Select
            style={{ minWidth: 320 }}
            value={supplierId}
            loading={suppliers.isFetching}
            options={[
              { value: '', label: 'Все поставщики' },
              ...(suppliers.data ?? []).map((row) => ({
                value: row.supplier_id,
                label: `${row.supplier_name} · не сопоставлено ${row.unmapped_count}`,
              })),
            ]}
            onChange={(value) => { setSupplierId(value); setSourcePage(1); }}
          />
          <Select
            style={{ width: 220 }}
            value={mappingFilter}
            options={[
              { value: 'all', label: 'Все категории' },
              { value: 'unmapped', label: 'Только не сопоставленные' },
              { value: 'mapped', label: 'Только сопоставленные' },
            ]}
            onChange={(value) => { setMappingFilter(value); setSourcePage(1); }}
          />
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="Название категории или поставщика"
            value={sourceQuery}
            onChange={(event) => { setSourceQuery(event.target.value); setSourcePage(1); }}
            style={{ width: 360, maxWidth: '100%' }}
          />
        </Space>
      </Card>
      <Table<SupplierCategory>
        rowKey="id"
        columns={sourceColumns}
        dataSource={sourceCategories.data?.items ?? []}
        loading={sourceCategories.isFetching}
        scroll={{ x: 1120 }}
        pagination={{
          current: sourcePage,
          pageSize: PAGE_SIZE,
          total: sourceCategories.data?.total ?? 0,
          showSizeChanger: false,
          showTotal: (total) => `Всего: ${total.toLocaleString('ru-RU')}`,
          onChange: setSourcePage,
        }}
        locale={{ emptyText: 'В этом фильтре категорий нет' }}
      />
    </Space>
  );

  const globalTable = (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Alert
        showIcon
        type="info"
        message="Общий справочник — дерево"
        description="Сначала загружаются только корневые категории. Дочерние узлы запрашиваются при первом раскрытии ветки и затем берутся из кэша. Товары и связи у родителя показаны с учётом всех потомков; строка «напрямую» показывает только сам узел."
      />
      <Space wrap>
        <Input
          allowClear
          prefix={<SearchOutlined />}
          placeholder="Поиск по общему справочнику"
          value={globalQuery}
          onChange={(event) => {
            setGlobalQuery(event.target.value);
            setGlobalPage(1);
            setExpandedGlobalRows([]);
          }}
          style={{ width: 360, maxWidth: '100%' }}
        />
        <Button onClick={() => {
          setIncludeDeprecated((value) => !value);
          setGlobalChildren({});
          setExpandedGlobalRows([]);
          setGlobalPage(1);
        }}>
          {includeDeprecated ? 'Скрыть объединённые' : 'Показать объединённые'}
        </Button>
      </Space>
      {isGlobalSearch && (
        <Alert
          showIcon
          type="info"
          message="Результаты поиска показаны с полным путем; очистите поиск, чтобы вернуться к дереву."
        />
      )}
      <Table<GlobalCategorySummary>
        rowKey="id"
        columns={globalColumns}
        dataSource={globalTreeRows}
        loading={isGlobalSearch ? globalSearchResults.isFetching : globalRoots.isFetching}
        pagination={{
          current: globalPage,
          pageSize: PAGE_SIZE,
          total: isGlobalSearch
            ? globalSearchResults.data?.total ?? 0
            : globalRoots.data?.total ?? 0,
          showSizeChanger: false,
          showTotal: (total) => `${isGlobalSearch ? 'Совпадений' : 'Корней'}: ${total.toLocaleString('ru-RU')}`,
          onChange: (page) => {
            setGlobalPage(page);
            setExpandedGlobalRows([]);
          },
        }}
        locale={{ emptyText: 'Общий справочник пока пуст' }}
      />
    </Space>
  );

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Space style={{ width: '100%', justifyContent: 'space-between' }} align="start" wrap>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>Категории товаров</Typography.Title>
          <Typography.Text type="secondary">Связывайте исходные названия поставщиков с единым редактируемым справочником.</Typography.Text>
        </div>
        {isAdmin && (
          <Space wrap>
          <Popconfirm
              title="Заполнить базовый справочник?"
              description="Будут добавлены отсутствующие ветки по полным путям категорий поставщиков. Операция идемпотентна."
              okText="Заполнить"
              cancelText="Отмена"
              onConfirm={() => bootstrap.mutate()}
            >
              <Button icon={<SyncOutlined />} loading={bootstrap.isPending}>Заполнить из прайсов</Button>
            </Popconfirm>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>Новая общая категория</Button>
          </Space>
        )}
      </Space>
      {error && <Alert showIcon type="error" message="Справочник не загружен" description={extractApiError(error)} />}
      <Row gutter={[12, 12]}>
        <Col xs={12} md={6}><Card size="small"><Statistic title="Исходных" value={stats.data?.supplier_categories_count ?? 0} prefix={<ApartmentOutlined />} /></Card></Col>
        <Col xs={12} md={6}><Card size="small"><Statistic title="Сопоставлено" value={stats.data?.mapped_count ?? 0} /></Card></Col>
        <Col xs={12} md={6}><Card size="small"><Statistic title="Осталось" value={stats.data?.unmapped_count ?? 0} /></Card></Col>
        <Col xs={12} md={6}><Card size="small"><Statistic title="Общих" value={stats.data?.global_categories_count ?? 0} /></Card></Col>
      </Row>
      <Progress percent={mappedPercent} status={mappedPercent === 100 ? 'success' : 'active'} format={(value) => `Сопоставлено ${value}%`} />
      <Tabs activeKey={tab} onChange={setTab} items={[
        { key: 'sources', label: `Категории поставщиков (${stats.data?.supplier_categories_count ?? 0})`, children: sourceTable },
        { key: 'global', label: `Общий справочник (${stats.data?.global_categories_count ?? 0})`, children: globalTable },
      ]} />
      <Modal
        title={`Происхождение «${provenanceCategory?.path ?? ''}»`}
        open={Boolean(provenanceCategory)}
        footer={null}
        width={960}
        onCancel={() => setProvenanceCategory(undefined)}
      >
        {provenance.isFetching && <Typography.Text>Загружаем данные источника…</Typography.Text>}
        {provenance.error && (
          <Alert
            showIcon
            type="error"
            message="Не удалось загрузить происхождение категории"
            description={extractApiError(provenance.error)}
          />
        )}
        {provenance.data?.items.length === 0 && (
          <Alert
            showIcon
            type="info"
            message="У категории нет прямых связей с категориями поставщиков"
            description="Она создана вручную или содержит связи только в дочерних узлах."
          />
        )}
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          {provenance.data?.items.map((source) => (
            <Card
              key={source.supplier_category_id}
              size="small"
              title={`${source.supplier_name} · ${source.supplier_code}`}
              extra={<Tag>{source.mapping_source === 'manual' ? 'Ручной маппинг' : 'Импорт'}</Tag>}
            >
              <Descriptions size="small" column={1} bordered>
                <Descriptions.Item label="Raw-заголовок">
                  <Typography.Text copyable style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                    {source.raw_name}
                  </Typography.Text>
                </Descriptions.Item>
                <Descriptions.Item label="Raw-путь из прайса">
                  <Typography.Text copyable style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                    {source.raw_path}
                  </Typography.Text>
                </Descriptions.Item>
                <Descriptions.Item label="Прайс">
                  {source.first_seen ? (
                    <Space direction="vertical" size={0}>
                      {priceVersionLine(source.first_seen)}
                      {source.last_seen && source.last_seen.id !== source.first_seen.id && (
                        priceVersionLine(source.last_seen, 'Последний: ')
                      )}
                    </Space>
                  ) : 'Версия не сохранена (историческая запись)'}
                </Descriptions.Item>
                <Descriptions.Item label="Товаров напрямую">
                  {source.product_count.toLocaleString('ru-RU')}
                </Descriptions.Item>
              </Descriptions>
              {source.aliases.length > 1 && (
                <div style={{ marginTop: 12 }}>
                  <Typography.Text strong>Все raw-варианты заголовка</Typography.Text>
                  <Space direction="vertical" size={4} style={{ width: '100%', marginTop: 6 }}>
                    {source.aliases.map((alias) => (
                      <Typography.Text key={`${alias.raw_path}-${alias.first_seen?.id ?? ''}`} code>
                        {alias.raw_path}
                      </Typography.Text>
                    ))}
                  </Space>
                </div>
              )}
              {source.item_samples.length > 0 && (
                <div style={{ marginTop: 12 }}>
                  <Typography.Text strong>Контрольные строки товаров</Typography.Text>
                  <Space direction="vertical" size={4} style={{ width: '100%', marginTop: 6 }}>
                    {source.item_samples.map((sample) => (
                      <Typography.Text
                        key={`${sample.price.id}-${sample.source_sheet}-${sample.source_row}-${sample.source_name}`}
                        type="secondary"
                      >
                        {sample.price.filename} · {sample.source_sheet}!{sample.source_row}: {sample.source_article ? `${sample.source_article} · ` : ''}{sample.source_name}
                      </Typography.Text>
                    ))}
                  </Space>
                </div>
              )}
            </Card>
          ))}
        </Space>
      </Modal>
      <Modal
        title={`Просмотр прайса «${previewPrice?.filename ?? ''}»`}
        open={Boolean(previewPrice)}
        footer={null}
        width="95vw"
        destroyOnHidden
        onCancel={() => setPreviewPrice(undefined)}
      >
        {pricePreview.error && (
          <Alert
            showIcon
            type="error"
            message="Не удалось открыть прайс"
            description={extractApiError(pricePreview.error)}
            style={{ marginBottom: 12 }}
          />
        )}
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Space wrap>
            <Typography.Text strong>Лист</Typography.Text>
            <Select
              aria-label="Лист прайса"
              style={{ minWidth: 280 }}
              value={previewSheetIndex}
              loading={pricePreview.isPending}
              options={(pricePreview.data?.sheets ?? []).map((sheet) => ({
                value: sheet.index,
                label: `${sheet.name} · ${sheet.row_count.toLocaleString('ru-RU')} строк`,
              }))}
              onChange={(value) => {
                setPreviewSheetIndex(value);
                setPreviewPage(1);
              }}
            />
            {pricePreview.data && (
              <Typography.Text type="secondary">
                {pricePreview.data.total_rows.toLocaleString('ru-RU')} строк · {pricePreview.data.total_columns} колонок
              </Typography.Text>
            )}
          </Space>
          <Table<ReviewSourceRow>
            rowKey="row_index"
            size="small"
            bordered
            columns={previewColumns}
            dataSource={pricePreview.data?.rows ?? []}
            loading={pricePreview.isFetching}
            scroll={{ x: 'max-content', y: '55vh' }}
            pagination={{
              current: previewPage,
              pageSize: PREVIEW_PAGE_SIZE,
              total: pricePreview.data?.total_rows ?? 0,
              showSizeChanger: false,
              showTotal: (total) => `Всего строк: ${total.toLocaleString('ru-RU')}`,
              onChange: setPreviewPage,
            }}
            locale={{ emptyText: pricePreview.isFetching ? 'Загрузка…' : 'На листе нет строк' }}
          />
        </Space>
      </Modal>
      <Modal
        title="Новая общая категория"
        open={createOpen}
        okText="Создать"
        cancelText="Отмена"
        confirmLoading={createCategory.isPending}
        okButtonProps={{ disabled: !categoryName.trim() }}
        onOk={() => createCategory.mutate()}
        onCancel={() => setCreateOpen(false)}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text>Короткое поставщико-независимое название</Typography.Text>
          <Input value={categoryName} maxLength={512} onChange={(event) => setCategoryName(event.target.value)} />
          <Typography.Text>Родительская категория (необязательно)</Typography.Text>
          <Select
            allowClear
            showSearch
            filterOption={false}
            style={{ width: '100%' }}
            placeholder="Корневая категория"
            options={globalOptions}
            value={parentId ?? undefined}
            onSearch={setGlobalOptionQuery}
            onChange={(value) => setParentId(value ?? null)}
          />
        </Space>
      </Modal>
      <Modal
        title={`Объединить «${mergeSource?.path ?? ''}»`}
        open={Boolean(mergeSource)}
        okText="Перенести связи"
        cancelText="Отмена"
        confirmLoading={merge.isPending}
        okButtonProps={{ disabled: !mergeTargetId }}
        onOk={() => merge.mutate()}
        onCancel={() => { setMergeSource(undefined); setMergeTargetId(undefined); }}
      >
        <Typography.Paragraph>Куда перенести все категории поставщиков? Решение будет сохранено как ручное и не перезапишется при следующем импорте.</Typography.Paragraph>
        <Select
          showSearch
          filterOption={false}
          style={{ width: '100%' }}
          placeholder="Найдите целевую категорию"
          options={globalOptions.filter((option) => option.value !== mergeSource?.id)}
          value={mergeTargetId}
          onSearch={setGlobalOptionQuery}
          onChange={setMergeTargetId}
        />
      </Modal>
    </Space>
  );
}
