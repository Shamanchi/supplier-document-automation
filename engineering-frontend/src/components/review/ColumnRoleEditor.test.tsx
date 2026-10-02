import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { ReviewMappingTable } from '../../api/types';
import ColumnRoleEditor from './ColumnRoleEditor';

const table: ReviewMappingTable = {
  sheet_name: 'Прайс',
  header_row_index: 0,
  columns: [
    { index: 0, role: 'ignore' },
    { index: 1, role: 'quantity_threshold' },
  ],
};

function choose(label: string, option: string) {
  fireEvent.mouseDown(screen.getByRole('combobox', { name: label }));
  fireEvent.click(screen.getByTitle(option));
}

describe('ColumnRoleEditor', () => {
  it('assigns a price role with deterministic defaults', () => {
    const onChange = vi.fn();
    render(<ColumnRoleEditor column={table.columns[0]} table={table} label="A Название" disabled={false} onChange={onChange} />);
    choose('Роль колонки A', 'Цена');
    expect(onChange).toHaveBeenCalledWith({
      index: 0,
      role: 'price',
      price_descriptor: { kind: 'other', vat_mode: 'unknown' },
      quantity_threshold_column_index: undefined,
    });
  });

  it('edits the price kind and quantity threshold', () => {
    const onChange = vi.fn();
    const price = { index: 0, role: 'price' as const, price_descriptor: { kind: 'other' as const, vat_mode: 'unknown' as const } };
    render(<ColumnRoleEditor column={price} table={table} label="A Цена" disabled={false} onChange={onChange} />);
    choose('Тип цены A Цена', 'retail');
    choose('Порог количества A Цена', 'Колонка 2');
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ price_descriptor: { kind: 'retail', vat_mode: 'unknown' } }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ quantity_threshold_column_index: 1 }));
  });

  it('preserves attribute metadata when assigning an attribute role', () => {
    const onChange = vi.fn();
    const column = { index: 0, role: 'ignore' as const, attribute_code: 'color', attribute_value_kind: 'decimal' as const, canonical_unit: 'mm' };
    render(<ColumnRoleEditor column={column} table={table} label="A Свойство" disabled={false} onChange={onChange} />);
    choose('Роль колонки A', 'Характеристика');
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({
      role: 'attribute', attribute_code: 'color', attribute_value_kind: 'decimal', canonical_unit: 'mm',
    }));
  });
});
