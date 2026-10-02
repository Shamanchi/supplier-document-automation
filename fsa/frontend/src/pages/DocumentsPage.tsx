import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, Col, Row, Select, Space, Statistic, Table, Tag, Typography } from 'antd'
import { Link } from 'react-router-dom'

import { fetchDocuments, fetchSummary, type DocumentItem, type Verdict } from '../api/client'
import { VERDICT_COLOR, VERDICT_LABEL } from '../api/verdict'

const VERDICT_OPTIONS = Object.entries(VERDICT_LABEL).map(([value, label]) => ({
  value,
  label,
}))

export default function DocumentsPage() {
  const [verdict, setVerdict] = useState<string | undefined>()

  const summary = useQuery({ queryKey: ['summary'], queryFn: fetchSummary })
  const documents = useQuery({
    queryKey: ['documents', verdict],
    queryFn: () => fetchDocuments({ verdict, limit: 100 }),
  })

  const byVerdict = summary.data?.by_verdict ?? {}
  const attachable = byVerdict.valid ?? 0
  const total = Object.values(byVerdict).reduce((sum, value) => sum + value, 0)

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Row gutter={16}>
        <Col span={6}>
          <Card>
            <Statistic title="Документов проверено" value={total} />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="Можно приложить к КП"
              value={attachable}
              valueStyle={{ color: '#389e0d' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="Недействительны"
              value={(byVerdict.expired ?? 0) + (byVerdict.terminated ?? 0)}
              valueStyle={{ color: '#cf1322' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="Требуют ручной проверки"
              value={(byVerdict.unknown ?? 0) + (byVerdict.not_found ?? 0)}
            />
          </Card>
        </Col>
      </Row>

      <Card
        title="Документы"
        extra={
          <Select
            allowClear
            placeholder="вердикт"
            style={{ width: 200 }}
            options={VERDICT_OPTIONS}
            value={verdict}
            onChange={(value) => setVerdict(value)}
          />
        }
      >
        <Table<DocumentItem>
          rowKey="id"
          loading={documents.isLoading}
          dataSource={documents.data?.items ?? []}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          columns={[
            {
              title: 'Файл',
              dataIndex: 'filename',
              render: (value: string, row) => <Link to={`/documents/${row.id}`}>{value}</Link>,
            },
            { title: 'Поставщик', dataIndex: 'supplier', width: 160 },
            {
              title: 'Тип',
              dataIndex: 'doc_class',
              width: 130,
            },
            {
              title: 'Источник текста',
              dataIndex: 'text_source',
              width: 140,
              render: (value: string, row) =>
                value === 'ocr' ? `OCR (${row.engine || '—'})` : 'текстовый слой',
            },
            { title: 'Номер сертификата', dataIndex: 'registration_number', width: 240 },
            {
              title: 'Вердикт',
              width: 170,
              render: (_, row) =>
                row.check ? (
                  <Tag color={VERDICT_COLOR[row.check.verdict as Verdict]}>
                    {VERDICT_LABEL[row.check.verdict as Verdict]}
                  </Tag>
                ) : (
                  <Typography.Text type="secondary">не проверялся</Typography.Text>
                ),
            },
            {
              title: 'Действует до',
              width: 130,
              render: (_, row) => row.check?.valid_until ?? '—',
            },
          ]}
        />
      </Card>
    </Space>
  )
}
