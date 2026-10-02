import { CheckCircleOutlined, DollarOutlined, PlayCircleOutlined, SaveOutlined } from '@ant-design/icons';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Card, Empty, Select, Skeleton, Space, Tag, Typography, message } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import {
  activatePriceImportReviewRule,
  applyPriceImportReview,
  extractApiError,
  extractApiErrorDetails,
  extractApiErrorEnvelope,
  fetchPriceImportReview,
  fetchPriceImportReviewSection,
  updatePriceImportReviewDecisions,
  updatePriceImportReviewCurrency,
  updatePriceImportReviewMapping,
  validatePriceImportReview,
} from '../../api/client';
import type { ReviewBlockedCell, ReviewDecision, ReviewMappingTable } from '../../api/types';
import { useAuth } from '../../auth/AuthContext';
import RowDecisionControl from './RowDecisionControl';
import SourceGrid from './SourceGrid';
import ValidationDiff from './ValidationDiff';
import { decisionKey, mappingForSection, mergeSectionMapping } from './reviewModel';
import './review.css';

/** Координаты заблокированных ячеек из конверта отказа (шаг обхода гарда). */
function blockedCellsFrom(error: unknown): ReviewBlockedCell[] {
  const evidence = extractApiErrorDetails(error)?.evidence;
  if (!Array.isArray(evidence)) return [];
  return evidence
    .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object')
    .map((item) => ({
      sheet: typeof item.sheet === 'string' ? item.sheet : undefined,
      row: typeof item.row === 'number' ? item.row : undefined,
      column: typeof item.column === 'number' ? item.column : undefined,
      value: typeof item.value === 'string' ? item.value : undefined,
    }));
}

export default function ReviewWorkspace({
  importId,
  sectionId,
  onResolved,
}: {
  importId: string;
  sectionId?: string;
  onResolved?: () => void;
}) {
  const { isAdmin } = useAuth();
  const queryClient = useQueryClient();
  const reviewQuery = useQuery({
    queryKey: ['price-import-review', importId],
    queryFn: () => fetchPriceImportReview(importId),
    refetchInterval: (result) => result.state.data?.status === 'applying' ? 2_000 : false,
  });
  const fallbackSectionId = useMemo(() => {
    const first = reviewQuery.data?.sections[0];
    return first && typeof first.section_id === 'string' ? first.section_id : undefined;
  }, [reviewQuery.data]);
  const activeSectionId = sectionId ?? fallbackSectionId;
  const sectionQuery = useQuery({
    queryKey: ['price-import-review-section', importId, activeSectionId],
    queryFn: () => fetchPriceImportReviewSection(importId, activeSectionId!),
    enabled: Boolean(activeSectionId),
  });
  const [mapping, setMapping] = useState<ReviewMappingTable | null>(null);
  const [dirty, setDirty] = useState(false);
  const [currencyDraft, setCurrencyDraft] = useState<string | null>(null);
  const [rejectTarget, setRejectTarget] = useState<{ row: number; panel: number | null } | null>(null);
  const [blockedCells, setBlockedCells] = useState<ReviewBlockedCell[]>([]);

  useEffect(() => {
    if (sectionQuery.data) {
      setMapping(mappingForSection(sectionQuery.data));
      setDirty(false);
    }
  }, [sectionQuery.data]);

  useEffect(() => {
    setCurrencyDraft(reviewQuery.data?.currency_override ?? null);
  }, [reviewQuery.data?.currency_override]);

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['price-import-review', importId] }),
      queryClient.invalidateQueries({ queryKey: ['price-import-review-section', importId] }),
      queryClient.invalidateQueries({ queryKey: ['price-import-review-queue'] }),
      queryClient.invalidateQueries({ queryKey: ['price-import', importId] }),
    ]);
  };
  const mutation = useMutation({
    mutationFn: (action: () => Promise<unknown>) => action(),
    onSuccess: async () => { message.success('Изменение принято'); await refresh(); },
    onError: (error) => {
      // Отказ гарда — не «ошибка запроса»: показываем, что именно скрывается,
      // и даём подтвердить обход отдельным действием (см. RowDecisionControl).
      if (extractApiErrorEnvelope(error)?.code === 'UNSAFE_NON_PRICE_DECISION') {
        setBlockedCells(blockedCellsFrom(error));
        message.warning('Решение скрывает ценовой признак: требуется подтвердить обход');
        return;
      }
      message.error(extractApiError(error));
    },
  });
  const run = (action: () => Promise<unknown>, after?: () => void) => {
    mutation.mutate(action, { onSuccess: after });
  };

  if (reviewQuery.isLoading || sectionQuery.isLoading) return <Skeleton active paragraph={{ rows: 8 }} />;
  if (reviewQuery.isError) return <Alert type="error" showIcon message="Review недоступен" description={extractApiError(reviewQuery.error)} />;
  if (!activeSectionId) return <Empty description="В import нет секций для ручной проверки" />;
  if (sectionQuery.isError || !sectionQuery.data || !reviewQuery.data || !mapping) {
    return <Alert type="error" showIcon message="Секция не загружена" description={extractApiError(sectionQuery.error)} />;
  }
  const section = sectionQuery.data;
  const review = reviewQuery.data;
  const editable = isAdmin && section.actions.can_edit === true && !mutation.isPending;
  const allDecisions = review.section_decisions;
  const updateDecisions = (next: ReviewDecision[]) => run(
    () => updatePriceImportReviewDecisions(importId, review.revision, next),
    () => { setRejectTarget(null); setBlockedCells([]); },
  );
  const acceptRow = (row: number, panel: number | null) => {
    const key = decisionKey(section.sheet_name, row, panel);
    updateDecisions(allDecisions.filter((item) => decisionKey(item.sheet, item.row_start, item.panel_index) !== key));
  };
  const rejectRow = (decision: ReviewDecision) => {
    const key = decisionKey(decision.sheet, decision.row_start, decision.panel_index);
    updateDecisions([
      ...allDecisions.filter((item) => decisionKey(item.sheet, item.row_start, item.panel_index) !== key),
      decision,
    ]);
  };

  return (
    <div className="review-workspace">
      {!isAdmin && <Alert type="info" showIcon message="Режим только для чтения" description="Редактирование и применение доступны администратору." />}
      <Space wrap style={{ justifyContent: 'space-between' }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0 }}>{section.sheet_name}</Typography.Title>
          <Typography.Text type="secondary">{section.section_id} · revision {section.revision}</Typography.Text>
        </div>
        <Space wrap>
          <Tag>{section.section_kind}</Tag>
          <Tag color={review.status === 'validated' ? 'green' : 'default'}>{review.status}</Tag>
          {dirty && <Tag color="gold">Не сохранено</Tag>}
        </Space>
      </Space>
      {(section.attempt?.error_message || section.attempt?.validation) && (
        <Alert
          type="warning"
          showIcon
          message={section.attempt.error_message ?? 'Модельный ответ требует проверки'}
          description={section.attempt.validation ? JSON.stringify(section.attempt.validation) : undefined}
        />
      )}
      <Card size="small" title="Валюта денежных значений">
        <Space wrap>
          <Select
            aria-label="Валюта прайса"
            value={currencyDraft ?? ''}
            style={{ width: 260 }}
            disabled={!editable}
            options={[
              { value: '', label: 'Не задана — определить из файла' },
              { value: 'RUB', label: 'RUB — российский рубль' },
              { value: 'USD', label: 'USD — доллар США' },
              { value: 'EUR', label: 'EUR — евро' },
              { value: 'KZT', label: 'KZT — тенге' },
            ]}
            onChange={(value) => setCurrencyDraft(value || null)}
          />
          <Button
            icon={<DollarOutlined />}
            disabled={!editable || currencyDraft === review.currency_override}
            loading={mutation.isPending}
            onClick={() => run(
              () => updatePriceImportReviewCurrency(
                importId,
                review.revision,
                currencyDraft,
              ),
            )}
          >Сохранить валюту</Button>
          <Typography.Text type="secondary">
            Используется для чисел без собственного обозначения валюты. После изменения выполните проверку и применение.
          </Typography.Text>
        </Space>
      </Card>
      <SourceGrid
        section={section}
        mapping={mapping}
        editable={editable}
        onMappingChange={(value) => { setMapping(value); setDirty(true); }}
        onAccept={acceptRow}
        onReject={(row, panel) => { setBlockedCells([]); setRejectTarget({ row, panel }); }}
      />
      <ValidationDiff validation={section.validation} />
      <Card size="small" className="review-action-bar">
        <Space wrap style={{ width: '100%', justifyContent: 'space-between' }}>
          <Typography.Text>
            Revision <Typography.Text code>{review.revision}</Typography.Text> · сервер определяет доступность каждого шага
          </Typography.Text>
          <Space wrap>
            <Button
              icon={<SaveOutlined />}
              disabled={!editable || !dirty}
              loading={mutation.isPending}
              onClick={() => run(
                () => updatePriceImportReviewMapping(
                  importId,
                  review.revision,
                  mergeSectionMapping(section.mapping, mapping),
                ),
                () => setDirty(false),
              )}
            >Сохранить mapping</Button>
            <Button
              icon={<CheckCircleOutlined />}
              disabled={!isAdmin || section.actions.can_validate !== true || dirty || mutation.isPending}
              onClick={() => run(() => validatePriceImportReview(importId, review.revision))}
            >Проверить</Button>
            <Button
              type="primary"
              icon={<PlayCircleOutlined />}
              disabled={!isAdmin || section.actions.can_apply !== true || dirty || mutation.isPending}
              onClick={() => run(() => applyPriceImportReview(importId, review.revision), onResolved)}
            >Применить</Button>
            <Button
              disabled={!isAdmin || section.actions.can_activate !== true || mutation.isPending}
              onClick={() => run(() => activatePriceImportReviewRule(importId, review.revision))}
            >Активировать правило</Button>
          </Space>
        </Space>
      </Card>
      <RowDecisionControl
        open={rejectTarget !== null}
        sheet={section.sheet_name}
        excelRow={rejectTarget?.row ?? null}
        panel={rejectTarget?.panel ?? null}
        pending={mutation.isPending}
        blocked={blockedCells}
        onCancel={() => { setRejectTarget(null); setBlockedCells([]); }}
        onConfirm={rejectRow}
      />
    </div>
  );
}
