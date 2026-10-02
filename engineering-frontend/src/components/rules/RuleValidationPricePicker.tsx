import { DownloadOutlined, PlayCircleOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Card, Collapse, Descriptions, Input, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import { exportParseRuleValidation, fetchImportedPriceVersions } from '../../api/client';
import type { ImportedPriceVersion, ParseRule, RuleValidationOut } from '../../api/types';
import { formatDate } from '../importFormat';
import { excelColumn } from '../review/reviewModel';

const PAGE_SIZE = 10;

function loaderLabel(version: ImportedPriceVersion): string {
  const loader = version.loader?.trim().toLowerCase();
  if (loader === 'pdf_text') return 'PDF';
  if (loader) return loader.toUpperCase();
  const extension = version.filename.split('.').pop();
  return extension ? extension.toUpperCase() : '—';
}

function loaderMatches(ruleLoader: string, version: ImportedPriceVersion): boolean {
  const versionLoader = version.loader?.trim().toLowerCase();
  if (versionLoader) return versionLoader === ruleLoader;
  const extension = version.filename.split('.').pop()?.toLowerCase();
  return extension === ruleLoader || (extension === 'pdf' && ruleLoader === 'pdf_text');
}

const PRICE_KIND_LABELS: Record<string, string> = {
  wholesale: 'оптовая', retail: 'розничная', dealer: 'дилерская',
  purchase: 'закупочная', other: 'цена',
};

const METRIC_LABELS: Record<string, string> = {
  rows_accepted: 'Позиций создано',
  items_with_price: 'Позиций с ценой',
  source_items: 'Позиций в исходном импорте',
  items_delta: 'Разница',
  blocking_errors: 'Блокирующих ошибок',
  errors_count: 'Ошибок',
  warnings_count: 'Предупреждений',
  no_name_rows: 'Строк без названия',
  price_recognized_share: 'Доля распознанных цен',
  quality_score: 'Качество разбора',
};

const PRIMARY_METRICS = [
  'items_with_price', 'rows_accepted', 'source_items',
  'blocking_errors', 'errors_count', 'warnings_count',
];

function metricText(key: string, value: unknown): string {
  if (typeof value === 'number' && ['price_recognized_share', 'quality_score'].includes(key)) {
    return `${Math.round(value * 1000) / 10}%`;
  }
  if (typeof value === 'number') return value.toLocaleString('ru-RU');
  if (value === null || value === undefined) return '—';
  return String(value);
}

function priceText(price: NonNullable<RuleValidationOut['samples']>[number]['prices'][number]): string {
  const value = price.is_on_request ? 'по запросу' : (price.amount ?? '—');
  const currency = price.currency ? ` ${price.currency}` : '';
  const source = price.source_column_index === null
    ? ''
    : ` · колонка ${excelColumn(price.source_column_index)}`;
  return `${PRICE_KIND_LABELS[price.kind] ?? price.kind}: ${value}${currency}${source}`;
}

interface RuleValidationPricePickerProps {
  rule: ParseRule;
  selectedVersionId?: string;
  selectedVersion?: ImportedPriceVersion;
  onSelect: (version: ImportedPriceVersion) => void;
  onValidate: () => void;
  validating: boolean;
  validationResult?: RuleValidationOut;
}

export default function RuleValidationPricePicker({
  rule,
  selectedVersionId,
  selectedVersion,
  onSelect,
  onValidate,
  validating,
  validationResult,
}: RuleValidationPricePickerProps) {
  const [searchInput, setSearchInput] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [showAllSamples, setShowAllSamples] = useState(false);
  const [exporting, setExporting] = useState<'json' | 'xlsx'>();
  const versionsQuery = useQuery({
    queryKey: ['price-versions-for-validate', rule.supplier_id, query, page],
    queryFn: () => fetchImportedPriceVersions({
      query: query || undefined,
      supplierId: rule.supplier_id ?? undefined,
      hasItems: true,
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
    }),
  });
  const sourceVersionId = rule.created_from_version_id;
  const rows = (versionsQuery.data?.items ?? []).filter(
    (version) => rule.supplier_id === null || version.supplier_id === rule.supplier_id,
  );
  const total = versionsQuery.data?.total ?? 0;
  const samples = validationResult?.samples ?? [];
  const visibleSamples = showAllSamples ? samples : samples.slice(0, 6);

  const downloadResult = async (format: 'json' | 'xlsx') => {
    if (!selectedVersionId) return;
    setExporting(format);
    try {
      const blob = await exportParseRuleValidation(rule.id, selectedVersionId, format);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      const sourceName = selectedVersion?.filename ?? rule.source_version?.filename ?? 'dry-run';
      anchor.href = url;
      anchor.download = `${sourceName.replace(/\.[^.]+$/, '')} — результат разбора.${format}`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } finally {
      setExporting(undefined);
    }
  };

  const columns: ColumnsType<ImportedPriceVersion> = [
    {
      title: 'Файл',
      dataIndex: 'filename',
      width: 300,
      render: (filename: string, version) => (
        <Space size={6} wrap>
          <Typography.Text strong>{filename}</Typography.Text>
          {version.id === sourceVersionId && <Tag color="blue">исходный</Tag>}
        </Space>
      ),
    },
    { title: 'Поставщик', dataIndex: 'supplier_name', width: 180 },
    {
      title: 'Тип',
      width: 90,
      render: (_, version) => <Tag>{loaderLabel(version)}</Tag>,
    },
    {
      title: 'Версия',
      width: 105,
      render: (_, version) => `v${version.version_no}`,
    },
    {
      title: 'Загружен',
      dataIndex: 'created_at',
      width: 170,
      render: (value: string) => formatDate(value),
    },
    {
      title: 'Позиции',
      dataIndex: 'item_count',
      align: 'right',
      width: 105,
      render: (value: number) => value.toLocaleString('ru-RU'),
    },
    {
      title: '',
      key: 'action',
      fixed: 'right',
      width: 125,
      render: (_, version) => {
        const selected = version.id === selectedVersionId;
        const compatible = (
          loaderMatches(rule.loader, version)
          && (rule.supplier_id === null || version.supplier_id === rule.supplier_id)
        );
        return (
          <Button
            size="small"
            type={selected ? 'primary' : 'default'}
            disabled={selected || !compatible}
            onClick={() => onSelect(version)}
          >
            {selected ? 'Выбран' : compatible ? 'Выбрать' : 'Другой тип'}
          </Button>
        );
      },
    },
  ];

  return (
    <Card size="small" title="Проверка на прайсе (dry-run)">
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {rule.source_version ? (
          <Alert
            showIcon
            type="info"
            message={(
              <Space wrap>
                <Typography.Text strong>Исходный прайс правила</Typography.Text>
                {selectedVersionId === rule.source_version.id && <Tag color="green">выбран автоматически</Tag>}
              </Space>
            )}
            description={(
              <Descriptions size="small" column={{ xs: 1, sm: 2, md: 3 }}>
                <Descriptions.Item label="Файл">{rule.source_version.filename}</Descriptions.Item>
                <Descriptions.Item label="Поставщик">{rule.source_version.supplier_name}</Descriptions.Item>
                <Descriptions.Item label="Тип">{loaderLabel(rule.source_version)}</Descriptions.Item>
                <Descriptions.Item label="Версия">v{rule.source_version.version_no}</Descriptions.Item>
                <Descriptions.Item label="Загружен">{formatDate(rule.source_version.created_at)}</Descriptions.Item>
                <Descriptions.Item label="Позиции">{rule.source_version.item_count.toLocaleString('ru-RU')}</Descriptions.Item>
              </Descriptions>
            )}
          />
        ) : (
          <Alert showIcon type="warning" message="У правила не найден исходный прайс" />
        )}

        {selectedVersion && selectedVersion.id !== sourceVersionId && (
          <Alert
            showIcon
            type="warning"
            message={`Для проверки выбран другой файл: ${selectedVersion.filename}`}
            description="Исходный прайс не изменится — этот выбор относится только к текущему dry-run."
          />
        )}

        <Space wrap>
          <Button
            type="primary"
            icon={<PlayCircleOutlined />}
            disabled={!selectedVersionId}
            loading={validating}
            onClick={onValidate}
          >
            Выполнить dry-run
          </Button>
          <Typography.Text type="secondary">
            {selectedVersion ? `Будет проверен файл «${selectedVersion.filename}»` : 'Сначала выберите прайс'}
          </Typography.Text>
        </Space>

        {validationResult && (
          <Space direction="vertical" size="small" style={{ width: '100%' }}>
            <Alert
              showIcon
              type={validationResult.passed ? 'success' : 'warning'}
              message={validationResult.passed ? 'Dry-run пройден — правило готово к активации' : 'Dry-run выполнен, но проверка не пройдена'}
              description={validationResult.passed
                ? 'Ниже показаны итоговые числа и реальные примеры позиций.'
                : 'Исправьте ошибки в правиле и повторите проверку.'}
            />
            <Descriptions bordered size="small" column={{ xs: 1, sm: 2, lg: 3 }}>
              {PRIMARY_METRICS
                .filter((key) => key in validationResult.metrics)
                .map((key) => (
                  <Descriptions.Item key={key} label={METRIC_LABELS[key]}>
                    {metricText(key, validationResult.metrics[key])}
                  </Descriptions.Item>
                ))}
            </Descriptions>
            {validationResult.passed && selectedVersionId && (
              <Space wrap>
                <Button
                  icon={<DownloadOutlined />}
                  loading={exporting === 'json'}
                  disabled={Boolean(exporting && exporting !== 'json')}
                  onClick={() => void downloadResult('json')}
                >
                  Скачать все позиции в JSON
                </Button>
                <Button
                  icon={<DownloadOutlined />}
                  loading={exporting === 'xlsx'}
                  disabled={Boolean(exporting && exporting !== 'xlsx')}
                  onClick={() => void downloadResult('xlsx')}
                >
                  Скачать проверочный XLSX
                </Button>
                <Typography.Text type="secondary">
                  XLSX: категории и все позиции на одном листе; ширина колонок подбирается автоматически.
                </Typography.Text>
              </Space>
            )}
            <Collapse
              size="small"
              items={[{
                key: 'technical-metrics',
                label: 'Технические метрики',
                children: (
                  <Descriptions size="small" column={{ xs: 1, sm: 2 }}>
                    {Object.entries(validationResult.metrics)
                      .filter(([key]) => !PRIMARY_METRICS.includes(key))
                      .map(([key, value]) => (
                        <Descriptions.Item key={key} label={METRIC_LABELS[key] ?? key}>
                          {metricText(key, value)}
                        </Descriptions.Item>
                      ))}
                  </Descriptions>
                ),
              }]}
            />
          </Space>
        )}

        {samples.length > 0 && (
          <div>
            <Typography.Title level={5} style={{ marginBottom: 4 }}>
              Примеры получившихся позиций
            </Typography.Title>
            <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>
              Это ограниченная выборка dry-run. Для матриц показывает, как оси ячейки
              стали характеристиками товара.
            </Typography.Paragraph>
            <Space direction="vertical" size="small" style={{ width: '100%' }}>
              {visibleSamples.map((sample, index) => (
                <Card
                  key={`${sample.source_sheet}:${sample.source_row}:${sample.name}:${index}`}
                  size="small"
                  type="inner"
                  title={sample.name}
                  extra={<Typography.Text type="secondary">{sample.source_sheet} · строка {sample.source_row}</Typography.Text>}
                >
                  <Space direction="vertical" size={4} style={{ width: '100%' }}>
                    <Space wrap>
                      {sample.article && <Tag>артикул: {sample.article}</Tag>}
                      {sample.unit && <Tag>единица: {sample.unit}</Tag>}
                      {sample.category && <Tag color="blue">категория: {sample.category}</Tag>}
                    </Space>
                    {sample.attributes.length > 0 && (
                      <Space wrap>
                        {sample.attributes.map((attribute, attributeIndex) => (
                          <Tag color="purple" key={`${attribute.code ?? attribute.name}:${attributeIndex}`}>
                            {attribute.name}: {attribute.raw_value}{attribute.unit ? ` ${attribute.unit}` : ''}
                          </Tag>
                        ))}
                      </Space>
                    )}
                    {sample.prices.map((price, priceIndex) => (
                      <Typography.Text strong key={`${price.kind}:${price.source_column_index}:${priceIndex}`}>
                        {priceText(price)}
                      </Typography.Text>
                    ))}
                  </Space>
                </Card>
              ))}
              {samples.length > 6 && (
                <Button type="link" onClick={() => setShowAllSamples((value) => !value)}>
                  {showAllSamples ? 'Свернуть примеры' : `Показать ещё ${samples.length - 6}`}
                </Button>
              )}
            </Space>
          </div>
        )}

        <div>
          <Typography.Title level={5} style={{ marginBottom: 4 }}>Другие прайсы для проверки</Typography.Title>
          <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>
            Это необязательный список для проверки совместимости правила с другим файлом. Исходный прайс уже выбран выше.
          </Typography.Paragraph>
          <Space direction="vertical" size="small" style={{ width: '100%' }}>
            <Space wrap style={{ justifyContent: 'space-between', width: '100%' }}>
              <Input.Search
                aria-label="Поиск по прайсам"
                allowClear
                value={searchInput}
                placeholder="Имя файла, поставщик или код"
                enterButton="Найти"
                onChange={(event) => {
                  const value = event.target.value;
                  setSearchInput(value);
                  if (!value && query) {
                    setQuery('');
                    setPage(1);
                  }
                }}
                onSearch={(value) => {
                  setQuery(value.trim());
                  setPage(1);
                }}
                style={{ width: 'min(100%, 520px)' }}
              />
              <Typography.Text type="secondary">{total.toLocaleString('ru-RU')} прайсов</Typography.Text>
            </Space>
            <Table<ImportedPriceVersion>
              rowKey="id"
              size="small"
              columns={columns}
              dataSource={rows}
              loading={versionsQuery.isFetching}
              scroll={{ x: 1080 }}
              locale={{ emptyText: query ? 'По запросу ничего не найдено' : 'Прайсы не найдены' }}
              pagination={{
                current: page,
                pageSize: PAGE_SIZE,
                total,
                showSizeChanger: false,
                hideOnSinglePage: true,
                onChange: setPage,
              }}
            />
          </Space>
        </div>
      </Space>
    </Card>
  );
}
