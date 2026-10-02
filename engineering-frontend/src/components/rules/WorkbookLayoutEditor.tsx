import { Alert, Card, Collapse, Descriptions, Select, Space, Tag, Typography } from 'antd';
import type { ReactNode } from 'react';
import type { Recipe, RecipeSheet, RuleValidationSample } from '../../api/types';
import { excelColumn } from '../review/reviewModel';
import MatrixPreviewModal from './MatrixPreviewModal';

const COLUMN_OPTIONS = Array.from({ length: 52 }, (_, index) => ({
  value: index,
  label: excelColumn(index),
  title: excelColumn(index),
}));

const PRICE_KIND_LABELS: Record<string, string> = {
  wholesale: 'оптовая',
  retail: 'розничная',
  dealer: 'дилерская',
  purchase: 'закупочная',
  other: 'цена',
};

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function column(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;
}

function columns(value: unknown): number[] {
  return Array.isArray(value)
    ? value.filter((entry): entry is number => (
        typeof entry === 'number' && Number.isInteger(entry) && entry >= 0
      ))
    : [];
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((entry): entry is Record<string, unknown> => (
        Boolean(entry) && typeof entry === 'object' && !Array.isArray(entry)
      ))
    : [];
}

function columnLabel(value: unknown): string {
  const index = column(value);
  return index === null ? 'не задана' : `колонка ${excelColumn(index)}`;
}

function columnLabels(value: unknown): string {
  const indexes = columns(value);
  return indexes.length > 0 ? indexes.map(excelColumn).join(', ') : 'не заданы';
}

function bindingColumn(value: unknown): string {
  return columnLabel(recordValue(value).column_index);
}

function bindingWithColumn(value: unknown, next: number | null, fallbackRow: unknown): Record<string, unknown> | null {
  if (next === null) return null;
  const binding = recordValue(value);
  const rowIndex = column(binding.row_index) ?? column(fallbackRow) ?? 0;
  return { ...binding, row_index: rowIndex, column_index: next };
}

function attributeName(value: unknown): string {
  const attribute = recordValue(value);
  return String(attribute.display_name ?? 'Характеристика');
}

function samplePriceText(sample: RuleValidationSample): string {
  if (sample.prices.length === 0) return 'цена не распознана';
  const visible = sample.prices.slice(0, 3).map((price) => {
    const amount = price.is_on_request ? 'по запросу' : (price.amount ?? '—');
    return `${PRICE_KIND_LABELS[price.kind] ?? price.kind}: ${amount}${price.currency ? ` ${price.currency}` : ''}`;
  });
  const rest = sample.prices.length - visible.length;
  return `${visible.join(' · ')}${rest > 0 ? ` · ещё ${rest}` : ''}`;
}

function ResultExample({ sample }: { sample?: RuleValidationSample }) {
  if (!sample) {
    return (
      <Alert
        type="info"
        showIcon
        message="Пример появится после dry-run"
        description="Запустите проверку выше: здесь будет показана реальная позиция, а не буквы колонок."
      />
    );
  }
  return (
    <Card
      size="small"
      type="inner"
      title="Пример реальной позиции"
      extra={<Typography.Text type="secondary">строка {sample.source_row}</Typography.Text>}
    >
      <Space direction="vertical" size={6} style={{ width: '100%' }}>
        <Typography.Text strong>{sample.name}</Typography.Text>
        <Space wrap size={[4, 4]}>
          {sample.article && <Tag>артикул: {sample.article}</Tag>}
          {sample.category && <Tag color="blue">категория: {sample.category}</Tag>}
          {sample.attributes.map((attribute, index) => (
            <Tag color="purple" key={`${attribute.code ?? attribute.name}:${index}`}>
              {attribute.name}: {attribute.raw_value}{attribute.unit ? ` ${attribute.unit}` : ''}
            </Tag>
          ))}
        </Space>
        <Typography.Text>{samplePriceText(sample)}</Typography.Text>
      </Space>
    </Card>
  );
}

function tabularShape(sheet: RecipeSheet): string {
  const active = sheet.columns.filter((candidate) => candidate.role !== 'ignore');
  const names = active.filter((candidate) => candidate.role === 'name').map((candidate) => candidate.index);
  const units = active.filter((candidate) => candidate.role === 'unit').map((candidate) => candidate.index);
  const prices = active.filter((candidate) => candidate.role === 'price').map((candidate) => candidate.index);
  const fields = active.filter((candidate) => !['name', 'unit', 'price'].includes(candidate.role));
  if (sheet.headerless) {
    return `таблица без шапки: название ${columnLabels(names)}, единица ${columnLabels(units)}, цена ${columnLabels(prices)}`;
  }
  if (names.length > 1) {
    const panels = names.map((name, index) => {
      const nextName = names[index + 1] ?? Number.POSITIVE_INFINITY;
      const panelPrices = prices.filter((price) => name < price && price < nextName);
      return `${excelColumn(name)} → цены ${panelPrices.map(excelColumn).join(', ')}`;
    });
    return `параллельные панели: ${panels.join('; ')}`;
  }
  const details = [
    names.length ? `название ${excelColumn(names[0])}` : null,
    units.length ? `единица ${excelColumn(units[0])}` : null,
    prices.length ? `цены ${prices.map(excelColumn).join(', ')}` : null,
    fields.length ? `доп. поля ${fields.map((field) => excelColumn(field.index)).join(', ')}` : null,
  ].filter(Boolean);
  return `одна широкая таблица: ${details.join(', ')}`;
}

interface ColumnSelectProps {
  ariaLabel: string;
  value: unknown;
  disabled: boolean;
  allowClear?: boolean;
  onChange: (value: number | null) => void;
}

function ColumnSelect({ ariaLabel, value, disabled, allowClear, onChange }: ColumnSelectProps) {
  return (
    <Select
      aria-label={ariaLabel}
      value={column(value)}
      options={COLUMN_OPTIONS}
      disabled={disabled}
      allowClear={allowClear}
      placeholder="Не задана"
      style={{ width: 150 }}
      onChange={(next) => onChange(typeof next === 'number' ? next : null)}
    />
  );
}

interface ColumnMultiSelectProps {
  ariaLabel: string;
  value: unknown;
  disabled: boolean;
  onChange: (value: number[]) => void;
}

function ColumnMultiSelect({ ariaLabel, value, disabled, onChange }: ColumnMultiSelectProps) {
  return (
    <Select
      aria-label={ariaLabel}
      mode="multiple"
      value={columns(value)}
      options={COLUMN_OPTIONS}
      disabled={disabled}
      style={{ width: 'min(100%, 480px)' }}
      onChange={onChange}
    />
  );
}

function Field({
  title,
  help,
  children,
}: {
  title: string;
  help: string;
  children: ReactNode;
}) {
  return (
    <Space direction="vertical" size={2} style={{ minWidth: 220 }}>
      <Typography.Text strong>{title}</Typography.Text>
      <Typography.Text type="secondary">{help}</Typography.Text>
      {children}
    </Space>
  );
}

interface WorkbookLayoutEditorProps {
  ruleId: string;
  priceVersionId: string | null;
  recipe: Recipe;
  editable: boolean;
  samples?: RuleValidationSample[];
  onChange: (recipe: Recipe) => void;
}

export default function WorkbookLayoutEditor({
  ruleId,
  priceVersionId,
  recipe,
  editable,
  samples = [],
  onChange,
}: WorkbookLayoutEditorProps) {
  const hydrant = recordValue(recipe.hydrant_matrix);
  const simplePanel = recordValue(hydrant.simple_panel);
  const service = recordValue(recipe.service_matrix);
  const linear = recordValue(service.linear);
  const sign = recordValue(service.sign);
  const generatedRegions = records(recipe.matrix_regions);
  const generatedSheets = new Set(generatedRegions.map((region) => (
    String(recordValue(region.match).name_pattern ?? '')
  )));
  const compositeBySheet = new Map<string, typeof recipe.sheets>();
  recipe.sheets.forEach((sheet) => {
    const name = typeof sheet.match?.name_pattern === 'string'
      ? sheet.match.name_pattern.trim().replace(/\s+/g, ' ')
      : '';
    if (!name) return;
    const members = compositeBySheet.get(name) ?? [];
    members.push(sheet);
    compositeBySheet.set(name, members);
  });
  const legacyServiceSheet = String(service.sheet ?? '').trim().replace(/\s+/g, ' ');
  const compositeSheets = [...compositeBySheet.entries()].filter(([name, members]) => (
    members.length > 1 && name !== legacyServiceSheet
  ));

  const sampleForSheet = (sheet: unknown, preferAttributes = false) => (
    (preferAttributes
      ? samples.find((sample) => sample.source_sheet === sheet && sample.attributes.length > 0)
      : undefined)
    ?? samples.find((sample) => sample.source_sheet === sheet)
  );
  const patchHydrant = (patch: Record<string, unknown>) => {
    onChange({ ...recipe, hydrant_matrix: { ...hydrant, ...patch } });
  };
  const patchSimplePanel = (patch: Record<string, unknown>) => {
    patchHydrant({ simple_panel: { ...simplePanel, ...patch } });
  };
  const patchService = (patch: Record<string, unknown>) => {
    onChange({ ...recipe, service_matrix: { ...service, ...patch } });
  };
  const patchLinear = (patch: Record<string, unknown>) => {
    patchService({ linear: { ...linear, ...patch } });
  };
  const patchSign = (patch: Record<string, unknown>) => {
    patchService({ sign: { ...sign, ...patch } });
  };
  const patchGeneratedBlock = (
    regionIndex: number,
    blockIndex: number,
    patch: Record<string, unknown>,
  ) => {
    const nextRegions = generatedRegions.map((region, candidateRegionIndex) => {
      if (candidateRegionIndex !== regionIndex) return region;
      const nextBlocks = records(region.blocks).map((block, candidateBlockIndex) => (
        candidateBlockIndex === blockIndex ? { ...block, ...patch } : block
      ));
      return { ...region, blocks: nextBlocks };
    });
    onChange({ ...recipe, matrix_regions: nextRegions });
  };

  return (
    <Card size="small" type="inner" title="Специальные форматы листов">
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Alert
          type="info"
          showIcon
          message="Сначала проверьте результат"
          description="Ниже показано, какие позиции попадут в базу. Буквы Excel-колонок нужны только при исправлении ошибочной привязки и скрыты в «Технической настройке»."
        />

        {generatedRegions.map((region, regionIndex) => {
          const match = recordValue(region.match);
          const sheet = String(match.name_pattern ?? `лист №${Number(match.or_index ?? 0) + 1}`);
          const blocks = records(region.blocks);
          return (
            <Card
              key={String(region.source_section_id ?? regionIndex)}
              size="small"
              title="Матричная таблица: ячейка цены → отдельная позиция"
              extra={<Tag color="blue">Лист «{sheet}»</Tag>}
            >
              <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                <div>
                  <Typography.Title level={5} style={{ margin: 0 }}>Что получится после разбора</Typography.Title>
                  <Typography.Paragraph type="secondary" style={{ margin: '4px 0 0' }}>
                    Каждая заполненная ценовая ячейка станет отдельной товарной позицией. Значения осей будут характеристиками, а не ещё одними ценами.
                  </Typography.Paragraph>
                </div>
                <Descriptions bordered size="small" column={{ xs: 1, sm: 2, lg: 3 }}>
                  <Descriptions.Item label="Проверено ценовых ячеек">
                    {String(region.validated_measure_cells ?? '—')}
                  </Descriptions.Item>
                  <Descriptions.Item label="Матричных блоков">{blocks.length}</Descriptions.Item>
                  <Descriptions.Item label="Режим">одна цена на позицию</Descriptions.Item>
                </Descriptions>
                <ResultExample sample={sampleForSheet(sheet, true)} />
                <MatrixPreviewModal
                  ruleId={ruleId}
                  priceVersionId={priceVersionId}
                  sourceSectionId={String(region.source_section_id ?? '')}
                  recipe={recipe}
                />
                <Collapse
                  items={[{
                    key: 'bindings',
                    label: 'Как связаны поля и оси',
                    children: (
                      <Space direction="vertical" size="small" style={{ width: '100%' }}>
                        {blocks.map((block, blockIndex) => {
                          const rowAxis = recordValue(block.row_axis);
                          const columnAxis = recordValue(block.column_axis);
                          return (
                            <Descriptions
                              key={String(block.block_ref ?? blockIndex)}
                              bordered
                              size="small"
                              title={`Блок ${blockIndex + 1}`}
                              column={{ xs: 1, sm: 2 }}
                            >
                              <Descriptions.Item label="Название">{bindingColumn(block.name)}</Descriptions.Item>
                              <Descriptions.Item label="Артикул">{bindingColumn(block.article)}</Descriptions.Item>
                              {Object.keys(rowAxis).length > 0 && (
                                <Descriptions.Item label={attributeName(rowAxis)}>
                                  {bindingColumn(block.row_axis_source)}{rowAxis.canonical_unit ? `, ${String(rowAxis.canonical_unit)}` : ''}
                                </Descriptions.Item>
                              )}
                              <Descriptions.Item label={attributeName(columnAxis)}>
                                заголовки {columnLabels(block.column_axis_columns)}{columnAxis.canonical_unit ? `, ${String(columnAxis.canonical_unit)}` : ''}
                              </Descriptions.Item>
                            </Descriptions>
                          );
                        })}
                      </Space>
                    ),
                  }, {
                    key: 'technical-matrix',
                    label: 'Техническая настройка колонок',
                    children: (
                      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                        <Alert
                          type="info"
                          showIcon
                          message="Изменения применяются к текущему черновику"
                          description="После исправления привязок откройте просмотр ещё раз, затем сохраните черновик и выполните dry-run."
                        />
                        {blocks.map((block, blockIndex) => {
                          const nameBinding = recordValue(block.name);
                          const rowAxis = recordValue(block.row_axis);
                          const nameRow = nameBinding.row_index;
                          return (
                            <Card key={String(block.block_ref ?? blockIndex)} size="small" title={`Блок ${blockIndex + 1}`}>
                              <Space wrap align="start" size="large">
                                <Field title="Название" help="Колонка с базовым названием позиции.">
                                  <ColumnSelect
                                    ariaLabel={`Название блока ${blockIndex + 1}`}
                                    value={nameBinding.column_index}
                                    disabled={!editable}
                                    onChange={(next) => patchGeneratedBlock(regionIndex, blockIndex, {
                                      name: bindingWithColumn(block.name, next, nameRow),
                                    })}
                                  />
                                </Field>
                                <Field title="Артикул" help="Необязательная колонка артикула.">
                                  <ColumnSelect
                                    ariaLabel={`Артикул блока ${blockIndex + 1}`}
                                    value={recordValue(block.article).column_index}
                                    allowClear
                                    disabled={!editable}
                                    onChange={(next) => patchGeneratedBlock(regionIndex, blockIndex, {
                                      article: bindingWithColumn(block.article, next, nameRow),
                                    })}
                                  />
                                </Field>
                                <Field title="Категория" help="Ячейка с категорией матричного блока.">
                                  <ColumnSelect
                                    ariaLabel={`Категория блока ${blockIndex + 1}`}
                                    value={recordValue(block.category).column_index}
                                    allowClear
                                    disabled={!editable}
                                    onChange={(next) => patchGeneratedBlock(regionIndex, blockIndex, {
                                      category: bindingWithColumn(block.category, next, nameRow),
                                    })}
                                  />
                                </Field>
                                {Object.keys(rowAxis).length > 0 && (
                                  <Field title={attributeName(rowAxis)} help="Колонка значения оси в строке товара.">
                                    <ColumnSelect
                                      ariaLabel={`Ось строки блока ${blockIndex + 1}`}
                                      value={recordValue(block.row_axis_source).column_index}
                                      disabled={!editable}
                                      onChange={(next) => patchGeneratedBlock(regionIndex, blockIndex, {
                                        row_axis_source: bindingWithColumn(block.row_axis_source, next, nameRow),
                                      })}
                                    />
                                  </Field>
                                )}
                                <Field title="Ценовые колонки" help="Каждая заполненная ячейка этих колонок станет отдельной позицией.">
                                  <ColumnMultiSelect
                                    ariaLabel={`Ценовые колонки блока ${blockIndex + 1}`}
                                    value={block.column_axis_columns}
                                    disabled={!editable}
                                    onChange={(next) => patchGeneratedBlock(regionIndex, blockIndex, { column_axis_columns: next })}
                                  />
                                </Field>
                              </Space>
                            </Card>
                          );
                        })}
                      </Space>
                    ),
                  }]}
                />
              </Space>
            </Card>
          );
        })}

        {compositeSheets.length > 0 && (
          <Card
            size="small"
            title="Составной лист: несколько форматов таблиц"
            extra={<Tag color="purple">листов: {compositeSheets.length}</Tag>}
          >
            <Space direction="vertical" size="small" style={{ width: '100%' }}>
              <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
                На одном листе найдены разные устойчивые структуры таблиц. Поэтому это не дубли:
                каждый шаблон применяется
                по структуре и заголовкам, без сохранённых номеров строк; его колонки и цены не смешиваются
                с соседним шаблоном. Полные маппинги редактируются выше.
              </Typography.Paragraph>
              <Descriptions bordered size="small" column={1}>
                {compositeSheets.map(([name, members]) => (
                  <Descriptions.Item key={name} label={name}>
                    <Space direction="vertical" size={4}>
                      <Tag>{members.length} шаблона разбора</Tag>
                      {members.map((member) => (
                        <Typography.Text key={String(member.source_section_id ?? recipe.sheets.indexOf(member))}>
                          Табличный шаблон {recipe.sheets.indexOf(member) + 1}: {tabularShape(member)}
                        </Typography.Text>
                      ))}
                    </Space>
                  </Descriptions.Item>
                ))}
              </Descriptions>
            </Space>
          </Card>
        )}

        {Object.keys(hydrant).length > 0 && !generatedSheets.has(String(hydrant.sheet ?? '')) && (
          <Card
            size="small"
            title="Подставки под гидрант"
            extra={<Space wrap size={4}><Tag color="blue">{String(hydrant.sheet ?? '—')}</Tag><Tag>2 области</Tag></Space>}
          >
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <div>
                <Typography.Title level={5} style={{ margin: 0 }}>Что получится после разбора</Typography.Title>
                <Typography.Paragraph type="secondary" style={{ margin: '4px 0 0' }}>
                  Основная матрица и нижняя обычная таблица разбираются независимо и не смешивают названия и цены.
                </Typography.Paragraph>
              </div>
              <Alert
                type="warning"
                showIcon
                message="Этот черновик использует старый режим матрицы"
                description="Одна строка становится одной позицией с несколькими ценами. В новых matrix-unfold правилах каждая ценовая ячейка становится отдельной позицией."
              />
              <Descriptions bordered size="small" column={{ xs: 1, sm: 2 }}>
                <Descriptions.Item label="Основная матрица">
                  название — {columnLabel(hydrant.name)}; вариант — {columnLabel(hydrant.outlet)}
                </Descriptions.Item>
                <Descriptions.Item label="Цены матрицы">колонки {columnLabels(hydrant.price_columns)}</Descriptions.Item>
                <Descriptions.Item label="Нижняя таблица">название — {columnLabel(simplePanel.name)}</Descriptions.Item>
                <Descriptions.Item label="Цены нижней таблицы">колонки {columnLabels(simplePanel.prices)}</Descriptions.Item>
              </Descriptions>
              <ResultExample sample={sampleForSheet(hydrant.sheet)} />
              <Collapse
                items={[{
                  key: 'technical-hydrant',
                  label: 'Техническая настройка колонок',
                  children: (
                    <Space direction="vertical" size="large" style={{ width: '100%' }}>
                      <Space wrap align="start" size="large">
                        <Field title="Название товара" help="Колонка с базовым названием.">
                          <ColumnSelect ariaLabel="Наименование матрицы гидрантов" value={hydrant.name} disabled={!editable} onChange={(name) => patchHydrant({ name })} />
                        </Field>
                        <Field title="Единица" help="Единица измерения товара.">
                          <ColumnSelect ariaLabel="Единица матрицы гидрантов" value={hydrant.unit} disabled={!editable} onChange={(unit) => patchHydrant({ unit })} />
                        </Field>
                        <Field title="Вариант из строки" help="Значение, которое уточняет название.">
                          <ColumnSelect ariaLabel="Ось варианта матрицы гидрантов" value={hydrant.outlet} disabled={!editable} onChange={(outlet) => patchHydrant({ outlet })} />
                        </Field>
                      </Space>
                      <Field title="Цены основной матрицы" help="Колонки, из которых берутся числовые цены и «по запросу».">
                        <ColumnMultiSelect ariaLabel="Цены матрицы гидрантов" value={hydrant.price_columns} disabled={!editable} onChange={(priceColumns) => patchHydrant({ price_columns: priceColumns })} />
                      </Field>
                      <Typography.Text strong>Нижняя обычная таблица</Typography.Text>
                      <Space wrap align="start" size="large">
                        <Field title="Название" help="Название позиции.">
                          <ColumnSelect ariaLabel="Наименование нижней таблицы гидрантов" value={simplePanel.name} disabled={!editable} onChange={(name) => patchSimplePanel({ name })} />
                        </Field>
                        <Field title="Единица" help="Единица измерения.">
                          <ColumnSelect ariaLabel="Единица нижней таблицы гидрантов" value={simplePanel.unit} disabled={!editable} onChange={(unit) => patchSimplePanel({ unit })} />
                        </Field>
                        <Field title="Цены" help="Колонки цен этой обычной таблицы.">
                          <ColumnMultiSelect ariaLabel="Цены нижней таблицы гидрантов" value={simplePanel.prices} disabled={!editable} onChange={(prices) => patchSimplePanel({ prices })} />
                        </Field>
                      </Space>
                    </Space>
                  ),
                }]}
              />
            </Space>
          </Card>
        )}

        {Object.keys(service).length > 0 && !generatedSheets.has(String(service.sheet ?? '')) && (
          <Card
            size="small"
            title="Составной лист: две независимые таблицы"
            extra={<Space wrap size={4}><Tag color="blue">{String(service.sheet ?? '—')}</Tag><Tag>2 набора позиций</Tag></Space>}
          >
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <div>
                <Typography.Title level={5} style={{ margin: 0 }}>Что получится после разбора</Typography.Title>
                <Typography.Paragraph type="secondary" style={{ margin: '4px 0 0' }}>
                  Это не матрица: каждая строка даёт позицию. Лист содержит два независимых набора позиций — оборудование/услуги с одной ценой и знаки с ценами по материалам. Их колонки не смешиваются.
                </Typography.Paragraph>
              </div>
              <ResultExample sample={sampleForSheet(service.sheet)} />
              <Collapse
                items={[{
                  key: 'bindings-service',
                  label: 'Как связаны поля и оси',
                  children: (
                    <Descriptions bordered size="small" column={{ xs: 1, sm: 2 }}>
                      <Descriptions.Item label="Оборудование и услуги">
                        название — {columnLabel(linear.name)}; единица — {columnLabel(linear.unit)}; цена — {columnLabel(linear.price)}
                      </Descriptions.Item>
                      <Descriptions.Item label="Знаки">
                        название — {columnLabel(sign.name)}; цены по материалам — {columnLabels(sign.price_columns)}
                      </Descriptions.Item>
                    </Descriptions>
                  ),
                }, {
                  key: 'technical-service',
                  label: 'Техническая настройка колонок',
                  children: (
                    <Space direction="vertical" size="large" style={{ width: '100%' }}>
                      <Typography.Text strong>Оборудование и услуги</Typography.Text>
                      <Space wrap align="start" size="large">
                        <Field title="Название" help="Колонка с названием позиции.">
                          <ColumnSelect ariaLabel="Наименование линейных позиций обслуживания" value={linear.name} disabled={!editable} onChange={(name) => patchLinear({ name })} />
                        </Field>
                        <Field title="Единица" help="Единица измерения.">
                          <ColumnSelect ariaLabel="Единица линейных позиций обслуживания" value={linear.unit} disabled={!editable} onChange={(unit) => patchLinear({ unit })} />
                        </Field>
                        <Field title="Цена" help="Одна цена позиции.">
                          <ColumnSelect ariaLabel="Цена линейных позиций обслуживания" value={linear.price} disabled={!editable} onChange={(price) => patchLinear({ price })} />
                        </Field>
                      </Space>
                      <Typography.Text strong>Знаки</Typography.Text>
                      <Space wrap align="start" size="large">
                        <Field title="Название" help="Название знака.">
                          <ColumnSelect ariaLabel="Наименование знаков" value={sign.name} disabled={!editable} onChange={(name) => patchSign({ name })} />
                        </Field>
                        <Field title="Цены по материалам" help="Каждая колонка содержит свою цену.">
                          <ColumnMultiSelect ariaLabel="Цены знаков" value={sign.price_columns} disabled={!editable} onChange={(priceColumns) => patchSign({ price_columns: priceColumns })} />
                        </Field>
                      </Space>
                    </Space>
                  ),
                }]}
              />
            </Space>
          </Card>
        )}
      </Space>
    </Card>
  );
}
