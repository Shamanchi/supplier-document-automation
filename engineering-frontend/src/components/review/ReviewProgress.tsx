import { Card, Col, Row, Statistic } from 'antd';

const LABELS: Record<string, string> = {
  pending: 'Ожидают',
  in_progress: 'В работе',
  validated: 'Проверены',
  accepted: 'Приняты',
  rejected: 'Отклонены',
};

export default function ReviewProgress({ totals }: { totals: Record<string, number> }) {
  return (
    <Row gutter={[8, 8]} className="review-progress">
      {Object.entries(LABELS).map(([status, label]) => (
        <Col xs={12} md={8} xl={4} key={status}>
          <Card size="small"><Statistic title={label} value={totals[status] ?? 0} /></Card>
        </Col>
      ))}
    </Row>
  );
}
