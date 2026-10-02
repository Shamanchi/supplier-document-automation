import { ReloadOutlined, SearchOutlined } from '@ant-design/icons';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { Alert, Button, Card, Empty, Input, Pagination, Select, Space, Spin, Tag, Typography } from 'antd';
import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { extractApiError, fetchPriceImportReviewQueue } from '../api/client';
import ReviewProgress from '../components/review/ReviewProgress';
import ReviewWorkspace from '../components/review/ReviewWorkspace';
import '../components/review/review.css';

const PAGE_SIZE = 50;

export default function ReviewQueuePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [status, setStatus] = useState(searchParams.get('status') ?? 'pending');
  const [search, setSearch] = useState(searchParams.get('query') ?? '');
  const [page, setPage] = useState(1);
  const selectedImport = searchParams.get('import');
  const selectedSection = searchParams.get('section');
  const query = useQuery({
    queryKey: ['price-import-review-queue', status, search, page],
    queryFn: () => fetchPriceImportReviewQueue({
      status: status || undefined,
      query: search || undefined,
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
    }),
    placeholderData: keepPreviousData,
  });

  const select = (importId: string, sectionId: string) => {
    const next = new URLSearchParams(searchParams);
    next.set('import', importId);
    next.set('section', sectionId);
    if (status) next.set('status', status); else next.delete('status');
    if (search) next.set('query', search); else next.delete('query');
    setSearchParams(next);
  };
  useEffect(() => {
    if (!selectedSection && query.data?.items[0]) {
      select(query.data.items[0].import_id, query.data.items[0].section_id);
    }
  // Selection should follow only a newly loaded queue, not every URL mutation.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query.data, selectedSection]);
  const moveNext = () => {
    const items = query.data?.items ?? [];
    const index = items.findIndex((item) => item.section_id === selectedSection && item.import_id === selectedImport);
    const next = items[index + 1] ?? items[0];
    if (next) select(next.import_id, next.section_id);
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <div>
        <Typography.Title level={3} style={{ margin: 0 }}>Очередь ручной проверки</Typography.Title>
        <Typography.Text type="secondary">Одна задача — одна source section. Mapping, строки и dry-run собраны в одном рабочем месте.</Typography.Text>
      </div>
      <ReviewProgress totals={query.data?.totals ?? {}} />
      <Space wrap>
        <Select
          value={status}
          style={{ width: 190 }}
          onChange={(value) => { setStatus(value); setPage(1); }}
          options={[
            { value: '', label: 'Все статусы' },
            { value: 'pending', label: 'Ожидают' },
            { value: 'in_progress', label: 'В работе' },
            { value: 'validated', label: 'Проверены' },
            { value: 'accepted', label: 'Приняты' },
            { value: 'rejected', label: 'Отклонены' },
          ]}
        />
        <Input
          allowClear
          prefix={<SearchOutlined />}
          value={search}
          onChange={(event) => { setSearch(event.target.value); setPage(1); }}
          placeholder="Файл, поставщик, лист или section ID"
          style={{ width: 340, maxWidth: '100%' }}
        />
        <Button icon={<ReloadOutlined />} onClick={() => void query.refetch()}>Обновить</Button>
      </Space>
      {query.isError && <Alert type="error" showIcon message="Очередь не загружена" description={extractApiError(query.error)} />}
      <div className="review-queue-layout">
        <Card size="small" title={`Секции · ${query.data?.total ?? 0}`}>
          {query.isLoading ? <Spin /> : !(query.data?.items.length) ? <Empty description="В этом фильтре нет секций" /> : (
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <Space direction="vertical" size={6} className="review-queue-list" style={{ width: '100%' }}>
                {query.data.items.map((item) => (
                  <Button
                    key={`${item.import_id}-${item.section_id}`}
                    className={`review-queue-item ${item.import_id === selectedImport && item.section_id === selectedSection ? 'is-selected' : ''}`}
                    onClick={() => select(item.import_id, item.section_id)}
                  >
                    <Space direction="vertical" size={1} style={{ width: '100%', alignItems: 'stretch' }}>
                      <Space style={{ justifyContent: 'space-between' }}>
                        <Typography.Text strong ellipsis>{item.filename}</Typography.Text>
                        <Tag>{item.status}</Tag>
                      </Space>
                      <Typography.Text>{item.sheet_name}</Typography.Text>
                      <Typography.Text type="secondary">{item.section_id} · rows {(item.start_row_index ?? 0) + 1}–{item.end_row_index ?? '…'}</Typography.Text>
                      {item.reason_code && <Typography.Text type="danger">{item.reason_code}</Typography.Text>}
                    </Space>
                  </Button>
                ))}
              </Space>
              <Pagination
                simple
                current={page}
                pageSize={PAGE_SIZE}
                total={query.data.total}
                showSizeChanger={false}
                onChange={setPage}
              />
            </Space>
          )}
        </Card>
        <Card size="small">
          {selectedImport && selectedSection
            ? <ReviewWorkspace importId={selectedImport} sectionId={selectedSection} onResolved={moveNext} />
            : <Empty description="Выберите секцию слева" />}
        </Card>
      </div>
    </Space>
  );
}
