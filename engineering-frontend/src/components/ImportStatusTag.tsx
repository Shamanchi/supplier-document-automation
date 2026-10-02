import {
  CheckCircleOutlined,
  CloseCircleOutlined,
  ExclamationCircleOutlined,
  ScanOutlined,
  SyncOutlined,
} from '@ant-design/icons';
import { Tag } from 'antd';
import type { PriceImportStatusName } from '../api/types';

const STATUS: Record<
  PriceImportStatusName,
  { color: string; label: string; icon: React.ReactNode }
> = {
  queued: { color: 'blue', label: 'В очереди', icon: <SyncOutlined spin /> },
  parsing: { color: 'processing', label: 'Разбор файла', icon: <SyncOutlined spin /> },
  generating_rule: { color: 'processing', label: 'Создание правила', icon: <SyncOutlined spin /> },
  compiling_rule: { color: 'processing', label: 'Проверка правила', icon: <SyncOutlined spin /> },
  reparsing: { color: 'processing', label: 'Повторный разбор', icon: <SyncOutlined spin /> },
  quality_check: { color: 'processing', label: 'Контроль качества', icon: <SyncOutlined spin /> },
  completed: { color: 'green', label: 'Готово', icon: <CheckCircleOutlined /> },
  review_required: {
    color: 'orange',
    label: 'Нужна проверка',
    icon: <ExclamationCircleOutlined />,
  },
  needs_ocr: { color: 'gold', label: 'Нужен OCR', icon: <ScanOutlined /> },
  failed: { color: 'red', label: 'Ошибка', icon: <CloseCircleOutlined /> },
};

export default function ImportStatusTag({ status }: { status: PriceImportStatusName }) {
  const value = STATUS[status];
  return (
    <Tag color={value.color} icon={value.icon}>
      {value.label}
    </Tag>
  );
}

export function importStatusLabel(status: PriceImportStatusName): string {
  return STATUS[status].label;
}
