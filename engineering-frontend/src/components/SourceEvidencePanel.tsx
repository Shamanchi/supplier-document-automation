import { Alert, Descriptions, Space, Typography } from 'antd';
import type { ImportedPriceItem, SourceEvidenceCell } from '../api/types';

function headerLabel(header: SourceEvidenceCell[] | null, index: number): string {
  const match = header?.find(([column]) => column === index);
  return match ? match[1] : `Колонка ${index + 1}`;
}

function RawCellsTable({
  header,
  cells,
}: {
  header: SourceEvidenceCell[] | null;
  cells: SourceEvidenceCell[];
}) {
  return (
    <Descriptions size="small" bordered column={1} colon={false}>
      {cells.map(([index, value]) => (
        <Descriptions.Item
          key={index}
          label={<Typography.Text type="secondary">{headerLabel(header, index)}</Typography.Text>}
        >
          <Typography.Text style={{ whiteSpace: 'pre-wrap' }}>{value}</Typography.Text>
        </Descriptions.Item>
      ))}
    </Descriptions>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <Typography.Text strong style={{ display: 'block', marginBottom: 4 }}>
      {children}
    </Typography.Text>
  );
}

export default function SourceEvidencePanel({ item }: { item: ImportedPriceItem }) {
  const evidence = item.source_evidence;
  if (!evidence || (!evidence.data && !evidence.header && !evidence.categories)) {
    return (
      <Alert
        type="info"
        showIcon
        message="Сырые данные ещё не сняты"
        description="Эта позиция загружена до появления следа источника. Переимпортируйте прайс, чтобы увидеть исходные строки."
      />
    );
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Typography.Text type="secondary">
        Исходный документ: {item.source_sheet}, строка {item.source_row}
        {item.source_panel_index != null ? `, панель ${item.source_panel_index + 1}` : ''}
      </Typography.Text>

      <div>
        <SectionTitle>Raw-data — строка позиции</SectionTitle>
        {evidence.data?.length ? (
          <RawCellsTable header={evidence.header} cells={evidence.data} />
        ) : (
          <Typography.Text type="secondary">нет</Typography.Text>
        )}
      </div>

      <div>
        <SectionTitle>Raw-header — заголовки колонок</SectionTitle>
        {evidence.header?.length ? (
          <Descriptions size="small" bordered column={1} colon={false}>
            {evidence.header.map(([index, value]) => (
              <Descriptions.Item
                key={index}
                label={<Typography.Text type="secondary">Колонка {index + 1}</Typography.Text>}
              >
                <Typography.Text style={{ whiteSpace: 'pre-wrap' }}>{value}</Typography.Text>
              </Descriptions.Item>
            ))}
          </Descriptions>
        ) : (
          <Typography.Text type="secondary">нет</Typography.Text>
        )}
      </div>

      <div>
        <SectionTitle>Raw-category — строки категории</SectionTitle>
        {evidence.categories?.length ? (
          <Space direction="vertical" size="small" style={{ width: '100%' }}>
            {evidence.categories.map((cells, rowIndex) => (
              <RawCellsTable key={rowIndex} header={evidence.header} cells={cells} />
            ))}
          </Space>
        ) : (
          <Typography.Text type="secondary">
            категория взята из строки позиции или имени листа
          </Typography.Text>
        )}
      </div>
    </Space>
  );
}
