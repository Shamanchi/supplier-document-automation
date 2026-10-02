import { ArrowLeftOutlined } from '@ant-design/icons';
import { useQuery } from '@tanstack/react-query';
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Progress,
  Row,
  Space,
  Spin,
  Statistic,
  Tabs,
  Typography,
} from 'antd';
import { Link, useParams } from 'react-router-dom';
import { useState } from 'react';
import { extractApiError, fetchPriceImport } from '../api/client';
import { TERMINAL_IMPORT_STATUSES } from '../api/types';
import ImportIssuesTable from '../components/ImportIssuesTable';
import ImportItemsTable from '../components/ImportItemsTable';
import ImportStatusTag from '../components/ImportStatusTag';
import ImportReview from '../components/review/ImportReview';
import { formatDate, metric } from '../components/importFormat';

export default function ImportPage() {
  const { id = '' } = useParams<{ id: string }>();
  const [activeTab, setActiveTab] = useState('items');
  const query = useQuery({
    queryKey: ['price-import', id],
    queryFn: () => fetchPriceImport(id),
    enabled: Boolean(id),
    refetchInterval: (result) =>
      result.state.data && !TERMINAL_IMPORT_STATUSES.has(result.state.data.status)
        ? 2_000
        : false,
  });

  if (query.isError) {
    return <Alert type="error" showIcon message="Импорт не загружен" description={extractApiError(query.error)} />;
  }
  if (query.isLoading || !query.data) {
    return <Spin tip="Загрузка импорта…" style={{ display: 'block', margin: '80px auto' }} />;
  }
  const value = query.data;
  const terminal = TERMINAL_IMPORT_STATUSES.has(value.status);
  const alert = value.status === 'review_required'
    ? { type: 'warning' as const, message: 'Результат требует проверки', description: 'Частичный результат не принят как успешный. Проверьте замечания ниже.' }
    : value.status === 'needs_ocr'
      ? { type: 'warning' as const, message: 'Нужен OCR', description: 'Файл является сканом и не разбирается без OCR.' }
      : value.status === 'failed'
        ? { type: 'error' as const, message: value.error_code ?? 'Ошибка импорта', description: value.error_message ?? 'Обработка не завершена.' }
        : null;

  return (
    <div>
      <Link to="/"><Button type="link" icon={<ArrowLeftOutlined />} style={{ paddingInline: 0 }}>Все импорты</Button></Link>
      <Space style={{ width: '100%', justifyContent: 'space-between' }} align="start" wrap>
        <div>
          <Typography.Title level={3} style={{ margin: '8px 0 0' }}>{value.filename}</Typography.Title>
          <Typography.Text type="secondary">{value.supplier_name} · {value.supplier_external_id ?? 'без внешнего кода'}</Typography.Text>
        </div>
        <ImportStatusTag status={value.status} />
      </Space>
      {!terminal && (
        <Card size="small" style={{ marginTop: 16 }}>
          <Typography.Text>Обработка продолжается автоматически</Typography.Text>
          <Progress percent={value.progress_percent} status="active" />
        </Card>
      )}
      {alert && <Alert showIcon style={{ marginTop: 16 }} {...alert} />}
      <Descriptions bordered size="small" column={{ xs: 1, md: 2, lg: 3 }} style={{ marginTop: 16 }}>
        <Descriptions.Item label="Создан">{formatDate(value.created_at)}</Descriptions.Item>
        <Descriptions.Item label="Завершён">{formatDate(value.finished_at)}</Descriptions.Item>
        <Descriptions.Item label="ID импорта"><Typography.Text copyable code>{value.id}</Typography.Text></Descriptions.Item>
        <Descriptions.Item label="Позиций">{value.item_count.toLocaleString('ru-RU')}</Descriptions.Item>
        <Descriptions.Item label="Ошибок">{value.errors_count}</Descriptions.Item>
        <Descriptions.Item label="Предупреждений">{value.warnings_count}</Descriptions.Item>
      </Descriptions>
      <Row gutter={[16, 16]} style={{ marginTop: 16 }}>
        <Col xs={24} sm={12} lg={6}><Card><Statistic title="Input tokens" value={metric(value.telemetry, 'input_tokens')} /></Card></Col>
        <Col xs={24} sm={12} lg={6}><Card><Statistic title="Output tokens" value={metric(value.telemetry, 'output_tokens')} /></Card></Col>
        <Col xs={24} sm={12} lg={6}><Card><Statistic title="Cached input" value={metric(value.telemetry, 'cached_input_tokens')} /></Card></Col>
        <Col xs={24} sm={12} lg={6}><Card><Statistic title="LLM latency, мс" value={metric(value.telemetry, 'llm_latency_ms')} /></Card></Col>
      </Row>
      {terminal && (
        <Tabs
          style={{ marginTop: 16 }}
          activeKey={activeTab}
          onChange={setActiveTab}
          items={[
            { key: 'items', label: `Позиции (${value.item_count})`, children: <ImportItemsTable importId={value.id} /> },
            {
              key: 'issues',
              label: `Замечания (${value.errors_count + value.warnings_count})`,
              children: <ImportIssuesTable importId={value.id} onOpenReview={() => setActiveTab('review')} />,
            },
            {
              key: 'quality',
              label: 'Контроль качества',
              children: (
                <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
                  {JSON.stringify(value.completeness ?? {}, null, 2)}
                </pre>
              ),
            },
            ...(['review_required', 'completed', 'failed'].includes(value.status)
              ? [{ key: 'review', label: 'Проверка и правило', children: <ImportReview importId={value.id} /> }]
              : []),
          ]}
        />
      )}
    </div>
  );
}
