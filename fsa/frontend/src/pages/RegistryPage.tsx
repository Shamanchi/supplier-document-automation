import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, Col, Input, Row, Statistic, Table, Tag } from 'antd'

import { fetchRegistryStats, searchRegistry, type Certificate } from '../api/client'

export default function RegistryPage() {
  const [query, setQuery] = useState('')

  const stats = useQuery({ queryKey: ['registry-stats'], queryFn: fetchRegistryStats })
  const results = useQuery({
    queryKey: ['registry-search', query],
    queryFn: () => searchRegistry(query),
    enabled: query.trim().length >= 2,
  })

  return (
    <>
      <Row gutter={16} style={{ marginBottom: 24 }}>
        <Col span={8}>
          <Card>
            <Statistic title="Карточек в реестре" value={stats.data?.certificates ?? 0} />
          </Card>
        </Col>
        <Col span={8}>
          <Card>
            <Statistic
              title="Действующих на сегодня"
              value={stats.data?.alive ?? 0}
              valueStyle={{ color: '#389e0d' }}
            />
          </Card>
        </Col>
        <Col span={8}>
          <Card>
            <Statistic title="Известных номеров" value={stats.data?.numbers ?? 0} />
          </Card>
        </Col>
      </Row>

      <Card title="Поиск по реестру">
        <Input.Search
          allowClear
          placeholder="номер сертификата, изготовитель или продукция"
          onSearch={setQuery}
          style={{ marginBottom: 16 }}
        />
        <Table<Certificate>
          rowKey="registration_number"
          loading={results.isFetching}
          dataSource={results.data ?? []}
          pagination={{ pageSize: 10 }}
          columns={[
            { title: 'Номер', dataIndex: 'registration_number', width: 260 },
            {
              title: 'Статус',
              dataIndex: 'status',
              width: 140,
              render: (value: string) => (
                <Tag color={value === 'Действует' ? 'green' : 'default'}>{value}</Tag>
              ),
            },
            { title: 'Действует до', dataIndex: 'valid_until', width: 130 },
            { title: 'Изготовитель', dataIndex: 'manufacturer_name' },
            { title: 'Продукция', dataIndex: 'product_full_name', ellipsis: true },
          ]}
        />
      </Card>
    </>
  )
}
