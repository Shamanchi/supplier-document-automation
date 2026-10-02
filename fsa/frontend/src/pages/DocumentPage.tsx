import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Descriptions, Space, Tag, Typography } from 'antd'
import { useParams } from 'react-router-dom'

import { fetchDocument, recheckDocument, type Verdict } from '../api/client'
import { VERDICT_COLOR, VERDICT_LABEL, isAttachable } from '../api/verdict'

export default function DocumentPage() {
  const { id = '' } = useParams()
  const client = useQueryClient()
  const document = useQuery({ queryKey: ['document', id], queryFn: () => fetchDocument(id) })
  const recheck = useMutation({
    mutationFn: () => recheckDocument(id),
    onSuccess: () => client.invalidateQueries({ queryKey: ['document', id] }),
  })

  if (!document.data) return <Card loading />
  const item = document.data
  const check = item.check

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      {check && !isAttachable(check.verdict as Verdict) && (
        <Alert
          type={check.verdict === 'unknown' || check.verdict === 'not_found' ? 'warning' : 'error'}
          showIcon
          message={`Прикладывать к КП нельзя: ${VERDICT_LABEL[check.verdict as Verdict]}`}
          description={
            check.review_reasons.length ? (
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {check.review_reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            ) : undefined
          }
        />
      )}

      <Card
        title={item.filename}
        extra={
          <Button loading={recheck.isPending} onClick={() => recheck.mutate()}>
            Пересверить с реестром
          </Button>
        }
      >
        <Descriptions column={2} bordered size="small">
          <Descriptions.Item label="Поставщик">{item.supplier || '—'}</Descriptions.Item>
          <Descriptions.Item label="Тип документа">{item.doc_class}</Descriptions.Item>
          <Descriptions.Item label="Источник текста">
            {item.text_source === 'ocr' ? `OCR (${item.engine})` : 'текстовый слой'}
          </Descriptions.Item>
          <Descriptions.Item label="Страниц">{item.page_count}</Descriptions.Item>
          <Descriptions.Item label="Номер сертификата" span={2}>
            {item.registration_number || '—'}
          </Descriptions.Item>
          <Descriptions.Item label="Вердикт">
            {check ? (
              <Tag color={VERDICT_COLOR[check.verdict as Verdict]}>
                {VERDICT_LABEL[check.verdict as Verdict]}
              </Tag>
            ) : (
              '—'
            )}
          </Descriptions.Item>
          <Descriptions.Item label="Действует до">{check?.valid_until ?? '—'}</Descriptions.Item>
          {item.download_url && (
            <Descriptions.Item label="Файл" span={2}>
              <a href={item.download_url} target="_blank" rel="noreferrer">
                скачать оригинал
              </a>
            </Descriptions.Item>
          )}
        </Descriptions>
      </Card>

      <Card title="Распознанный текст">
        <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', maxHeight: 320, overflow: 'auto' }}>
          {item.ocr_text_preview || '—'}
        </Typography.Paragraph>
      </Card>
    </Space>
  )
}
