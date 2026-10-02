import { ApiOutlined, FileTextOutlined, PlusOutlined, StopOutlined, SyncOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, App, Button, Card, Col, Form, Input, InputNumber, Modal, Popconfirm, Row, Space, Table, Tag, Typography } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useState } from 'react';
import {
  createServiceClient,
  extractApiError,
  fetchServiceClients,
  revokeServiceClient,
  rotateServiceClient,
} from '../api/client';
import type { ServiceClient, ServiceClientSecret } from '../api/types';
import { formatDate } from '../components/importFormat';

interface ClientForm {
  name: string;
  requestsPerMinute: number;
  maxConcurrentImports: number;
}

export default function ApiAccessPage() {
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<ClientForm>();
  const [createOpen, setCreateOpen] = useState(false);
  const [secret, setSecret] = useState<ServiceClientSecret>();
  const query = useQuery({ queryKey: ['service-clients'], queryFn: fetchServiceClients });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['service-clients'] });
  const create = useMutation({
    mutationFn: (values: ClientForm) => createServiceClient(values),
    onSuccess: async (result) => {
      setCreateOpen(false);
      form.resetFields();
      setSecret(result);
      await refresh();
    },
    onError: (error) => void message.error(extractApiError(error)),
  });
  const rotate = useMutation({
    mutationFn: rotateServiceClient,
    onSuccess: async (result) => { setSecret(result); await refresh(); },
    onError: (error) => void message.error(extractApiError(error)),
  });
  const revoke = useMutation({
    mutationFn: revokeServiceClient,
    onSuccess: async () => { await refresh(); void message.success('API-ключ отозван'); },
    onError: (error) => void message.error(extractApiError(error)),
  });

  const columns: ColumnsType<ServiceClient> = [
    {
      title: 'Интеграция', key: 'name',
      render: (_, row) => <Space direction="vertical" size={1}><Typography.Text strong>{row.name}</Typography.Text><Typography.Text code>{row.key_prefix}…</Typography.Text></Space>,
    },
    { title: 'Статус', dataIndex: 'is_active', key: 'status', width: 110, render: (value: boolean) => <Tag color={value ? 'green' : 'default'}>{value ? 'Активен' : 'Отозван'}</Tag> },
    { title: 'Запросов/мин', dataIndex: 'requests_per_minute', key: 'rpm', width: 130, align: 'right' },
    { title: 'Параллельных импортов', dataIndex: 'max_concurrent_imports', key: 'concurrency', width: 190, align: 'right' },
    { title: 'Последний запрос', dataIndex: 'last_used_at', key: 'last_used', width: 180, render: (value: string | null) => value ? formatDate(value) : 'Ещё не использовался' },
    {
      title: 'Действия', key: 'actions', width: 220,
      render: (_, row) => (
        <Space wrap>
          <Popconfirm title="Выпустить новый ключ?" description="Старый ключ сразу перестанет работать." onConfirm={() => rotate.mutate(row.id)} okText="Перевыпустить" cancelText="Отмена">
            <Button size="small" icon={<SyncOutlined />}>Новый ключ</Button>
          </Popconfirm>
          {row.is_active && (
            <Popconfirm title="Отозвать API-ключ?" onConfirm={() => revoke.mutate(row.id)} okText="Отозвать" cancelText="Отмена">
              <Button size="small" danger icon={<StopOutlined />}>Отозвать</Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ];

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Space style={{ width: '100%', justifyContent: 'space-between' }} align="start" wrap>
        <div>
          <Typography.Title level={3} style={{ margin: 0 }}>API и интеграции</Typography.Title>
          <Typography.Text type="secondary">Отдельные ключи, лимиты и webhook-секреты для внешних систем загрузки прайсов.</Typography.Text>
        </div>
        <Space wrap>
          <Button href="/api/docs" target="_blank" rel="noreferrer" icon={<ApiOutlined />}>Swagger UI</Button>
          <Button href="/api/openapi.json" target="_blank" rel="noreferrer" icon={<FileTextOutlined />}>OpenAPI JSON</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>Новая интеграция</Button>
        </Space>
      </Space>
      <Alert showIcon type="info" message="Основной поток API" description="POST /api/v1/price-imports принимает файл и возвращает import_id. Затем GET /price-imports/{id} показывает статус, /items — позиции с пагинацией, /issues — замечания. Повтор запроса защищён Idempotency-Key." />
      <Row gutter={[12, 12]}>
        {[
          ['1. Загрузить', 'POST /api/v1/price-imports', 'multipart/form-data + Idempotency-Key'],
          ['2. Следить', 'GET /api/v1/price-imports/{id}', 'статус, прогресс и ошибки'],
          ['3. Забрать результат', 'GET …/{id}/items', 'limit/offset, цены и категории'],
        ].map(([title, endpoint, copy]) => (
          <Col xs={24} lg={8} key={title}><Card size="small" title={title}><Typography.Text code copyable>{endpoint}</Typography.Text><Typography.Paragraph type="secondary" style={{ margin: '8px 0 0' }}>{copy}</Typography.Paragraph></Card></Col>
        ))}
      </Row>
      <Typography.Title level={4} style={{ marginBottom: 0 }}>Ключи интеграций</Typography.Title>
      {query.isError && <Alert type="error" showIcon message="API-клиенты не загружены" description={extractApiError(query.error)} />}
      <Table<ServiceClient> rowKey="id" columns={columns} dataSource={query.data ?? []} loading={query.isFetching} scroll={{ x: 1050 }} pagination={false} locale={{ emptyText: 'Внешних интеграций пока нет' }} />
      <Modal title="Новая API-интеграция" open={createOpen} okText="Создать и показать ключ" cancelText="Отмена" confirmLoading={create.isPending} onOk={() => form.submit()} onCancel={() => setCreateOpen(false)}>
        <Form<ClientForm> form={form} layout="vertical" initialValues={{ requestsPerMinute: 60, maxConcurrentImports: 4 }} onFinish={(values) => create.mutate(values)}>
          <Form.Item name="name" label="Название системы" rules={[{ required: true, message: 'Укажите название' }]}><Input placeholder="Например, Bitrix24 production" /></Form.Item>
          <Form.Item name="requestsPerMinute" label="Запросов в минуту" rules={[{ required: true }]}><InputNumber min={1} max={10000} style={{ width: '100%' }} /></Form.Item>
          <Form.Item name="maxConcurrentImports" label="Одновременных импортов" rules={[{ required: true }]}><InputNumber min={1} max={100} style={{ width: '100%' }} /></Form.Item>
        </Form>
      </Modal>
      <Modal title="Сохраните секреты сейчас" open={Boolean(secret)} okText="Я сохранил" cancelButtonProps={{ style: { display: 'none' } }} onOk={() => setSecret(undefined)} closable={false} maskClosable={false}>
        <Alert showIcon type="warning" message="API-ключ показывается только один раз" style={{ marginBottom: 16 }} />
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text strong>API key</Typography.Text>
          <Input.TextArea value={secret?.api_key} readOnly autoSize />
          <Typography.Text copyable={{ text: secret?.api_key }}>Скопировать API key</Typography.Text>
          <Typography.Text strong>Webhook secret</Typography.Text>
          <Input.TextArea value={secret?.webhook_secret} readOnly autoSize />
          <Typography.Text copyable={{ text: secret?.webhook_secret }}>Скопировать webhook secret</Typography.Text>
        </Space>
      </Modal>
    </Space>
  );
}
