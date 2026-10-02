import type {
  ReviewColumnRole,
  ReviewMapping,
  ReviewMappingTable,
  ReviewSectionDetail,
} from '../../api/types';

export const REVIEW_ROLES: Array<{ value: ReviewColumnRole; label: string }> = [
  { value: 'name', label: 'Наименование' },
  { value: 'price', label: 'Цена' },
  { value: 'article', label: 'Артикул' },
  { value: 'manufacturer', label: 'Производитель' },
  { value: 'description', label: 'Описание' },
  { value: 'attribute', label: 'Характеристика' },
  { value: 'unit', label: 'Единица' },
  { value: 'quantity_threshold', label: 'Порог количества' },
  { value: 'discount_percent', label: 'Скидка' },
  { value: 'weight', label: 'Вес' },
  { value: 'volume', label: 'Объём' },
  { value: 'dimensions', label: 'Габариты' },
  { value: 'stock', label: 'Наличие' },
  { value: 'lead_time', label: 'Срок поставки' },
  { value: 'category', label: 'Категория' },
  { value: 'photo', label: 'Фото' },
  { value: 'row_number', label: 'Номер строки' },
  { value: 'ignore', label: 'Не использовать' },
];

export function excelColumn(index: number): string {
  let value = index + 1;
  let label = '';
  while (value > 0) {
    value -= 1;
    label = String.fromCharCode(65 + (value % 26)) + label;
    value = Math.floor(value / 26);
  }
  return label;
}

export function mappingForSection(section: ReviewSectionDetail): ReviewMappingTable {
  const existing = section.mapping?.tables.find(
    (table) =>
      table.sheet_name === section.sheet_name &&
      table.header_row_index === section.header_row_index,
  );
  if (existing) return structuredClone(existing);
  const width = Math.max(
    section.header_values.length,
    ...section.rows.map((row) => row.cells.length),
    0,
  );
  return {
    sheet_name: section.sheet_name,
    header_row_index: section.header_row_index,
    columns: Array.from({ length: width }, (_, index) => ({ index, role: 'ignore' })),
  };
}

export function mergeSectionMapping(
  mapping: ReviewMapping | null,
  table: ReviewMappingTable,
): ReviewMapping {
  const tables = (mapping?.tables ?? []).filter(
    (candidate) =>
      candidate.sheet_name !== table.sheet_name ||
      candidate.header_row_index !== table.header_row_index,
  );
  return { tables: [...tables, structuredClone(table)] };
}

export function decisionKey(sheet: string, excelRow: number, panel: number | null): string {
  return `${sheet}\u0000${excelRow}\u0000${panel ?? 'all'}`;
}

export type ProjectedDiff = {
  key: string;
  kind: 'added' | 'removed' | 'changed' | 'unchanged';
  sheet: string;
  row: number;
  panel: number | null;
  items: Array<Record<string, unknown>>;
};

export function projectValidationDiff(diff: Record<string, unknown>): ProjectedDiff[] {
  const groups = new Map<string, ProjectedDiff>();
  for (const kind of ['added', 'removed', 'changed', 'unchanged'] as const) {
    const items = Array.isArray(diff[kind]) ? diff[kind] : [];
    for (const candidate of items) {
      if (!candidate || typeof candidate !== 'object') continue;
      const item = candidate as Record<string, unknown>;
      const sheet = String(item.sheet ?? item.source_sheet ?? '');
      const row = Number(item.row ?? item.source_row ?? 0);
      const panelValue = item.panel ?? item.source_panel_index;
      const panel = panelValue === null || panelValue === undefined ? null : Number(panelValue);
      const key = decisionKey(sheet, row, panel);
      const groupKey = `${kind}\u0000${key}`;
      const group = groups.get(groupKey) ?? { key, kind, sheet, row, panel, items: [] };
      group.items.push(item);
      groups.set(groupKey, group);
    }
  }
  return [...groups.values()];
}
