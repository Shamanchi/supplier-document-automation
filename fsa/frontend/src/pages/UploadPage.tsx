import { useState } from 'react'
import { InboxOutlined } from '@ant-design/icons'
import { Alert, Card, Input, Space, Table, Tag, Upload } from 'antd'

import { uploadDocument, type DocumentItem, type Verdict } from '../api/client'
import { VERDICT_COLOR, VERDICT_LABEL } from '../api/verdict'

interface Row {
  key: string
  filename: string
  reused: boolean
  document: DocumentItem
}

export default function UploadPage() {
  const [supplier, setSupplier] = useState('')
  const [rows, setRows] = useState<Row[]>([])

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message="Повторная загрузка того же файла не запускает распознавание заново"
        description="Документ опознаётся по содержимому (sha256), поэтому один и тот же сертификат у разных поставщиков обрабатывается один раз."
      />

      <Card title="Загрузка документов">
        <Input
          placeholder="поставщик (необязательно)"
          value={supplier}
          onChange={(event) => setSupplier(event.target.value)}
          style={{ marginBottom: 16, maxWidth: 360 }}
        />
        <Upload.Dragger
          multiple
          accept=".pdf,.png"
          customRequest={async (options) => {
            const file = options.file as File
            try {
              const result = await uploadDocument(file, supplier)
              setRows((previous) => [
                {
                  key: result.document.id + file.name,
                  filename: file.name,
                  reused: result.reused,
                  document: result.document,
                },
                ...previous,
              ])
              options.onSuccess?.(result)
            } catch (error) {
              options.onError?.(error as Error)
            }
          }}
        >
          <p className="ant-upload-drag-icon">
            <InboxOutlined />
          </p>
          <p className="ant-upload-text">Перетащите PDF или PNG сюда</p>
          <p className="ant-upload-hint">Сканы уходят в OCR, документы с текстовым слоем — нет</p>
        </Upload.Dragger>
      </Card>

      {rows.length > 0 && (
        <Card title="Результат">
          <Table<Row>
            dataSource={rows}
            pagination={false}
            columns={[
              { title: 'Файл', dataIndex: 'filename' },
              {
                title: 'Обработка',
                render: (_, row) =>
                  row.reused ? 'уже был распознан' : row.document.text_source === 'ocr' ? 'OCR' : 'текстовый слой',
              },
              {
                title: 'Вердикт',
                render: (_, row) =>
                  row.document.check ? (
                    <Tag color={VERDICT_COLOR[row.document.check.verdict as Verdict]}>
                      {VERDICT_LABEL[row.document.check.verdict as Verdict]}
                    </Tag>
                  ) : (
                    '—'
                  ),
              },
            ]}
          />
        </Card>
      )}
    </Space>
  )
}
