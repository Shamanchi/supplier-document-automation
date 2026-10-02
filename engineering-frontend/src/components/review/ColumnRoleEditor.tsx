import { Select, Space, Typography } from 'antd';
import type { ReviewMappingColumn, ReviewMappingTable } from '../../api/types';
import { REVIEW_ROLES } from './reviewModel';

export default function ColumnRoleEditor({
  column,
  table,
  label,
  disabled,
  onChange,
}: {
  column: ReviewMappingColumn;
  table: ReviewMappingTable;
  label: string;
  disabled: boolean;
  onChange: (column: ReviewMappingColumn) => void;
}) {
  const updateRole = (role: ReviewMappingColumn['role']) => {
    const next: ReviewMappingColumn = { index: column.index, role };
    if (role === 'price') {
      next.price_descriptor = column.price_descriptor ?? { kind: 'other', vat_mode: 'unknown' };
      next.quantity_threshold_column_index = column.quantity_threshold_column_index;
    }
    if (role === 'attribute') {
      next.attribute_code = column.attribute_code;
      next.attribute_value_kind = column.attribute_value_kind ?? 'text';
      next.canonical_unit = column.canonical_unit;
    }
    onChange(next);
  };
  return (
    <Space direction="vertical" size={4} style={{ minWidth: 150 }}>
      <Typography.Text strong>{label}</Typography.Text>
      <Select
        aria-label={`Роль колонки ${label.split(' ')[0]}`}
        size="small"
        value={column.role}
        disabled={disabled}
        onChange={updateRole}
        options={REVIEW_ROLES}
        style={{ width: '100%' }}
      />
      {column.role === 'price' && (
        <Space.Compact block>
          <Select
            aria-label={`Тип цены ${label}`}
            size="small"
            disabled={disabled}
            value={column.price_descriptor?.kind ?? 'other'}
            options={['wholesale', 'retail', 'dealer', 'purchase', 'other'].map((value) => ({ value }))}
            onChange={(kind) => onChange({
              ...column,
              price_descriptor: {
                kind,
                vat_mode: column.price_descriptor?.vat_mode ?? 'unknown',
              },
            })}
          />
          <Select
            aria-label={`Порог количества ${label}`}
            size="small"
            allowClear
            disabled={disabled}
            placeholder="Порог"
            value={column.quantity_threshold_column_index}
            options={table.columns
              .filter((candidate) => candidate.role === 'quantity_threshold')
              .map((candidate) => ({ value: candidate.index, label: `Колонка ${candidate.index + 1}` }))}
            onChange={(value) => onChange({ ...column, quantity_threshold_column_index: value })}
          />
        </Space.Compact>
      )}
    </Space>
  );
}
