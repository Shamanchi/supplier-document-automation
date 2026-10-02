import { EyeOutlined } from '@ant-design/icons';
import { Alert, Button, Modal, Space, Spin, Table, Tag, Typography } from 'antd';
import { useState } from 'react';
import { extractApiError, fetchRuleMatrixPreview } from '../../api/client';
import type { Recipe, RuleMatrixPreview, RuleValidationSample } from '../../api/types';
import './matrixPreview.css';

function priceText(sample: RuleValidationSample): string {
  return sample.prices.map((price) => {
    const amount = price.is_on_request ? 'по запросу' : (price.amount ?? '—');
    return `${price.kind}: ${amount}${price.currency ? ` ${price.currency}` : ''}`;
  }).join(' · ');
}

export default function MatrixPreviewModal({
  ruleId,
  priceVersionId,
  sourceSectionId,
  recipe,
}: {
  ruleId: string;
  priceVersionId: string | null;
  sourceSectionId: string;
  recipe: Recipe;
}) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<RuleMatrixPreview | null>(null);

  const show = async () => {
    setOpen(true);
    if (!priceVersionId) return;
    setLoading(true);
    setError(null);
    try {
      setPreview(await fetchRuleMatrixPreview(ruleId, {
        priceVersionId,
        sourceSectionId,
        recipe,
      }));
    } catch (reason) {
      setError(extractApiError(reason));
    } finally {
      setLoading(false);
    }
  };

  const parsedCells = new Set((preview?.items ?? []).flatMap((item) => (
    item.prices
      .filter((price) => price.source_column_index !== null)
      .map((price) => `${item.source_row}:${price.source_column_index}`)
  )));
  const columns = preview?.rows[0]?.cells.map((cell) => cell.excel_column) ?? [];

  return (
    <>
      <Button icon={<EyeOutlined />} disabled={!priceVersionId} onClick={() => void show()}>
        Просмотреть матрицу и результат
      </Button>
      <Modal
        open={open}
        width="min(96vw, 1500px)"
        title="Проверка матричного разворота"
        footer={<Button onClick={() => setOpen(false)}>Закрыть</Button>}
        onCancel={() => setOpen(false)}
        destroyOnHidden={false}
      >
        {!priceVersionId && (
          <Alert type="warning" showIcon message="У правила не выбран исходный прайс" />
        )}
        {loading && <div className="matrix-preview-loading"><Spin size="large" /></div>}
        {error && <Alert type="error" showIcon message="Не удалось построить просмотр" description={error} />}
        {preview && !loading && (
          <Space direction="vertical" size="large" style={{ width: '100%' }}>
            <section>
              <Space wrap align="baseline">
                <Typography.Title level={4} style={{ margin: 0 }}>Исходная матрица</Typography.Title>
                <Tag color="blue">{preview.sheet_name}</Tag>
                <Typography.Text type="secondary">
                  строки {preview.row_offset + 1}–{preview.row_offset + preview.total_rows}
                </Typography.Text>
              </Space>
              <Typography.Paragraph type="secondary">
                Зелёным отмечены ценовые ячейки, которые породили позиции.
              </Typography.Paragraph>
              <div className="matrix-preview-grid-wrap" role="region" aria-label="Исходная матрица с заголовками">
                <table className="matrix-preview-grid">
                  <thead>
                    <tr><th className="matrix-preview-corner" />{columns.map((label) => <th key={label}>{label}</th>)}</tr>
                  </thead>
                  <tbody>
                    {preview.rows.map((row) => (
                      <tr key={row.row_index}>
                        <th scope="row">{row.excel_row}</th>
                        {row.cells.map((cell) => (
                          <td
                            key={cell.column_index}
                            className={[
                              parsedCells.has(`${row.excel_row}:${cell.column_index}`) ? 'is-parsed' : '',
                              cell.is_merged_fill ? 'is-merged' : '',
                            ].filter(Boolean).join(' ') || undefined}
                          >
                            {cell.is_merged_fill ? '↳' : (cell.value ?? '')}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
            <section>
              <Space wrap align="baseline">
                <Typography.Title level={4} style={{ margin: 0 }}>Результат разбора</Typography.Title>
                <Tag color="green">{preview.total_items.toLocaleString('ru-RU')} позиций</Tag>
              </Space>
              <Typography.Paragraph type="secondary">
                Полный результат текущих настроек. Координата ведёт к исходной ценовой ячейке сверху.
              </Typography.Paragraph>
              <Table<RuleValidationSample>
                size="small"
                rowKey={(item) => `${item.source_row}:${item.prices[0]?.source_column_index ?? 'none'}:${item.name}`}
                dataSource={preview.items}
                pagination={{ pageSize: 25, showSizeChanger: true, pageSizeOptions: [25, 50, 100], showTotal: (total) => `Всего ${total}` }}
                scroll={{ x: 1100 }}
                columns={[
                  {
                    title: 'Ячейка', width: 90, fixed: 'left',
                    render: (_, item) => `${item.prices[0]?.source_column_index === null ? '?' : columns[item.prices[0]?.source_column_index ?? -1] ?? '?'}${item.source_row}`,
                  },
                  { title: 'Позиция', dataIndex: 'name', width: 320 },
                  { title: 'Артикул', dataIndex: 'article', width: 120, render: (value) => value ?? '—' },
                  { title: 'Категория', dataIndex: 'category', width: 220, render: (value) => value ?? '—' },
                  {
                    title: 'Характеристики', width: 300,
                    render: (_, item) => item.attributes.length > 0
                      ? item.attributes.map((attribute) => `${attribute.name}: ${attribute.raw_value}${attribute.unit ? ` ${attribute.unit}` : ''}`).join(' · ')
                      : '—',
                  },
                  { title: 'Цена', width: 230, render: (_, item) => priceText(item) },
                ]}
              />
            </section>
          </Space>
        )}
      </Modal>
    </>
  );
}
