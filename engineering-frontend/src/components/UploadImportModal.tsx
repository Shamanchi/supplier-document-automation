import { InboxOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Form, Input, Modal, Select, Upload, message } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { createPriceImport, extractApiError, fetchPriceImportOptions } from '../api/client';

interface FormValues {
  supplierExternalId: string;
  supplierName?: string;
  currencyHint?: string;
  provider?: string;
  model?: string;
}

function newIdempotencyKey(): string {
  if (typeof crypto.randomUUID === 'function') return `ui-${crypto.randomUUID()}`;
  return `ui-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export default function UploadImportModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (importId: string) => void;
}) {
  const [form] = Form.useForm<FormValues>();
  const [file, setFile] = useState<File | null>(null);
  const [idempotencyKey, setIdempotencyKey] = useState(newIdempotencyKey);
  const queryClient = useQueryClient();
  const providerName = Form.useWatch('provider', form);
  const { data: options } = useQuery({
    queryKey: ['price-import-options'],
    queryFn: fetchPriceImportOptions,
    enabled: open,
  });
  const enabledProviders = useMemo(
    () => options?.providers.filter((provider) => provider.enabled) ?? [],
    [options],
  );
  const selectedProvider = enabledProviders.find(
    (provider) => provider.provider === providerName,
  );

  useEffect(() => {
    if (!open || !options || form.getFieldValue('provider')) return;
    const provider =
      enabledProviders.find((item) => item.provider === options.defaults.provider) ??
      enabledProviders[0];
    if (provider) {
      form.setFieldsValue({ provider: provider.provider, model: provider.default_model });
    }
  }, [enabledProviders, form, open, options]);

  const mutation = useMutation({
    mutationFn: async (values: FormValues) => {
      if (!file) throw new Error('Выберите файл прайса');
      return createPriceImport({
        ...values,
        file,
        idempotencyKey,
      });
    },
    onSuccess: async (result) => {
      message.success(result.replayed ? 'Открыт уже существующий импорт' : 'Прайс принят в обработку');
      await queryClient.invalidateQueries({ queryKey: ['price-imports'] });
      form.resetFields();
      setFile(null);
      setIdempotencyKey(newIdempotencyKey());
      onCreated(result.import_id);
    },
    onError: (error) => message.error(extractApiError(error)),
  });

  return (
    <Modal
      title="Загрузить прайс"
      open={open}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText="Загрузить и разобрать"
      cancelText="Отмена"
      confirmLoading={mutation.isPending}
      width={640}
      destroyOnClose={false}
    >
      <Form form={form} layout="vertical" onFinish={(values) => mutation.mutate(values)}>
        <Form.Item label="Файл" required>
          <Upload.Dragger
            accept=".xls,.xlsx,.csv,.pdf,.doc,.docx,.txt"
            maxCount={1}
            beforeUpload={(nextFile) => {
              setFile(nextFile);
              setIdempotencyKey(newIdempotencyKey());
              return false;
            }}
            onRemove={() => {
              setFile(null);
              setIdempotencyKey(newIdempotencyKey());
            }}
          >
            <p className="ant-upload-drag-icon"><InboxOutlined /></p>
            <p>Перетащите прайс сюда или нажмите для выбора</p>
          </Upload.Dragger>
        </Form.Item>
        <Form.Item
          name="supplierExternalId"
          label="Код поставщика"
          rules={[{ required: true, whitespace: true, message: 'Укажите код поставщика' }]}
        >
          <Input maxLength={255} placeholder="например, global-security-kz" />
        </Form.Item>
        <Form.Item name="supplierName" label="Название поставщика">
          <Input maxLength={255} placeholder="Если не указано, используется код" />
        </Form.Item>
        <Form.Item name="currencyHint" label="Валюта, если в файле не указана">
          <Select
            allowClear
            showSearch
            placeholder="Не подставлять валюту"
            options={['KZT', 'RUB', 'USD', 'EUR', 'CNY', 'BYN', 'UZS'].map((value) => ({
              value,
              label: value,
            }))}
          />
        </Form.Item>
        <Form.Item name="provider" label="LLM-провайдер">
          <Select
            allowClear
            placeholder="Серверный профиль по умолчанию"
            options={enabledProviders.map((provider) => ({
              value: provider.provider,
              label: provider.label,
            }))}
            onChange={(value) => {
              const provider = enabledProviders.find((item) => item.provider === value);
              form.setFieldValue('model', provider?.default_model);
            }}
          />
        </Form.Item>
        <Form.Item name="model" label="Модель">
          <Select
            allowClear
            disabled={!selectedProvider}
            placeholder="Модель профиля по умолчанию"
            options={selectedProvider?.models.map((model) => ({ value: model, label: model }))}
          />
        </Form.Item>
      </Form>
    </Modal>
  );
}
