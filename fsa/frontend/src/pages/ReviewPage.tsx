import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  Col,
  Input,
  Modal,
  Row,
  Segmented,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
  message,
} from 'antd'
import { Link } from 'react-router-dom'

import {
  decideDocument,
  fetchReviewQueue,
  fetchReviewSummary,
  type DecisionAction,
  type ReviewQueueItem,
} from '../api/client'
import { VERDICT_COLOR, VERDICT_LABEL } from '../api/verdict'

const ACTION_LABEL: Record<DecisionAction, string> = {
  confirm: 'Подтверждён',
  reject: 'Отклонён',
  correct: 'Номер исправлен',
}

// Имя проверяющего хранится локально: учётных записей в контуре нет, а
// подпись под решением нужна — без неё история решений бесполезна.
const REVIEWER_KEY = 'fsa-reviewer'

export default function ReviewPage() {
  const client = useQueryClient()
  const [state, setState] = useState<'pending' | 'decided' | 'all'>('pending')
  const [reviewer, setReviewer] = useState(localStorage.getItem(REVIEWER_KEY) ?? '')
  const [correcting, setCorrecting] = useState<ReviewQueueItem | null>(null)
  const [correctedNumber, setCorrectedNumber] = useState('')
  const [comment, setComment] = useState('')

  const summary = useQuery({ queryKey: ['review-summary'], queryFn: fetchReviewSummary })
  const queue = useQuery({
    queryKey: ['review-queue', state],
    queryFn: () => fetchReviewQueue(state),
  })

  const decide = useMutation({
    mutationFn: decideDocument,
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ['review-queue'] })
      client.invalidateQueries({ queryKey: ['review-summary'] })
      setCorrecting(null)
      setCorrectedNumber('')
      setComment('')
      message.success('Решение записано')
    },
    onError: () => message.error('Не удалось записать решение'),
  })

  function submit(item: ReviewQueueItem, action: DecisionAction, number = '') {
    if (!reviewer.trim()) {
      message.warning('Укажите, кто принимает решение')
      return
    }
    localStorage.setItem(REVIEWER_KEY, reviewer.trim())
    decide.mutate({
      documentId: item.document_id,
      action,
      reviewer: reviewer.trim(),
      comment,
      number,
    })
  }

  const counters = summary.data

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Row gutter={16}>
        <Col span={6}>
          <Card>
            <Statistic title="Ждут подтверждения" value={counters?.pending ?? 0} />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="Подтверждено"
              value={(counters?.confirmed ?? 0) + (counters?.corrected ?? 0)}
              valueStyle={{ color: '#389e0d' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic
              title="Отклонено"
              value={counters?.rejected ?? 0}
              valueStyle={{ color: '#cf1322' }}
            />
          </Card>
        </Col>
        <Col span={6}>
          <Card>
            <Statistic title="Подтверждение устарело" value={counters?.stale ?? 0} />
          </Card>
        </Col>
      </Row>

      <Alert
        type="info"
        showIcon
        message="Подтверждение относится к состоянию документа"
        description="Если вердикт изменился, номер перечитан или документ распознан заново, подтверждение устаревает и документ снова попадает в очередь. Решения не перезаписываются — новое кладётся рядом, история остаётся."
      />

      <Card
        title="Очередь на подтверждение"
        extra={
          <Space>
            <Input
              placeholder="кто проверяет"
              value={reviewer}
              onChange={(event) => setReviewer(event.target.value)}
              style={{ width: 180 }}
            />
            <Segmented
              value={state}
              onChange={(value) => setState(value as typeof state)}
              options={[
                { label: 'Ждут', value: 'pending' },
                { label: 'Решённые', value: 'decided' },
                { label: 'Все', value: 'all' },
              ]}
            />
          </Space>
        }
      >
        <Table<ReviewQueueItem>
          rowKey="document_id"
          loading={queue.isLoading}
          dataSource={queue.data?.items ?? []}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          columns={[
            {
              title: 'Документ',
              dataIndex: 'filename',
              render: (value: string, item) => (
                <Link to={`/documents/${item.document_id}`}>{value || item.document_id}</Link>
              ),
            },
            { title: 'Поставщик', dataIndex: 'supplier' },
            {
              title: 'Вердикт',
              dataIndex: 'verdict',
              render: (value: ReviewQueueItem['verdict']) => (
                <Tag color={VERDICT_COLOR[value]}>{VERDICT_LABEL[value] ?? value}</Tag>
              ),
            },
            {
              title: 'Решение',
              render: (_, item) => {
                if (!item.decided) return <Typography.Text type="secondary">нет</Typography.Text>
                return (
                  <Space direction="vertical" size={0}>
                    <span>{ACTION_LABEL[item.action as DecisionAction] ?? item.action}</span>
                    <Typography.Text type="secondary">{item.reviewer}</Typography.Text>
                    {item.stale && <Tag color="orange">устарело: {item.stale_reason}</Tag>}
                  </Space>
                )
              },
            },
            {
              title: 'Действия',
              render: (_, item) => (
                <Space>
                  <Button
                    type="primary"
                    size="small"
                    onClick={() => submit(item, 'confirm')}
                    disabled={decide.isPending}
                  >
                    Подтвердить
                  </Button>
                  <Button size="small" onClick={() => submit(item, 'reject')} disabled={decide.isPending}>
                    Отклонить
                  </Button>
                  <Button
                    size="small"
                    onClick={() => {
                      setCorrecting(item)
                      setCorrectedNumber('')
                    }}
                  >
                    Исправить номер
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      </Card>

      <Modal
        open={correcting !== null}
        title="Исправить номер сертификата"
        okText="Сохранить"
        cancelText="Отмена"
        confirmLoading={decide.isPending}
        onCancel={() => setCorrecting(null)}
        onOk={() => correcting && submit(correcting, 'correct', correctedNumber)}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text type="secondary">
            Номер проверяется по реестру так же, как прочитанный движком: если он не найден,
            документ останется без подтверждения.
          </Typography.Text>
          <Input
            placeholder="ЕАЭС RU С-RU.ПБ97.В.00272/21"
            value={correctedNumber}
            onChange={(event) => setCorrectedNumber(event.target.value)}
          />
          <Input.TextArea
            placeholder="комментарий"
            value={comment}
            onChange={(event) => setComment(event.target.value)}
            rows={2}
          />
        </Space>
      </Modal>
    </Space>
  )
}
