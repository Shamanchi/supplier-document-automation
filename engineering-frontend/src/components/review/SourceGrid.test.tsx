import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { ReviewMappingTable, ReviewSectionDetail } from '../../api/types';
import SourceGrid from './SourceGrid';

const section: ReviewSectionDetail = {
  import_id: 'import-1',
  review_id: 'review-1',
  section_id: 'sec-1',
  revision: 1,
  sheet_name: 'Прайс',
  section_kind: 'price_rows',
  header_row_index: 0,
  header_values: ['Наименование', 'Цена'],
  rows: [{
    row_index: 1,
    excel_row: 2,
    kind: 'source',
    cells: [
      { column_index: 0, excel_column: 'A', value: 'Товар', is_bold: false, is_merged_fill: false },
      { column_index: 1, excel_column: 'B', value: 100, is_bold: false, is_merged_fill: false },
    ],
  }],
  horizontal_panels: [],
  mapping: null,
  decisions: [],
  attempt: null,
  validation: null,
  actions: { can_edit: true, can_validate: true, can_apply: false, can_activate: false },
};

const mapping: ReviewMappingTable = {
  sheet_name: 'Прайс',
  header_row_index: 0,
  columns: [{ index: 0, role: 'name' }, { index: 1, role: 'price' }],
};

describe('SourceGrid', () => {
  it('renders Excel coordinates and emits row decisions', () => {
    const onAccept = vi.fn();
    const onReject = vi.fn();
    render(
      <SourceGrid
        section={section}
        mapping={mapping}
        editable
        onMappingChange={vi.fn()}
        onAccept={onAccept}
        onReject={onReject}
      />,
    );
    expect(screen.getByRole('columnheader', { name: /A Наименование/ })).toBeVisible();
    expect(screen.getByRole('rowheader', { name: '2' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Принять строку 2' }));
    expect(onAccept).toHaveBeenCalledWith(2, null);
    fireEvent.click(screen.getByRole('button', { name: 'Отклонить строку 2' }));
    expect(onReject).toHaveBeenCalledWith(2, null);
  });

  it('moves the active cell with arrow keys', () => {
    render(<SourceGrid section={section} mapping={mapping} editable onMappingChange={vi.fn()} onAccept={vi.fn()} onReject={vi.fn()} />);
    const first = screen.getByText('Товар').closest('td')!;
    first.focus();
    fireEvent.keyDown(first, { key: 'ArrowRight' });
    expect(screen.getByText('100').closest('td')).toHaveClass('is-active');
  });
});
