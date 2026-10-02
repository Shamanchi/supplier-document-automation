import { useState } from 'react'
import { InboxOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Card, Input, Space, Table, Tag, Upload } from 'antd'

import {
  fetchRegistryImports,
  uploadRegistryFile,
  type RegistryImportItem,
} from '../api/client'

const KIND_LABEL: Record<RegistryImportItem['kind'], string> = {
  index: 'карточки (CSV)',
  numbers: 'список номеров',
  archive: 'архив набора (7z)',
}

const STATUS_COLOR: Record<RegistryImportItem['status'], string> = {
  queued: 'default',
  running: 'processing',
  completed: 'green',
  failed: 'red',
}

const STATUS_LABEL: Record<RegistryImportItem['status'], string> = {
  queued: 'в очереди',
  running: 'обрабатывается',
  completed: 'загружен',
  failed: 'ошибка',
}

export default function RegistryImportsPage() {
  const [note, setNote] = useState('')
  const client = useQueryClient()

  const imports = useQuery({
    queryKey: ['registry-imports'],
    queryFn: fetchRegistryImports,
    // Импорт идёт в очереди, поэтому список сам догоняет статус.
    refetchInterval: (query) =>
      (query.state.data?.items ?? []).some((item) =>
        item.status === 'queued' || item.status === 'running',
      )
        ? 3000
        : false,
  })

  const upload = useMutation({
    mutationFn: ({ file }: { file: File }) => uploadRegistryFile(file, note),
    onSuccess: () => client.invalidateQueries({ queryKey: ['registry-imports'] }),
  })

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message="Файлы реестра добавляются вручную"
        description="Принимаются CSV с карточками, список номеров и 7z-архив набора открытых данных. Вид определяется по содержимому файла, расширение — только подсказка. Импорт идёт в фоне."
      />

      <Card title="Загрузка файла реестра">
        <Input
          placeholder="комментарий, например «срез за январь 2026»"
          value={note}
          onChange={(event) => setNote(event.target.value)}
          style={{ marginBottom: 16, maxWidth: 420 }}
        />
        <Upload.Dragger
          multiple
          accept=".csv,.txt,.7z"
          showUploadList={false}
          customRequest={async (options) => {
            try {
              const result = await upload.mutateAsync({ file: options.file as File })
              options.onSuccess?.(result)
            } catch (error) {
              options.onError?.(error as Error)
            }
          }}
        >
          <p className="ant-upload-drag-icon">
            <InboxOutlined />
          </p>
          <p className="ant-upload-text">Перетащите файл реестра сюда</p>
          <p className="ant-upload-hint">CSV карточек · список номеров · 7z-архив набора</p>
        </Upload.Dragger>
      </Card>

      <Card title="История загрузок">
        <Table<RegistryImportItem>
          rowKey="id"
          loading={imports.isLoading}
          dataSource={imports.data?.items ?? []}
          pagination={{ pageSize: 15, showSizeChanger: false }}
          columns={[
            { title: 'Файл', dataIndex: 'filename' },
            {
              title: 'Вид',
              dataIndex: 'kind',
              width: 190,
              render: (value: RegistryImportItem['kind']) => KIND_LABEL[value],
            },
            {
              title: 'Статус',
              dataIndex: 'status',
              width: 160,
              render: (value: RegistryImportItem['status']) => (
                <Tag color={STATUS_COLOR[value]}>{STATUS_LABEL[value]}</Tag>
              ),
            },
            { title: 'Карточек', dataIndex: 'certificates_loaded', width: 110 },
            { title: 'Номеров', dataIndex: 'numbers_loaded', width: 110 },
            { title: 'Комментарий', dataIndex: 'note', ellipsis: true },
            {
              title: 'Ошибка',
              dataIndex: 'error',
              ellipsis: true,
              render: (value: string) => value || '—',
            },
          ]}
        />
      </Card>
    </Space>
  )
}
