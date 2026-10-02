import { Alert, Button, Form, Input, Modal, Select, Typography } from 'antd';
import { useEffect } from 'react';
import type { ReviewBlockedCell, ReviewDecision } from '../../api/types';

const REASON_OPTIONS = [
  { value: 'operator_review', label: 'Проверено оператором' },
  { value: 'supplier_card', label: 'Карточка поставщика' },
  { value: 'technical_table', label: 'Техническая таблица' },
  { value: 'note_or_footer', label: 'Примечание / подвал' },
];

/** Причина, которой нельзя подтверждать обход: она ничего не объясняет. */
const GENERIC_REASON_CODE = 'operator_review';

export default function RowDecisionControl({
  open,
  sheet,
  excelRow,
  panel,
  pending,
  blocked = [],
  onCancel,
  onConfirm,
}: {
  open: boolean;
  sheet: string;
  excelRow: number | null;
  panel: number | null;
  pending: boolean;
  /** Заблокированные гардом ячейки из последнего отказа — включает шаг обхода. */
  blocked?: ReviewBlockedCell[];
  onCancel: () => void;
  onConfirm: (decision: ReviewDecision) => void;
}) {
  const [form] = Form.useForm<{ decision: ReviewDecision['decision']; reason_code: string; comment: string }>();
  const reasonCode = Form.useWatch('reason_code', form);
  useEffect(() => {
    if (open) form.setFieldsValue({ decision: 'not_price_section', reason_code: GENERIC_REASON_CODE, comment: '' });
  }, [form, open]);

  const override = blocked.length > 0;
  const overrideReady = reasonCode !== undefined && reasonCode !== GENERIC_REASON_CODE;
  const submit = (overrideGuard: boolean) => void form.validateFields().then((values) => {
    if (excelRow === null) return;
    onConfirm({
      sheet,
      row_start: excelRow,
      row_end: excelRow,
      panel_index: panel,
      ...values,
      ...(overrideGuard ? { override_guard: true } : {}),
    });
  });

  return (
    <Modal
      title={`Отклонить строку ${excelRow ?? ''}`}
      open={open}
      confirmLoading={pending}
      okText="Сохранить решение"
      cancelText="Отмена"
      onCancel={onCancel}
      footer={override ? [
        <Button key="cancel" onClick={onCancel}>Отмена</Button>,
        <Button
          key="override"
          danger
          disabled={!overrideReady}
          loading={pending}
          onClick={() => submit(true)}
        >Обойти защиту и отклонить</Button>,
      ] : undefined}
      onOk={() => submit(false)}
    >
      <Form form={form} layout="vertical">
        <Form.Item name="decision" label="Решение" rules={[{ required: true }]}>
          <Select options={[
            { value: 'not_price_section', label: 'Не является прайс-секцией' },
            { value: 'catalog_without_price', label: 'Каталог без цены' },
          ]} />
        </Form.Item>
        <Form.Item name="reason_code" label="Код причины" rules={[{ required: true }]}>
          <Select options={REASON_OPTIONS} />
        </Form.Item>
        <Form.Item name="comment" label="Комментарий" rules={[{ required: true, min: 3 }]}>
          <Input.TextArea rows={3} placeholder="Почему эта строка не должна создавать позицию" />
        </Form.Item>
      </Form>
      {override && (
        <Alert
          type="warning"
          showIcon
          message="Решение скрывает ценовой признак"
          description={
            <>
              <Typography.Paragraph style={{ marginBottom: 4 }}>
                Гард не даёт молча выкинуть строку, похожую на настоящую цену. Заблокированные ячейки:
              </Typography.Paragraph>
              <ul style={{ margin: '0 0 8px', paddingLeft: 20 }}>
                {blocked.map((cell, index) => (
                  <li key={`${cell.sheet}-${cell.row}-${cell.column}-${index}`}>
                    лист «{cell.sheet}», строка {cell.row}, колонка {cell.column}
                    {cell.value ? `: ${cell.value}` : ''}
                  </li>
                ))}
              </ul>
              <Typography.Text type={overrideReady ? 'secondary' : 'danger'}>
                {overrideReady
                  ? 'Обход попадёт в журнал review вместе с причиной и координатами.'
                  : 'Для обхода выберите конкретную причину: «Проверено оператором» не подходит.'}
              </Typography.Text>
            </>
          }
        />
      )}
    </Modal>
  );
}
