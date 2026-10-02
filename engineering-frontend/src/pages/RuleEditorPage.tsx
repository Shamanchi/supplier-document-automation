import {
  ArrowLeftOutlined,
  CheckCircleOutlined,
  CopyOutlined,
  DeleteOutlined,
  PlusOutlined,
  SaveOutlined,
  StopOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Collapse,
  Descriptions,
  Input,
  InputNumber,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  activateParseRule,
  autoDraftParseRule,
  cloneParseRule,
  deprecateParseRule,
  extractApiError,
  fetchParseRule,
  rebindParseRuleMatrix,
  updateParseRule,
  validateParseRule,
} from '../api/client';
import type {
  AttributeValueKind,
  ImportedPriceVersion,
  ParseRule,
  Recipe,
  RecipeColumn,
  RecipeColumnRole,
  RecipePriceDescriptor,
  RecipeSheet,
  RuleAutoDraftOut,
  RuleValidationOut,
} from '../api/types';
import { useAuth } from '../auth/AuthContext';
import { formatDate } from '../components/importFormat';
import { excelColumn, REVIEW_ROLES } from '../components/review/reviewModel';
import RuleValidationPricePicker from '../components/rules/RuleValidationPricePicker';
import WorkbookLayoutEditor from '../components/rules/WorkbookLayoutEditor';

const STATUS_LABELS: Record<ParseRule['status'], string> = {
  draft: 'Черновик',
  active: 'Активно',
  deprecated: 'Архив',
};

const ROLE_OPTIONS = REVIEW_ROLES as { value: RecipeColumnRole; label: string }[];

const PRICE_KIND_OPTIONS = [
  { value: 'wholesale', label: 'Оптовая' },
  { value: 'retail', label: 'Розничная' },
  { value: 'dealer', label: 'Дилерская' },
  { value: 'purchase', label: 'Закупочная' },
  { value: 'other', label: 'Другая' },
];

const VAT_OPTIONS = [
  { value: 'included', label: 'С НДС' },
  { value: 'excluded', label: 'Без НДС' },
  { value: 'unknown', label: 'Не указано' },
];

const VALUE_KIND_OPTIONS: { value: AttributeValueKind; label: string }[] = [
  { value: 'text', label: 'Текст' },
  { value: 'integer', label: 'Целое число' },
  { value: 'decimal', label: 'Дробное число' },
  { value: 'boolean', label: 'Да / Нет' },
  { value: 'date', label: 'Дата' },
  { value: 'datetime', label: 'Дата и время' },
  { value: 'option', label: 'Вариант' },
];

const ATTRIBUTE_ROLES: RecipeColumnRole[] = [
  'attribute', 'manufacturer', 'discount_percent', 'weight', 'volume',
  'stock', 'lead_time', 'quantity_threshold', 'photo',
];

function normalizedSheetLabel(value: unknown): string {
  return typeof value === 'string'
    ? value.trim().replace(/\s+/g, ' ').toLocaleLowerCase('ru-RU')
    : '';
}

function categoryComesFromSheetName(sheet: RecipeSheet): boolean {
  if (sheet.default_category_source === 'sheet_name') return true;
  const sheetName = normalizedSheetLabel(sheet.match?.name_pattern);
  const category = normalizedSheetLabel(sheet.default_category_path);
  return Boolean(sheetName && category && sheetName === category);
}

function displaySheetName(sheet: RecipeSheet, index: number): string {
  const name = typeof sheet.match?.name_pattern === 'string'
    ? sheet.match.name_pattern.trim().replace(/\s+/g, ' ')
    : '';
  return name || `лист ${index + 1}`;
}

interface HorizontalPanelView {
  start: number;
  end: number;
  nameColumn: number;
  priceColumns: number[];
  columns: RecipeColumn[];
}

interface HorizontalPanelLayout {
  panels: HorizontalPanelView[];
  separators: number[];
}

function horizontalPanelLayout(sheet: RecipeSheet): HorizontalPanelLayout | null {
  const columns = [...sheet.columns].sort((left, right) => left.index - right.index);
  const nameColumns = columns.filter((column) => column.role === 'name').map((column) => column.index);
  if (nameColumns.length < 2) return null;

  const separators: number[] = [];
  const panels = nameColumns.map((start, panelIndex) => {
    const nextStart = nameColumns[panelIndex + 1] ?? Number.POSITIVE_INFINITY;
    const panelColumns = columns.filter((column) => column.index >= start && column.index < nextStart);
    if (panelIndex + 1 < nameColumns.length) {
      while (panelColumns[panelColumns.length - 1]?.role === 'ignore') {
        separators.unshift(panelColumns.pop()!.index);
      }
    }
    const lastColumn = panelColumns[panelColumns.length - 1];
    return {
      start,
      end: lastColumn?.index ?? start,
      nameColumn: start,
      priceColumns: panelColumns
        .filter((column) => column.role === 'price')
        .map((column) => column.index),
      columns: panelColumns,
    };
  });
  if (panels.some((panel) => panel.priceColumns.length === 0)) return null;
  return { panels, separators: separators.sort((left, right) => left - right) };
}

function metricNumber(metrics: Record<string, unknown>, key: string): number | null {
  const value = metrics[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function isWorkbookLayoutRecipe(recipe: Recipe | undefined): boolean {
  return recipe?.format === 'workbook_layout_v1';
}

function normalizeRecipe(raw: Record<string, unknown> | null | undefined): Recipe {
  const base = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
  const sheetsRaw = Array.isArray(base.sheets) ? base.sheets : [];
  const sheets = sheetsRaw.map((entry) => {
    const sheet = entry && typeof entry === 'object' && !Array.isArray(entry)
      ? entry as Record<string, unknown>
      : {};
    const match = sheet.match && typeof sheet.match === 'object' && !Array.isArray(sheet.match)
      ? sheet.match
      : {};
    const columns = Array.isArray(sheet.columns) ? [...sheet.columns] : [];
    while (columns.length > 0) {
      const last = recordValue(columns[columns.length - 1]);
      if (last.role !== 'ignore' || normalizedSheetLabel(last.header)) break;
      columns.pop();
    }
    const normalized = { ...sheet, match, columns } as RecipeSheet;
    return categoryComesFromSheetName(normalized)
      ? {
          ...normalized,
          default_category_source: 'sheet_name',
          default_category_path: null,
        }
      : normalized;
  });
  return { ...base, sheets } as Recipe;
}

export default function RuleEditorPage() {
  const { message } = App.useApp();
  const { isAdmin } = useAuth();
  const { ruleId = '' } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [name, setName] = useState<string>();
  const [recipe, setRecipe] = useState<Recipe>();
  const [jsonDraft, setJsonDraft] = useState('');
  const [jsonError, setJsonError] = useState<string>();
  const [validateVersionId, setValidateVersionId] = useState<string>();
  const [validateVersion, setValidateVersion] = useState<ImportedPriceVersion>();
  const [validationResult, setValidationResult] = useState<RuleValidationOut>();

  const ruleQuery = useQuery({
    queryKey: ['parse-rule', ruleId],
    queryFn: () => fetchParseRule(ruleId),
    enabled: Boolean(ruleId),
  });
  const rule = ruleQuery.data;
  const editable = Boolean(rule && isAdmin && rule.status === 'draft');

  // Seed local editor state once the rule arrives.
  const [seededFor, setSeededFor] = useState<string>();
  if (rule && seededFor !== rule.id) {
    setSeededFor(rule.id);
    setName(rule.name);
    setRecipe(normalizeRecipe(rule.recipe));
    setValidateVersionId(rule.created_from_version_id ?? undefined);
    setValidateVersion(rule.source_version ?? undefined);
    setValidationResult(undefined);
  }

  const invalidateRule = async () => {
    await queryClient.invalidateQueries({ queryKey: ['parse-rule', ruleId] });
    await queryClient.invalidateQueries({ queryKey: ['parse-rules-page'] });
  };

  const saveRule = useMutation({
    mutationFn: () => updateParseRule(ruleId, {
      name: (name ?? '').trim(),
      recipe: (recipe ?? { sheets: [] }) as Record<string, unknown>,
    }),
    onSuccess: async () => {
      await invalidateRule();
      setValidationResult(undefined);
      void message.success('Черновик сохранён новой ревизией');
    },
    onError: (error) => void message.error(extractApiError(error)),
  });

  const validate = useMutation({
    mutationFn: () => validateParseRule(ruleId, validateVersionId!),
    onSuccess: async (result) => {
      setValidationResult(result);
      await invalidateRule();
      void message.success(result.passed ? 'Dry-run пройден успешно' : 'Dry-run выполнен, проверка не пройдена');
    },
    onError: (error) => void message.error(extractApiError(error)),
  });

  const [autoDraft, setAutoDraft] = useState<RuleAutoDraftOut>();
  const reconstruct = useMutation({
    mutationFn: () => autoDraftParseRule(ruleId),
    onSuccess: (result) => {
      setRecipe(normalizeRecipe(result.recipe));
      setAutoDraft(result);
      setJsonDraft('');
      void message.success(`Автоподбор колонок: листов — ${result.sheet_count}`);
    },
    onError: (error) => void message.error(extractApiError(error)),
  });

  const rebindMatrix = useMutation({
    mutationFn: () => rebindParseRuleMatrix(
      ruleId,
      validateVersionId!,
      (recipe ?? { sheets: [] }) as Record<string, unknown>,
    ),
    onSuccess: (result) => {
      setRecipe(normalizeRecipe(result.recipe));
      setAutoDraft(result);
      setValidationResult(undefined);
      setJsonDraft('');
      void message.success('Матричные привязки обновлены; остальные части правила сохранены');
    },
    onError: (error) => void message.error(extractApiError(error)),
  });

  const clone = useMutation({
    mutationFn: () => cloneParseRule(ruleId),
    onSuccess: async (draft) => {
      await queryClient.invalidateQueries({ queryKey: ['parse-rules-page'] });
      void message.success('Создан черновик для редактирования');
      navigate(`/rules/${draft.id}`);
    },
    onError: (error) => void message.error(extractApiError(error)),
  });

  const lifecycle = useMutation({
    mutationFn: (action: 'activate' | 'deprecate') =>
      action === 'activate' ? activateParseRule(ruleId) : deprecateParseRule(ruleId),
    onSuccess: async (_, action) => {
      await invalidateRule();
      void message.success(action === 'activate' ? 'Правило активировано' : 'Правило отправлено в архив');
    },
    onError: (error) => void message.error(extractApiError(error)),
  });

  const sheets = recipe?.sheets ?? [];
  const workbookLayout = isWorkbookLayoutRecipe(recipe);
  const hasMatrixRegions = (
    Array.isArray(recipe?.matrix_regions)
    && recipe.matrix_regions.length > 0
  );
  const [showIgnoredColumns, setShowIgnoredColumns] = useState(false);

  // Group only sheets whose editable parsing semantics are identical. Hidden
  // ignored columns are presentation noise: suppliers often leave a different
  // number of empty trailing columns on otherwise identical sheets.
  const sheetSignature = (sheet: RecipeSheet): string => {
    const headerMode = sheet.repeat
      ? 'repeat'
      : (sheet.header_row == null ? 'headerless' : 'header');
    const columns = sheet.columns
      .filter((column) => showIgnoredColumns || column.role !== 'ignore')
      .map((column) => ({
        index: column.index,
        role: column.role,
        header: column.header ?? '',
        price_descriptor: column.price_descriptor ?? null,
        attribute_code: column.attribute_code ?? null,
        attribute_value_kind: column.attribute_value_kind ?? null,
        canonical_unit: column.canonical_unit ?? null,
        quantity_threshold_column_index: column.quantity_threshold_column_index ?? null,
      }))
      .sort((left, right) => left.index - right.index);
    return JSON.stringify({
      headerMode,
      skip: Boolean(sheet.skip),
      repeat: Boolean(sheet.repeat),
      allowQualifiedPrices: Boolean(sheet.allow_qualified_prices),
      nameFallbackColumns: [...(sheet.name_fallback_column_indexes ?? [])].sort((a, b) => a - b),
      columns,
      inlineAttributes: sheet.inline_attributes ?? [],
    });
  };
  const groups: number[][] = useMemo(() => {
    const bySignature = new Map<string, number[]>();
    sheets.forEach((sheet, index) => {
      const members = bySignature.get(sheetSignature(sheet)) ?? [];
      members.push(index);
      bySignature.set(sheetSignature(sheet), members);
    });
    return [...bySignature.values()];
  }, [sheets, showIgnoredColumns]);

  const patchSheets = (indices: number[], patch: Partial<RecipeSheet>) => {
    setRecipe((prev) => prev && ({
      ...prev,
      sheets: prev.sheets.map((sheet, i) => (indices.includes(i) ? { ...sheet, ...patch } : sheet)),
    }));
  };
  const patchSheet = (index: number, patch: Partial<RecipeSheet>) => patchSheets([index], patch);
  const patchColumn = (indices: number[], columnIndex: number, patch: Partial<RecipeColumn>) => {
    setRecipe((prev) => prev && ({
      ...prev,
      sheets: prev.sheets.map((sheet, si) => (indices.includes(si) ? {
        ...sheet,
        columns: sheet.columns.map((column) => (
          column.index === columnIndex ? { ...column, ...patch } : column
        )),
      } : sheet)),
    }));
  };
  const addColumn = (indices: number[]) => {
    setShowIgnoredColumns(true);
    setRecipe((prev) => prev && ({
      ...prev,
      sheets: prev.sheets.map((sheet, si) => {
        if (!indices.includes(si)) return sheet;
        const nextIndex = sheet.columns.reduce((max, c) => Math.max(max, c.index + 1), 0);
        return { ...sheet, columns: [...sheet.columns, { index: nextIndex, role: 'ignore' as RecipeColumnRole }] };
      }),
    }));
  };
  const removeColumn = (indices: number[], columnIndex: number) => {
    setRecipe((prev) => prev && ({
      ...prev,
      sheets: prev.sheets.map((sheet, si) => (indices.includes(si) ? {
        ...sheet,
        columns: sheet.columns.filter((column) => column.index !== columnIndex),
      } : sheet)),
    }));
  };
  const addSheet = () => {
    setRecipe((prev) => prev && ({
      ...prev,
      sheets: [...prev.sheets, { match: {}, columns: [] } as RecipeSheet],
    }));
  };
  const removeSheets = (indices: number[]) => {
    setRecipe((prev) => prev && ({
      ...prev,
      sheets: prev.sheets.filter((_, si) => !indices.includes(si)),
    }));
  };

  const applyJson = () => {
    try {
      const parsed: unknown = JSON.parse(jsonDraft);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('JSON должен быть объектом');
      }
      setRecipe(normalizeRecipe(parsed as Record<string, unknown>));
      setJsonError(undefined);
      void message.success('JSON применён к форме');
    } catch (error) {
      setJsonError(error instanceof Error ? error.message : 'Некорректный JSON');
    }
  };

  const columnTableColumns = (sheetIndices: number[]): ColumnsType<RecipeColumn> => [
    {
      title: 'Колонка',
      dataIndex: 'index',
      key: 'index',
      width: 120,
      render: (value: number) => (
        <Space size={4}>
          <Tag color="blue" style={{ marginRight: 0 }}>{excelColumn(value)}</Tag>
          <InputNumber
            size="small"
            min={0}
            value={value}
            disabled={!editable}
            onChange={(next) => patchColumn(sheetIndices, value, { index: typeof next === 'number' ? next : 0 })}
          />
        </Space>
      ),
    },
    {
      title: 'Заголовок колонки',
      dataIndex: 'header',
      key: 'header',
      width: 180,
      render: (value: string | undefined, column) => (
        <Input
          size="small"
          value={value ?? ''}
          placeholder="наименование"
          disabled={!editable}
          onChange={(event) => patchColumn(sheetIndices, column.index, { header: event.target.value })}
        />
      ),
    },
    {
      title: 'Роль',
      dataIndex: 'role',
      key: 'role',
      width: 200,
      render: (value: RecipeColumnRole, column) => (
        <Select
          size="small"
          value={value}
          style={{ width: '100%' }}
          options={ROLE_OPTIONS}
          disabled={!editable}
          onChange={(role) => {
            const patch: Partial<RecipeColumn> = { role };
            if (role === 'price' && !column.price_descriptor) {
              patch.price_descriptor = { kind: 'other', vat_mode: 'unknown' };
            }
            patchColumn(sheetIndices, column.index, patch);
          }}
        />
      ),
    },
    {
      title: 'Тип цены / НДС',
      key: 'price',
      width: 220,
      render: (_: unknown, column) => {
        if (column.role !== 'price') return <Typography.Text type="secondary">—</Typography.Text>;
        const descriptor: RecipePriceDescriptor = column.price_descriptor ?? { kind: 'other', vat_mode: 'unknown' };
        return (
          <Space size={4}>
            <Select
              size="small"
              value={descriptor.kind}
              style={{ width: 110 }}
              options={PRICE_KIND_OPTIONS}
              disabled={!editable}
              onChange={(kind) => patchColumn(sheetIndices, column.index, {
                price_descriptor: { ...descriptor, kind },
              })}
            />
            <Select
              size="small"
              value={descriptor.vat_mode}
              style={{ width: 100 }}
              options={VAT_OPTIONS}
              disabled={!editable}
              onChange={(vat_mode) => patchColumn(sheetIndices, column.index, {
                price_descriptor: { ...descriptor, vat_mode },
              })}
            />
          </Space>
        );
      },
    },
    {
      title: 'Атрибут: код / тип / ед.',
      key: 'attribute',
      width: 300,
      render: (_: unknown, column) => {
        if (!ATTRIBUTE_ROLES.includes(column.role)) {
          return <Typography.Text type="secondary">—</Typography.Text>;
        }
        return (
          <Space size={4}>
            <Input
              size="small"
              value={column.attribute_code ?? ''}
              placeholder="код"
              style={{ width: 110 }}
              disabled={!editable}
              onChange={(event) => patchColumn(sheetIndices, column.index, { attribute_code: event.target.value })}
            />
            <Select
              size="small"
              value={column.attribute_value_kind ?? undefined}
              placeholder="тип"
              style={{ width: 100 }}
              options={VALUE_KIND_OPTIONS}
              allowClear
              disabled={!editable}
              onChange={(attribute_value_kind) => patchColumn(sheetIndices, column.index, {
                attribute_value_kind: attribute_value_kind ?? null,
              })}
            />
            <Input
              size="small"
              value={column.canonical_unit ?? ''}
              placeholder="ед."
              style={{ width: 70 }}
              disabled={!editable}
              onChange={(event) => patchColumn(sheetIndices, column.index, { canonical_unit: event.target.value })}
            />
          </Space>
        );
      },
    },
    {
      title: '',
      key: 'remove',
      width: 44,
      render: (_: unknown, column) => (
        <Button
          size="small"
          type="text"
          danger
          icon={<DeleteOutlined />}
          disabled={!editable}
          onClick={() => removeColumn(sheetIndices, column.index)}
        />
      ),
    },
  ];

  if (ruleQuery.isLoading) {
    return <Typography.Text type="secondary">Загрузка правила…</Typography.Text>;
  }
  if (ruleQuery.isError || !rule) {
    return (
      <Alert
        type="error"
        showIcon
        message="Правило не найдено"
        description={extractApiError(ruleQuery.error)}
        action={<Link to="/rules"><Button size="small">К списку правил</Button></Link>}
      />
    );
  }

  const usage = rule.usage ?? { price_versions: 0, database_items: 0, source_rows: 0 };
  const storedValidation = recordValue(rule.validation);
  const validationMetrics = recordValue(storedValidation.metrics);
  const parsedItems = metricNumber(validationMetrics, 'items_with_price');
  const metricSourceItems = metricNumber(validationMetrics, 'source_items');
  const sourceItems = metricSourceItems ?? (
    storedValidation.price_version_id === rule.source_version?.id
      ? (rule.source_version?.item_count ?? 0)
      : 0
  );
  const lostItems = parsedItems !== null && sourceItems > 0 && parsedItems < sourceItems
    ? sourceItems - parsedItems
    : 0;
  const validated = storedValidation.passed === true && lostItems === 0;
  const storedSamples = Array.isArray(storedValidation.samples)
    ? storedValidation.samples
    : [];
  const storedValidationResult = storedValidation.passed === true
    && typeof storedValidation.at === 'string'
    && typeof storedValidation.price_version_id === 'string'
    ? {
        at: storedValidation.at,
        price_version_id: storedValidation.price_version_id,
        metrics: validationMetrics,
        passed: true,
        samples: storedSamples,
      } as RuleValidationOut
    : undefined;

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Space style={{ justifyContent: 'space-between', width: '100%' }} wrap>
        <Space>
          <Link to="/rules"><Button icon={<ArrowLeftOutlined />}>К списку правил</Button></Link>
          <Tag color={rule.status === 'active' ? 'green' : rule.status === 'draft' ? 'gold' : 'default'}>
            {STATUS_LABELS[rule.status]}
          </Tag>
          <Tag>{rule.loader.toUpperCase()}</Tag>
          <Typography.Text type="secondary">ревизия {rule.revision}</Typography.Text>
        </Space>
        <Space wrap>
          {rule.source_import_id && (
            <Link to={`/imports/${rule.source_import_id}`}><Button>Исходный импорт</Button></Link>
          )}
          {isAdmin && rule.status !== 'draft' && (
            <Button icon={<CopyOutlined />} loading={clone.isPending} onClick={() => clone.mutate()}>
              Редактировать (создать черновик)
            </Button>
          )}
          {editable && (
            <Button
              type="primary"
              icon={<SaveOutlined />}
              loading={saveRule.isPending}
              onClick={() => {
                if (!(name ?? '').trim()) {
                  void message.error('Название правила не может быть пустым');
                  return;
                }
                saveRule.mutate();
              }}
            >
              Сохранить черновик
            </Button>
          )}
          {editable && validated && (
            <Button
              type="primary"
              icon={<CheckCircleOutlined />}
              onClick={() => lifecycle.mutate('activate')}
            >
              Активировать
            </Button>
          )}
          {isAdmin && rule.status === 'active' && (
            <Button danger icon={<StopOutlined />} onClick={() => lifecycle.mutate('deprecate')}>
              В архив
            </Button>
          )}
        </Space>
      </Space>

      <div>
        <Typography.Text strong>Название правила</Typography.Text>
        <Input
          aria-label="Название правила"
          value={name ?? ''}
          disabled={!editable}
          onChange={(event) => setName(event.target.value)}
          style={{ marginTop: 6, maxWidth: 560 }}
        />
      </div>

      <Descriptions bordered size="small" column={{ xs: 1, sm: 2, md: 3 }}>
        <Descriptions.Item label="Поставщик">{rule.supplier_name ?? 'Любой'}</Descriptions.Item>
        <Descriptions.Item label="Источник">{rule.provenance}</Descriptions.Item>
        <Descriptions.Item label="Изменено">{formatDate(rule.updated_at)}</Descriptions.Item>
        <Descriptions.Item label="Версий прайсов">{usage.price_versions.toLocaleString('ru-RU')}</Descriptions.Item>
        <Descriptions.Item label="Строк-источников">{usage.source_rows.toLocaleString('ru-RU')}</Descriptions.Item>
        <Descriptions.Item label="Позиций в БД">{usage.database_items.toLocaleString('ru-RU')}</Descriptions.Item>
      </Descriptions>

      {lostItems > 0 && parsedItems !== null
        ? (
          <Alert
            showIcon
            type="error"
            message={`Dry-run разобрал ${parsedItems.toLocaleString('ru-RU')} из ${sourceItems.toLocaleString('ru-RU')} позиций`}
            description={`Потеряно ${lostItems.toLocaleString('ru-RU')}. Активация заблокирована: сохраните полное правило и повторите dry-run.`}
          />
        )
        : validated
          ? <Alert showIcon type="success" message="Dry-run пройден — правило готово к активации" />
          : <Alert showIcon type="warning" message="Правило ещё не подтверждено dry-run проверкой на прайсе" />}

      {/* Rule structure editor */}
      <Card
        size="small"
        title="Структура правила"
        extra={editable ? (
          <Space>
            <Checkbox
              checked={showIgnoredColumns}
              onChange={(event) => setShowIgnoredColumns(event.target.checked)}
            >
              игнорируемые колонки
            </Checkbox>
            {hasMatrixRegions && validateVersionId && (
              <Button
                size="small"
                icon={<ThunderboltOutlined />}
                loading={rebindMatrix.isPending}
                onClick={() => rebindMatrix.mutate()}
              >
                Перепривязать матрицу
              </Button>
            )}
            {rule.created_from_version_id && (
              <Button
                size="small"
                icon={<ThunderboltOutlined />}
                loading={reconstruct.isPending}
                onClick={() => reconstruct.mutate()}
              >
                Автоподбор колонок
              </Button>
            )}
            <Button size="small" icon={<PlusOutlined />} onClick={addSheet}>Добавить лист</Button>
          </Space>
        ) : null}
      >
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
            Каждый табличный шаблон хранит лист, способ поиска заголовка и маппинг колонок.
            В новых правилах номера строк не фиксируются: заголовок находится по названиям колонок,
            а строки-разделы меняют категорию до следующего раздела. Поэтому добавление и удаление товаров
            в следующей версии прайса не требует нового запуска DeepSeek.
          </Typography.Paragraph>
          {groups.map((memberIndices) => {
            const representative = sheets[memberIndices[0]];
            const isGroup = memberIndices.length > 1;
            const isDynamicGroup = isGroup && memberIndices.every(
              (index) => Boolean(sheets[index].dynamic_body),
            );
            const ignoredCount = representative.columns.filter((c) => c.role === 'ignore').length;
            const visibleColumns = showIgnoredColumns
              ? representative.columns
              : representative.columns.filter((c) => c.role !== 'ignore');
            const memberLabel = (index: number) => displaySheetName(sheets[index], index);
            const categoryUsesSheetName = categoryComesFromSheetName(representative);
            const panelLayout = horizontalPanelLayout(representative);
            return (
              <Card
                key={memberIndices.join(',')}
                size="small"
                type="inner"
                title={(
                  <Space wrap size={4}>
                    {isGroup
                      ? (
                        <Typography.Text strong>
                          {isDynamicGroup ? 'Шаблоны' : 'Секции'} ×{memberIndices.length}
                        </Typography.Text>
                      )
                      : <Typography.Text strong>Табличный шаблон {memberIndices[0] + 1}</Typography.Text>}
                    {isGroup
                      ? <Tag color="geekblue">одинаковая структура</Tag>
                      : (
                        typeof representative.match?.name_pattern === 'string' && representative.match.name_pattern
                          ? <Tag color="blue">{representative.match.name_pattern}</Tag>
                          : <Tag>по индексу</Tag>
                      )}
                    {representative.repeat ? (
                      <Tag color="purple">
                        повторяющаяся секция
                        {typeof representative.repeat_sections === 'number' && representative.repeat_sections > 1
                          ? ` ×${representative.repeat_sections}`
                          : ''}
                      </Tag>
                    ) : null}
                    {representative.header_match === 'signature' ? (
                      <Tag color="cyan">заголовок по названиям колонок</Tag>
                    ) : null}
                    {representative.headerless ? (
                      <Tag color="cyan">без шапки · по форме строк</Tag>
                    ) : null}
                    {representative.skip ? <Tag color="red">пропускается</Tag> : null}
                  </Space>
                )}
                extra={editable ? (
                  <Button
                    size="small"
                    type="text"
                    danger
                    icon={<DeleteOutlined />}
                    onClick={() => removeSheets(memberIndices)}
                  >
                    {isGroup ? `Удалить секции (${memberIndices.length})` : 'Удалить лист'}
                  </Button>
                ) : null}
              >
                <Space direction="vertical" size="small" style={{ width: '100%' }}>
                  {isGroup ? (
                    <div role="region" aria-label="Индивидуальные настройки секций группы">
                      <Space direction="vertical" size="small" style={{ width: '100%' }}>
                        <Typography.Text type="secondary">
                          {isDynamicGroup
                            ? 'Маппинг колонок общий; для каждого шаблона отдельно задаются лист, способ поиска заголовка и источник категории. Номера строк не сохраняются.'
                            : 'Маппинг колонок общий, а лист, диапазон строк и категория сохраняются отдельно для каждой секции.'}
                        </Typography.Text>
                        {memberIndices.map((index) => {
                          const member = sheets[index];
                          const memberUsesSheetName = categoryComesFromSheetName(member);
                          return (
                            <Card key={index} size="small" type="inner" title={memberLabel(index)}>
                              <Space wrap size="middle">
                                <Space size={4}>
                                  <Typography.Text type="secondary">Имя листа:</Typography.Text>
                                  <Input
                                    size="small"
                                    value={member.match?.name_pattern ?? ''}
                                    style={{ width: 210 }}
                                    disabled={!editable}
                                    onChange={(event) => patchSheet(index, {
                                      match: { ...member.match, name_pattern: event.target.value },
                                    })}
                                  />
                                </Space>
                                <Space size={4}>
                                  <Typography.Text type="secondary">Заголовок:</Typography.Text>
                                  {member.dynamic_body ? (
                                    <Tag color="cyan">
                                      {member.headerless ? 'без шапки · по форме строк' : 'авто — по названиям колонок'}
                                    </Tag>
                                  ) : (
                                    <InputNumber
                                      size="small" min={0} value={member.header_row ?? null} style={{ width: 72 }}
                                      disabled={!editable || Boolean(member.repeat)}
                                      onChange={(next) => patchSheet(index, { header_row: typeof next === 'number' ? next : null })}
                                    />
                                  )}
                                </Space>
                                <Space size={4}>
                                  <Typography.Text type="secondary">Строки:</Typography.Text>
                                  {member.dynamic_body ? (
                                    <Tag>авто — до следующего шаблона или конца листа</Tag>
                                  ) : (
                                    <>
                                      <InputNumber
                                        size="small" min={0} value={member.start_row ?? null} placeholder="с" style={{ width: 72 }}
                                        disabled={!editable}
                                        onChange={(next) => patchSheet(index, { start_row: typeof next === 'number' ? next : null })}
                                      />
                                      <InputNumber
                                        size="small" min={0} value={member.end_row ?? null} placeholder="по" style={{ width: 72 }}
                                        disabled={!editable}
                                        onChange={(next) => patchSheet(index, { end_row: typeof next === 'number' ? next : null })}
                                      />
                                    </>
                                  )}
                                </Space>
                                <Checkbox
                                  checked={memberUsesSheetName}
                                  disabled={!editable}
                                  onChange={(event) => patchSheet(index, event.target.checked
                                    ? { default_category_source: 'sheet_name', default_category_path: null }
                                    : { default_category_source: null, default_category_path: '' })}
                                >
                                  категория из имени листа
                                </Checkbox>
                                {memberUsesSheetName && (
                                  <Tag color="blue">{memberLabel(index)} → {memberLabel(index)}</Tag>
                                )}
                                <Input
                                  size="small"
                                  aria-label={`Категория секции ${index + 1}`}
                                  value={memberUsesSheetName ? '' : (member.default_category_path ?? '')}
                                  placeholder={memberUsesSheetName ? 'имя листа' : 'Категория'}
                                  style={{ width: 250 }}
                                  disabled={!editable || memberUsesSheetName}
                                  onChange={(event) => patchSheet(index, {
                                    default_category_source: null,
                                    default_category_path: event.target.value,
                                  })}
                                />
                              </Space>
                            </Card>
                          );
                        })}
                      </Space>
                    </div>
                  ) : categoryUsesSheetName && (
                    <Space wrap size={4}>
                      <Typography.Text type="secondary">Категории:</Typography.Text>
                      {memberIndices.map((index) => {
                        const sheetName = memberLabel(index);
                        return <Tag key={index}>{sheetName} → {sheetName}</Tag>;
                      })}
                    </Space>
                  )}
                  {!isGroup && <Space wrap size="middle">
                    <Space size={4}>
                      <Typography.Text type="secondary">Имя листа:</Typography.Text>
                      <Input
                        size="small"
                        value={representative.match?.name_pattern ?? ''}
                        placeholder="точное имя листа"
                        style={{ width: 220 }}
                        disabled={!editable || isGroup}
                        onChange={(event) => patchSheets(memberIndices, {
                          match: { ...representative.match, name_pattern: event.target.value },
                        })}
                      />
                    </Space>
                    <Space size={4}>
                      <Typography.Text type="secondary">или № листа:</Typography.Text>
                      <InputNumber
                        size="small"
                        min={0}
                        value={representative.match?.or_index ?? null}
                        style={{ width: 80 }}
                        disabled={!editable || isGroup}
                        onChange={(next) => patchSheets(memberIndices, {
                          match: { ...representative.match, or_index: typeof next === 'number' ? next : null },
                        })}
                      />
                    </Space>
                    <Space size={4}>
                      <Typography.Text type="secondary">Строка заголовка:</Typography.Text>
                      {representative.repeat ? (
                        <Tag color="purple">авто — находится в каждой секции</Tag>
                      ) : representative.header_match === 'signature' ? (
                        <Tag color="cyan">авто — по названиям колонок</Tag>
                      ) : representative.headerless ? (
                        <Tag color="cyan">нет — по форме строк</Tag>
                      ) : (
                        <>
                          <InputNumber
                            size="small"
                            min={0}
                            value={representative.header_row ?? null}
                            style={{ width: 80 }}
                            disabled={!editable}
                            onChange={(next) => patchSheets(memberIndices, { header_row: typeof next === 'number' ? next : null })}
                          />
                          {representative.header_row == null && <Tag color="orange">без шапки</Tag>}
                        </>
                      )}
                    </Space>
                    <Space size={4}>
                      <Typography.Text type="secondary">Строки данных:</Typography.Text>
                      {representative.dynamic_body ? (
                        <Tag color="cyan">авто — до следующего шаблона или конца листа</Tag>
                      ) : (
                        <>
                          <InputNumber
                            size="small"
                            min={0}
                            placeholder="с"
                            value={representative.start_row ?? null}
                            style={{ width: 70 }}
                            disabled={!editable}
                            onChange={(next) => patchSheets(memberIndices, { start_row: typeof next === 'number' ? next : null })}
                          />
                          <InputNumber
                            size="small"
                            min={0}
                            placeholder="по"
                            value={representative.end_row ?? null}
                            style={{ width: 70 }}
                            disabled={!editable}
                            onChange={(next) => patchSheets(memberIndices, { end_row: typeof next === 'number' ? next : null })}
                          />
                        </>
                      )}
                    </Space>
                  </Space>}
                  <Space wrap size="middle">
                    {!isGroup && <Checkbox
                      checked={categoryUsesSheetName}
                      disabled={!editable}
                      onChange={(event) => patchSheets(memberIndices, event.target.checked
                        ? { default_category_source: 'sheet_name', default_category_path: null }
                        : { default_category_source: null, default_category_path: '' })}
                    >
                      категория из имени листа
                    </Checkbox>}
                    {!isGroup && <Space size={4}>
                      <Typography.Text type="secondary">Категория по умолчанию:</Typography.Text>
                      <Input
                        size="small"
                        value={categoryUsesSheetName ? '' : (representative.default_category_path ?? '')}
                        placeholder={categoryUsesSheetName ? 'берётся из имени каждого листа' : 'Раздел / Подраздел'}
                        style={{ width: 280 }}
                        disabled={!editable || categoryUsesSheetName}
                        onChange={(event) => patchSheets(memberIndices, {
                          default_category_source: null,
                          default_category_path: event.target.value,
                        })}
                      />
                    </Space>}
                    <Checkbox
                      checked={Boolean(representative.allow_qualified_prices)}
                      disabled={!editable}
                      onChange={(event) => patchSheets(memberIndices, { allow_qualified_prices: event.target.checked })}
                    >
                      цены с диапазонами
                    </Checkbox>
                    <Checkbox
                      checked={Boolean(representative.skip)}
                      disabled={!editable}
                      onChange={(event) => patchSheets(memberIndices, { skip: event.target.checked })}
                    >
                      пропустить лист
                    </Checkbox>
                    {!representative.dynamic_body && <Checkbox
                      checked={Boolean(representative.repeat)}
                      disabled={!editable}
                      onChange={(event) => patchSheets(memberIndices, event.target.checked
                        ? { repeat: true, header_row: null }
                        : { repeat: false, repeat_sections: undefined })}
                    >
                      повторяющаяся секция
                    </Checkbox>}
                  </Space>
                  {representative.repeat && (
                    <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
                      На листе повторяется связка «категория → заголовок → позиции». Одна секция в правиле
                      покрывает все повторы: заголовок находится автоматически, а категория каждой секции
                      берётся из её строки-раздела.
                    </Typography.Paragraph>
                  )}

                  {panelLayout ? (
                    <Space direction="vertical" size="small" style={{ width: '100%' }}>
                      {panelLayout.panels.map((panel, panelIndex) => {
                        const panelColumns = panel.columns.filter((column) => (
                          showIgnoredColumns || column.role !== 'ignore'
                        ));
                        return (
                          <Card
                            key={`panel-${panel.start}`}
                            size="small"
                            type="inner"
                            title={`Панель ${panelIndex + 1} · ${excelColumn(panel.start)}:${excelColumn(panel.end)}`}
                            extra={(
                              <Typography.Text strong>
                                {excelColumn(panel.nameColumn)} → цены {panel.priceColumns.map(excelColumn).join(', ')}
                              </Typography.Text>
                            )}
                          >
                            <Table<RecipeColumn>
                              rowKey="__rowKey"
                              size="small"
                              columns={columnTableColumns(memberIndices)}
                              dataSource={panelColumns.map((column) => ({
                                ...column,
                                __rowKey: `col-${column.index}`,
                              }))}
                              pagination={false}
                              scroll={{ x: 1020 }}
                            />
                          </Card>
                        );
                      })}
                      {panelLayout.separators.length > 0 && (
                        <Typography.Text type="secondary">
                          {panelLayout.separators.map(excelColumn).join(', ')} — разделитель между панелями, не поле товара и не потерянные данные.
                        </Typography.Text>
                      )}
                    </Space>
                  ) : (
                    <Table<RecipeColumn>
                      rowKey="__rowKey"
                      size="small"
                      columns={columnTableColumns(memberIndices)}
                      dataSource={visibleColumns.map((column) => ({ ...column, __rowKey: `col-${column.index}` }))}
                      pagination={false}
                      scroll={{ x: 1020 }}
                      locale={{ emptyText: 'Колонки не настроены' }}
                    />
                  )}
                  {!panelLayout && !showIgnoredColumns && ignoredCount > 0 && (
                    <Typography.Text type="secondary">
                      Скрыто игнорируемых колонок: {ignoredCount}
                    </Typography.Text>
                  )}
                  {editable && (
                    <Button size="small" icon={<PlusOutlined />} onClick={() => addColumn(memberIndices)}>
                      Добавить колонку
                    </Button>
                  )}
                </Space>
              </Card>
            );
          })}
          {workbookLayout && recipe && (
            <WorkbookLayoutEditor
              ruleId={rule.id}
              priceVersionId={validateVersionId ?? rule.created_from_version_id}
              recipe={recipe}
              editable={editable}
              samples={(validationResult ?? storedValidationResult)?.samples}
              onChange={setRecipe}
            />
          )}
          {!sheets.length && !hasMatrixRegions && (
            <Alert
              type="warning"
              showIcon
              message="В правиле нет листов"
              description="Правило пустое. Восстановите колонки автоматически из исходного прайса или добавьте лист вручную."
              action={isAdmin && rule.created_from_version_id ? (
                <Button
                  type="primary"
                  icon={<ThunderboltOutlined />}
                  loading={reconstruct.isPending}
                  onClick={() => reconstruct.mutate()}
                >
                  Восстановить из файла
                </Button>
              ) : undefined}
            />
          )}
          {autoDraft && (
            <Alert
              showIcon
              type={autoDraft.passed ? 'success' : 'warning'}
              message={`Автоподбор: листов — ${autoDraft.sheet_count}, позиций — ${String(autoDraft.metrics.rows_accepted ?? '—')}`}
              description="Колонки загружены в форму, но ещё не сохранены. Проверьте роли колонок и нажмите «Сохранить черновик»."
            />
          )}
        </Space>
      </Card>

      {/* Advanced raw JSON */}
      <Collapse
        items={[{
          key: 'json',
          label: 'JSON правила (для опытных)',
          children: (
            <Space direction="vertical" size="small" style={{ width: '100%' }}>
              <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
                Полный JSON, включая inline-атрибуты и служебные поля. Изменения применяются кнопкой —
                форма выше обновится. Непонятные поля сохраняются как есть.
              </Typography.Paragraph>
              <Input.TextArea
                value={jsonDraft}
                onChange={(event) => setJsonDraft(event.target.value)}
                onFocus={() => { if (!jsonDraft) setJsonDraft(JSON.stringify(recipe ?? { sheets: [] }, null, 2)); }}
                autoSize={{ minRows: 10, maxRows: 30 }}
                style={{ fontFamily: 'monospace' }}
              />
              {jsonError && <Alert type="error" showIcon message={jsonError} />}
              <Space>
                <Button onClick={() => { setJsonDraft(JSON.stringify(recipe ?? { sheets: [] }, null, 2)); setJsonError(undefined); }}>
                  Обновить из формы
                </Button>
                <Button type="primary" disabled={!editable} onClick={applyJson}>
                  Применить JSON к форме
                </Button>
              </Space>
            </Space>
          ),
        }]}
      />

      {isAdmin && (
        <RuleValidationPricePicker
          rule={rule}
          selectedVersionId={validateVersionId}
          selectedVersion={validateVersion}
          onSelect={(version) => {
            setValidateVersionId(version.id);
            setValidateVersion(version);
          }}
          onValidate={() => validate.mutate()}
          validating={validate.isPending}
          validationResult={validationResult ?? storedValidationResult}
        />
      )}

    </Space>
  );
}
