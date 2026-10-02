import { Alert, Card, Empty, Space, Tag, Typography } from 'antd';
import type { ReviewValidation } from '../../api/types';
import { projectValidationDiff } from './reviewModel';

const COLORS = { added: 'green', removed: 'red', changed: 'gold', unchanged: 'default' } as const;

export default function ValidationDiff({ validation }: { validation: ReviewValidation | null }) {
  if (!validation) return <Empty description="Текущая revision ещё не проверена" />;
  const rows = projectValidationDiff(validation.diff);
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Alert
        type={validation.passed ? 'success' : 'error'}
        showIcon
        message={validation.passed ? 'Dry-run прошёл' : 'Dry-run содержит блокирующие ошибки'}
        description={`Revision ${validation.revision}`}
      />
      <div className="review-diff">
        <Card size="small" title="Source coordinates">
          {rows.length ? rows.map((row) => (
            <div className="review-diff-row" key={`${row.kind}-${row.key}`}>
              <Tag color={COLORS[row.kind]}>{row.kind}</Tag>
              <Typography.Text code>{row.sheet}!{row.row}{row.panel === null ? '' : ` · panel ${row.panel + 1}`}</Typography.Text>
              <Typography.Text type="secondary">{row.items.length} result item(s)</Typography.Text>
            </div>
          )) : <Typography.Text type="secondary">Coordinate diff не содержит построчных изменений.</Typography.Text>}
        </Card>
        <Card size="small" title="Результат и quality gates">
          <pre className="review-json-diagnostics">{JSON.stringify(validation.metrics, null, 2)}</pre>
          <pre className="review-json-diagnostics">{JSON.stringify(validation.diff, null, 2)}</pre>
        </Card>
      </div>
    </Space>
  );
}
