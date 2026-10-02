import { describe, expect, it } from 'vitest';
import type { ReviewMapping, ReviewSectionDetail } from '../../api/types';
import {
  decisionKey,
  excelColumn,
  mappingForSection,
  mergeSectionMapping,
  projectValidationDiff,
} from './reviewModel';

const section: ReviewSectionDetail = {
  import_id: 'import-1',
  review_id: 'review-1',
  section_id: 'sec-1',
  revision: 2,
  sheet_name: 'Прайс',
  section_kind: 'price_rows',
  header_row_index: 0,
  header_values: ['Наименование', 'Цена'],
  rows: [],
  horizontal_panels: [],
  mapping: null,
  decisions: [],
  attempt: null,
  validation: null,
  actions: { can_edit: true, can_validate: true, can_apply: false, can_activate: false },
};

describe('reviewModel', () => {
  it('converts zero-based indexes to Excel labels', () => {
    expect([0, 25, 26, 51, 52].map(excelColumn)).toEqual(['A', 'Z', 'AA', 'AZ', 'BA']);
  });

  it('creates an explicit ignore mapping for every visible column', () => {
    expect(mappingForSection(section).columns).toEqual([
      { index: 0, role: 'ignore' },
      { index: 1, role: 'ignore' },
    ]);
  });

  it('returns an isolated copy of an existing section mapping', () => {
    const mapped = {
      ...section,
      mapping: { tables: [{ sheet_name: 'Прайс', header_row_index: 0, columns: [{ index: 0, role: 'name' as const }] }] },
    };
    const table = mappingForSection(mapped);
    table.columns[0].role = 'ignore';
    expect(mapped.mapping.tables[0].columns[0].role).toBe('name');
  });

  it('replaces only the selected section mapping', () => {
    const mapping: ReviewMapping = {
      tables: [{ sheet_name: 'Другой', header_row_index: 0, columns: [] }],
    };
    const merged = mergeSectionMapping(mapping, {
      sheet_name: 'Прайс',
      header_row_index: 0,
      columns: [{ index: 0, role: 'name' }, { index: 1, role: 'price' }],
    });
    expect(merged.tables).toHaveLength(2);
    expect(merged.tables[1].columns[1].role).toBe('price');
  });

  it('keeps row-panel decisions distinct', () => {
    expect(decisionKey('Прайс', 12, 0)).not.toBe(decisionKey('Прайс', 12, 1));
  });

  it('groups several result items from one physical coordinate', () => {
    const projected = projectValidationDiff({
      added: [
        { sheet: 'Прайс', row: 12, panel: 0, name: 'A' },
        { sheet: 'Прайс', row: 12, panel: 0, name: 'B' },
      ],
      removed: [{ sheet: 'Прайс', row: 13, panel: null, name: 'C' }],
    });
    expect(projected[0].items).toHaveLength(2);
    expect(projected.map((row) => row.kind)).toEqual(['added', 'removed']);
  });

  it('ignores malformed diff records and supports source coordinate aliases', () => {
    expect(projectValidationDiff({
      added: [null, 'bad'],
      changed: [{ source_sheet: 'Лист', source_row: 7, source_panel_index: 1 }],
      unchanged: 'not-an-array',
    })).toEqual([expect.objectContaining({ sheet: 'Лист', row: 7, panel: 1 })]);
  });
});
