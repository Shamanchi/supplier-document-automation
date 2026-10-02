import { CheckOutlined, CloseOutlined } from '@ant-design/icons';
import { Button, Space, Tooltip } from 'antd';
import { useState } from 'react';
import type { ReviewMappingColumn, ReviewMappingTable, ReviewSectionDetail } from '../../api/types';
import ColumnRoleEditor from './ColumnRoleEditor';
import './review.css';

export default function SourceGrid({
  section,
  mapping,
  editable,
  onMappingChange,
  onAccept,
  onReject,
}: {
  section: ReviewSectionDetail;
  mapping: ReviewMappingTable;
  editable: boolean;
  onMappingChange: (mapping: ReviewMappingTable) => void;
  onAccept: (excelRow: number, panel: number | null) => void;
  onReject: (excelRow: number, panel: number | null) => void;
}) {
  const [active, setActive] = useState<[number, number] | null>(null);
  const updateColumn = (column: ReviewMappingColumn) => {
    onMappingChange({
      ...mapping,
      columns: mapping.columns.map((candidate) => candidate.index === column.index ? column : candidate),
    });
  };
  const move = (row: number, column: number, key: string) => {
    const next: [number, number] = [row, column];
    if (key === 'ArrowUp') next[0] = Math.max(0, row - 1);
    if (key === 'ArrowDown') next[0] = Math.min(section.rows.length - 1, row + 1);
    if (key === 'ArrowLeft') next[1] = Math.max(0, column - 1);
    if (key === 'ArrowRight') next[1] = Math.min(mapping.columns.length - 1, column + 1);
    setActive(next);
  };
  return (
    <div className="review-grid-wrap" role="region" aria-label={`Исходная сетка ${section.sheet_name}`}>
      <table className="review-grid">
        <thead>
          <tr>
            <th className="review-grid-corner" aria-label="Номер строки" />
            {mapping.columns.map((column) => {
              const excel = section.rows[0]?.cells[column.index]?.excel_column ?? String(column.index + 1);
              const header = section.header_values[column.index] ?? 'Без заголовка';
              return (
                <th key={column.index} scope="col">
                  <ColumnRoleEditor
                    column={column}
                    table={mapping}
                    label={`${excel} ${header}`}
                    disabled={!editable}
                    onChange={updateColumn}
                  />
                </th>
              );
            })}
            <th scope="col" className="review-grid-actions">Решение</th>
          </tr>
        </thead>
        <tbody>
          {section.rows.map((row, rowPosition) => (
            <tr key={`${row.kind}-${row.row_index}`} className={row.kind === 'header' ? 'is-header' : undefined}>
              <th scope="row" aria-label={String(row.excel_row)}>{row.excel_row}</th>
              {mapping.columns.map((column, columnPosition) => {
                const cell = row.cells[column.index];
                const selected = active?.[0] === rowPosition && active[1] === columnPosition;
                return (
                  <td
                    key={column.index}
                    tabIndex={selected || (!active && rowPosition === 0 && columnPosition === 0) ? 0 : -1}
                    className={selected ? 'is-active' : undefined}
                    onFocus={() => setActive([rowPosition, columnPosition])}
                    onKeyDown={(event) => {
                      if (event.key.startsWith('Arrow')) {
                        event.preventDefault();
                        move(rowPosition, columnPosition, event.key);
                      }
                    }}
                  >
                    <span className={cell?.is_bold ? 'is-bold' : undefined}>
                      {cell?.is_merged_fill ? '↳' : String(cell?.value ?? '')}
                    </span>
                  </td>
                );
              })}
              <td className="review-grid-actions">
                {row.kind !== 'header' && (
                  <Space.Compact>
                    <Tooltip title="Принять строку">
                      <Button
                        size="small"
                        aria-label={`Принять строку ${row.excel_row}`}
                        icon={<CheckOutlined />}
                        disabled={!editable}
                        onClick={() => onAccept(row.excel_row, null)}
                      />
                    </Tooltip>
                    <Tooltip title="Отклонить строку">
                      <Button
                        size="small"
                        danger
                        aria-label={`Отклонить строку ${row.excel_row}`}
                        icon={<CloseOutlined />}
                        disabled={!editable}
                        onClick={() => onReject(row.excel_row, null)}
                      />
                    </Tooltip>
                  </Space.Compact>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
