import { useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Col,
  Input,
  InputNumber,
  Modal,
  Row,
  Segmented,
  Space,
  Statistic,
  Table,
  Tag,
  Tooltip,
  Typography,
  Upload,
  message,
} from 'antd';

import {
  createCalculation,
  downloadCalculationExcel,
  extractApiError,
  fetchCanonicalStats,
  fetchCatalogStats,
  fetchIndexStats,
  fetchPhysicalReferenceStats,
  overrideCalculationPrice,
  rebuildCanonical,
  rememberSynonym,
  runPlayground,
  uploadPhysicalReference,
  type CalculationLineInput,
  type CalculationResult,
  type MatchCandidate,
  type PlaygroundLine,
  type PlaygroundResult,
  type ProductVariant,
  type VariantOffer,
} from '../api/client';

const SAMPLE = `Огнетушитель ОП-4 закачной — 25 шт
Рукав пожарный РПК-50 — 10 м
Шкаф пожарный ШПК-320 — 2 шт
Ороситель спринклерный СВН-15 — 40 шт`;

const DECISION: Record<PlaygroundLine['decision'], { label: string; color: string }> = {
  auto: { label: 'принято автоматически', color: 'green' },
  review: { label: 'нужен выбор менеджера', color: 'orange' },
  // «Уточнить» и «не найдено» — разные исходы: в первом случае похожее в
  // каталоге есть, но запрос без модели; во втором товара нет вовсе.
  clarify: { label: 'уточнить у клиента', color: 'gold' },
  not_found: { label: 'не найдено', color: 'red' },
};

const STAGE: Record<string, string> = {
  synonym: 'словарь синонимов',
  article: 'артикул',
  designation: 'обозначение изделия',
  vector: 'вектор (pgvector)',
  fuzzy: 'fuzzy',
  none: '—',
};

const VERDICT: Record<string, { label: string; color: string }> = {
  valid: { label: 'действует', color: 'green' },
  expired: { label: 'срок истёк', color: 'red' },
  terminated: { label: 'прекращён', color: 'red' },
  suspended: { label: 'приостановлен', color: 'orange' },
  not_found: { label: 'нет в реестре', color: 'default' },
  unknown: { label: 'не проверено', color: 'default' },
};

function CertificateCell({ line }: { line: PlaygroundLine }) {
  const info = line.certificate;
  if (!info) return <Typography.Text type="secondary">—</Typography.Text>;
  if (!info.available) {
    return (
      <Tooltip title={info.reason_unavailable}>
        <Tag>не проверено</Tag>
      </Tooltip>
    );
  }
  const verdict = VERDICT[info.verdict] ?? { label: info.verdict, color: 'default' };
  return (
    <Space direction="vertical" size={0}>
      <Space size={4}>
        <Tag color={verdict.color}>{verdict.label}</Tag>
        {info.attachable ? (
          <Tag color="green">можно приложить</Tag>
        ) : (
          <Tag color="orange">нельзя приложить</Tag>
        )}
      </Space>
      {info.registration_number && (
        <Typography.Text code>{info.registration_number}</Typography.Text>
      )}
      {info.valid_until && (
        <Typography.Text type="secondary">до {info.valid_until}</Typography.Text>
      )}
      {info.confirmed_by && (
        <Typography.Text type="secondary">подтвердил: {info.confirmed_by}</Typography.Text>
      )}
      {info.review_reasons.slice(0, 2).map((reason) => (
        <Typography.Text key={reason} type="warning" style={{ fontSize: 12 }}>
          {reason}
        </Typography.Text>
      ))}
    </Space>
  );
}

const DISCOUNT_SOURCE: Record<string, string> = {
  none: 'без скидки',
  supplier: 'по поставщику',
  category: 'по категории',
  article: 'по артикулу',
  volume: 'объёмная',
};

/** Строка расчёта, ещё не отправленная на сервер. */
interface BasketRow {
  price_item_id: string;
  name: string;
  supplier: string;
  quantity: number;
  source_line: string;
  canonical_id: string;
}

function money(value: number | null | undefined) {
  return value === null || value === undefined ? '—' : value.toLocaleString('ru-RU');
}

function VariantsTable({
  line,
  onAdd,
}: {
  line: PlaygroundLine;
  onAdd: (line: PlaygroundLine, variant: ProductVariant, offer: VariantOffer) => void;
}) {
  const variants = line.variants ?? [];
  if (variants.length === 0) {
    return (
      <Typography.Text type="secondary">
        Обозначение в строке не распознано — весь ряд собрать не по чему.
      </Typography.Text>
    );
  }
  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Typography.Text type="secondary">
        Весь ряд по обозначению {(line.designations ?? []).join(', ')} — {variants.length} шт.
      </Typography.Text>
      <Table<ProductVariant>
        size="small"
        rowKey="canonical_id"
        dataSource={variants}
        pagination={variants.length > 10 ? { pageSize: 10 } : false}
        columns={[
          {
            title: 'Позиция',
            dataIndex: 'title',
            render: (value: string, variant) => (
              <Space size={6} wrap>
                <span>{value}</span>
                {variant.relevance >= 1 && <Tag color="green">точно по запросу</Tag>}
                {variant.merge_status === 'disputed' && <Tag color="red">спорное слияние</Tag>}
              </Space>
            ),
          },
          {
            title: 'Предложения поставщиков',
            render: (_, variant) =>
              variant.offers.length === 0 ? (
                <Typography.Text type="secondary">нет активных прайсов</Typography.Text>
              ) : (
                <Space direction="vertical" size={2}>
                  {variant.offers.map((offer) => (
                    <Space key={offer.price_item_id} size={8}>
                      <span>
                        {offer.supplier}: {money(offer.price)} {offer.currency ?? ''}{' '}
                        {offer.unit ? `/ ${offer.unit}` : ''}
                      </span>
                      <Button size="small" onClick={() => onAdd(line, variant, offer)}>
                        В расчёт
                      </Button>
                    </Space>
                  ))}
                </Space>
              ),
          },
        ]}
      />
    </Space>
  );
}

export default function PlaygroundPage() {
  const [text, setText] = useState(SAMPLE);
  const [supplier, setSupplier] = useState('');
  // По умолчанию ищем во всех проектах. Менеджеру нужна позиция из любого
  // прайса компании, а разделение по проектам — это способ хранения загрузок,
  // а не граница ассортимента: на боевом каталоге 223 тыс. позиций из 227 тыс.
  // оказались вне текущего проекта, и подбор молча отвечал «не найдено».
  const [scope, setScope] = useState<'current' | 'all'>('all');
  const [result, setResult] = useState<PlaygroundResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [stats, setStats] = useState<{ canonicals: number; disputed: number } | null>(null);
  const [index, setIndex] = useState<{ vectors: number; versions: number } | null>(null);
  const [catalog, setCatalog] = useState<{ total: number; searchable: number; positions: number } | null>(null);
  const [physical, setPhysical] = useState<{ rows: number; withWeight: number; withVolume: number } | null>(null);
  const [basket, setBasket] = useState<BasketRow[]>([]);
  const [calculation, setCalculation] = useState<CalculationResult | null>(null);
  // Правка цены всегда идёт через диалог: причина обязательна, и без неё
  // сервер правку не примет.
  const [editing, setEditing] = useState<{ id: string; name: string } | null>(null);
  const [editPrice, setEditPrice] = useState<number | null>(null);
  const [editReason, setEditReason] = useState('');

  async function run() {
    setLoading(true);
    try {
      const [playground, canonical, indexStats, catalogStats, physicalStats] = await Promise.all([
        runPlayground(text, true, supplier, scope === 'all' ? 'all' : 'current'),
        fetchCanonicalStats().catch(() => null),
        // Статистика индекса необязательна: подбор работает и на неполном
        // индексе, а её отсутствие не должно ронять экран.
        fetchIndexStats().catch(() => null),
        fetchCatalogStats().catch(() => null),
        fetchPhysicalReferenceStats().catch(() => null),
      ]);
      setResult(playground);
      if (canonical) setStats({ canonicals: canonical.canonicals, disputed: canonical.disputed });
      if (indexStats) setIndex({ vectors: indexStats.vectors, versions: indexStats.versions });
      if (catalogStats) setCatalog({ total: catalogStats.suppliers_total, searchable: catalogStats.suppliers_searchable, positions: catalogStats.positions_searchable });
      if (physicalStats) setPhysical({ rows: physicalStats.rows, withWeight: physicalStats.with_weight, withVolume: physicalStats.with_volume });
    } catch (error) {
      message.error(extractApiError(error));
    } finally {
      setLoading(false);
    }
  }

  async function uploadReference(file: File) {
    try {
      const loaded = await uploadPhysicalReference(file);
      const next = await fetchPhysicalReferenceStats();
      setPhysical({ rows: next.rows, withWeight: next.with_weight, withVolume: next.with_volume });
      message.success(`Справочник загружен: ${loaded.imported}, пропущено: ${loaded.skipped}`);
    } catch (error) {
      message.error(extractApiError(error));
    }
  }

  async function rebuild() {
    setLoading(true);
    try {
      const built = await rebuildCanonical();
      message.success(
        `Канон пересобран: ${built.canonicals} товаров из ${built.items} позиций, спорных ${built.disputed}`,
      );
      setStats({ canonicals: built.canonicals, disputed: built.disputed });
    } catch (error) {
      message.error(extractApiError(error));
    } finally {
      setLoading(false);
    }
  }

  function addToBasket(line: PlaygroundLine, variant: ProductVariant, offer: VariantOffer) {
    setBasket((rows) => {
      if (rows.some((row) => row.price_item_id === offer.price_item_id)) {
        message.info('Позиция уже в расчёте');
        return rows;
      }
      return [
        ...rows,
        {
          price_item_id: offer.price_item_id,
          name: offer.name || variant.title,
          supplier: offer.supplier,
          // Количество из заявки — то, что менеджер уже разобрал в строке.
          quantity: line.quantity ?? 1,
          source_line: line.source_line,
          canonical_id: variant.canonical_id,
        },
      ];
    });
  }

  async function buildCalculation() {
    if (basket.length === 0) return;
    setLoading(true);
    try {
      const lines: CalculationLineInput[] = basket.map((row) => ({
        price_item_id: row.price_item_id,
        quantity: row.quantity,
        source_line: row.source_line,
        canonical_id: row.canonical_id,
      }));
      setCalculation(await createCalculation(lines, 'Расчёт из песочницы'));
    } catch (error) {
      message.error(extractApiError(error));
    } finally {
      setLoading(false);
    }
  }

  async function saveOverride() {
    if (!editing) return;
    if (!editReason.trim()) {
      message.warning('Без причины корректировка не сохраняется');
      return;
    }
    try {
      const updated = await overrideCalculationPrice(editing.id, editPrice, editReason.trim());
      setCalculation((current) =>
        current
          ? {
              ...current,
              items: current.items.map((row) => (row.id === updated.id ? updated : row)),
            }
          : current,
      );
      setEditing(null);
      setEditReason('');
      message.success('Цена скорректирована, правка записана в журнал');
    } catch (error) {
      message.error(extractApiError(error));
    }
  }

  async function copyCalculation() {
    if (!calculation) return;
    const safeCell = (value: unknown) => {
      const textValue = String(value ?? '');
      return /^[=+\-@]/.test(textValue) ? `'${textValue}` : textValue;
    };
    const header = ['Наименование', 'Поставщик', 'Количество', 'Ед.', 'Цена', 'Вес, кг', 'Объём, м³'];
    const rows = calculation.items.map((row) => [
      row.name,
      row.supplier_name,
      row.quantity,
      row.unit_code ?? '',
      row.final_price ?? '',
      row.weight_kg ?? '',
      row.volume_m3 ?? '',
    ].map(safeCell));
    try {
      await navigator.clipboard.writeText([header, ...rows].map((row) => row.join('\t')).join('\n'));
      message.success('Расчёт скопирован в буфер');
    } catch {
      message.error('Браузер не разрешил доступ к буферу обмена');
    }
  }

  async function remember(line: PlaygroundLine, candidate: MatchCandidate) {
    try {
      await rememberSynonym(line.query_name, candidate.canonical_id);
      message.success('Выбор запомнен — в следующий раз подбор найдёт сразу');
    } catch (error) {
      message.error(extractApiError(error));
    }
  }

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Alert
        type="info"
        showIcon
        message="Песочница подбора"
        description="Тот же путь, которым пойдёт расчёт: строка заявки → разбор количества и единицы → каскад подбора (синонимы → артикул → вектор → fuzzy) → документация из контура сертификатов. Ничего не сохраняется в расчёт, только показывается."
      />

      <Row gutter={16}>
        <Col span={16}>
          <Card title="Заявка">
            <Input.TextArea
              rows={6}
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder="Одна позиция в строке: «Огнетушитель ОП-4 — 25 шт»"
            />
            <Space style={{ marginTop: 12 }}>
              <Input
                placeholder="поставщик (необязательно)"
                value={supplier}
                onChange={(event) => setSupplier(event.target.value)}
                style={{ width: 220 }}
              />
              <Segmented
                value={scope}
                onChange={(value) => setScope(value as 'current' | 'all')}
                options={[
                  { label: 'Текущий проект', value: 'current' },
                  { label: 'Все проекты', value: 'all' },
                ]}
              />
              <Button type="primary" loading={loading} onClick={() => void run()}>
                Подобрать
              </Button>
              <Button loading={loading} onClick={() => void rebuild()}>
                Пересобрать канон
              </Button>
            </Space>
          </Card>
        </Col>
        <Col span={8}>
          <Card title="Канонический слой">
            <Space size="large">
              <Statistic title="Товаров" value={stats?.canonicals ?? 0} />
              <Statistic
                title="Спорных"
                value={stats?.disputed ?? 0}
                valueStyle={{ color: (stats?.disputed ?? 0) > 0 ? '#cf1322' : undefined }}
              />
            </Space>
            {index && (
              <Typography.Text type="secondary" style={{ display: 'block', marginTop: 8 }}>
                индекс подбора: {index.vectors} векторов из {index.versions} версий прайсов
              </Typography.Text>
            )}
            {catalog && (
              <Typography.Text type="secondary" style={{ display: 'block', marginTop: 4 }}>
                поставщики: {catalog.searchable} в поиске из {catalog.total}; позиций {catalog.positions}
              </Typography.Text>
            )}
            <Space style={{ marginTop: 10 }} wrap>
              <Typography.Text type="secondary">
                справочник ППО: {physical?.rows ?? 0} (вес {physical?.withWeight ?? 0}, объём {physical?.withVolume ?? 0})
              </Typography.Text>
              <Upload
                accept=".csv,.xlsx"
                showUploadList={false}
                customRequest={({ file, onSuccess, onError }) => {
                  void uploadReference(file as File)
                    .then(() => onSuccess?.({}))
                    .catch((error) => onError?.(error as Error));
                }}
              >
                <Button size="small">Загрузить ППО</Button>
              </Upload>
            </Space>
            {result && !result.certificates_configured && (
              <Alert
                style={{ marginTop: 12 }}
                type="warning"
                showIcon
                message="Контур сертификатов не настроен"
                description="Подбор работает, документация показывается как «не проверено». Адрес сервиса задаётся переменной FSA_BASE_URL."
              />
            )}
          </Card>
        </Col>
      </Row>

      {result && (
        <Card
          title="Результат подбора"
          extra={
            <Typography.Text type="secondary">
              пороги: автоприём ≥ {result.thresholds.auto}, проверка ≥ {result.thresholds.review}
            </Typography.Text>
          }
        >
          <Table<PlaygroundLine>
            rowKey="ordinal"
            dataSource={result.lines}
            pagination={false}
            expandable={{
              // Ряд исполнений раскрывается по строке заявки: по «ОП-4
              // закачной» менеджеру нужны все ОП-4, а не тройка лучших.
              expandedRowRender: (line) => <VariantsTable line={line} onAdd={addToBasket} />,
              rowExpandable: (line) => (line.variants ?? []).length > 0,
            }}
            columns={[
              {
                title: 'Строка заявки',
                dataIndex: 'source_line',
                render: (value: string, line) => (
                  <Space direction="vertical" size={0}>
                    <span>{value}</span>
                    <Typography.Text type="secondary">
                      {line.query_name}
                      {line.quantity !== null ? ` · ${line.quantity} ${line.unit_code ?? ''}` : ''}
                    </Typography.Text>
                    {line.warnings.map((warning) => (
                      <Typography.Text key={warning} type="warning" style={{ fontSize: 12 }}>
                        {warning}
                      </Typography.Text>
                    ))}
                  </Space>
                ),
              },
              {
                title: 'Решение',
                dataIndex: 'decision',
                width: 200,
                render: (value: PlaygroundLine['decision'], line) => (
                  <Space direction="vertical" size={0}>
                    <Tag color={DECISION[value].color}>{DECISION[value].label}</Tag>
                    <Typography.Text type="secondary">{STAGE[line.stage] ?? line.stage}</Typography.Text>
                  </Space>
                ),
              },
              {
                title: 'Кандидаты',
                render: (_, line) =>
                  line.candidates.length === 0 ? (
                    <Typography.Text type="secondary">нет</Typography.Text>
                  ) : (
                    <Space direction="vertical" size={4} style={{ width: '100%' }}>
                      {line.candidates.map((candidate) => (
                        <Space key={candidate.canonical_id} size={8} wrap>
                          <Typography.Text>{candidate.title}</Typography.Text>
                          <Tag>{candidate.score.toFixed(2)}</Tag>
                          {candidate.article && <Tag color="blue">{candidate.article}</Tag>}
                          {candidate.merge_status === 'disputed' && (
                            <Tag color="red">спорное слияние</Tag>
                          )}
                          <Button size="small" onClick={() => void remember(line, candidate)}>
                            Это оно
                          </Button>
                        </Space>
                      ))}
                    </Space>
                  ),
              },
              {
                title: 'Документация',
                width: 260,
                render: (_, line) => <CertificateCell line={line} />,
              },
            ]}
          />
        </Card>
      )}

      {basket.length > 0 && (
        <Card
          title={`Отобрано в расчёт — ${basket.length}`}
          extra={
            <Space>
              <Button onClick={() => setBasket([])}>Очистить</Button>
              <Button type="primary" loading={loading} onClick={() => void buildCalculation()}>
                Посчитать со скидками
              </Button>
            </Space>
          }
        >
          <Table<BasketRow>
            size="small"
            rowKey="price_item_id"
            dataSource={basket}
            pagination={false}
            columns={[
              { title: 'Позиция', dataIndex: 'name' },
              { title: 'Поставщик', dataIndex: 'supplier', width: 200 },
              {
                title: 'Количество',
                dataIndex: 'quantity',
                width: 140,
                render: (value: number, row) => (
                  <InputNumber
                    min={0.001}
                    value={value}
                    onChange={(next) =>
                      setBasket((rows) =>
                        rows.map((item) =>
                          item.price_item_id === row.price_item_id
                            ? { ...item, quantity: Number(next) || 1 }
                            : item,
                        ),
                      )
                    }
                  />
                ),
              },
              {
                title: '',
                width: 90,
                render: (_, row) => (
                  <Button
                    size="small"
                    danger
                    onClick={() =>
                      setBasket((rows) =>
                        rows.filter((item) => item.price_item_id !== row.price_item_id),
                      )
                    }
                  >
                    Убрать
                  </Button>
                ),
              },
            ]}
          />
        </Card>
      )}

      {calculation && (
        <Card
          title="Расчёт"
          extra={
            <Space size="large">
              <Button onClick={() => void downloadCalculationExcel(calculation.id)}>
                Скачать Excel
              </Button>
              <Button onClick={() => void copyCalculation()}>Копировать</Button>
            {calculation.totals && (
              <Space size="large">
                <Statistic
                  title="До скидок"
                  value={calculation.totals.base}
                  precision={2}
                  valueStyle={{ fontSize: 16 }}
                />
                <Statistic
                  title="После скидок"
                  value={calculation.totals.discounted}
                  precision={2}
                  valueStyle={{ fontSize: 16 }}
                />
                <Statistic
                  title="К оплате"
                  value={calculation.totals.final}
                  precision={2}
                  valueStyle={{ fontSize: 16, color: '#3f8600' }}
                />
                <Statistic title="Вес, кг" value={calculation.totals.weight_kg} precision={3} valueStyle={{ fontSize: 16 }} />
                <Statistic title="Объём, м³" value={calculation.totals.volume_m3} precision={4} valueStyle={{ fontSize: 16 }} />
              </Space>
            )
            }</Space>
          }
        >
          <Table
            size="small"
            rowKey="id"
            dataSource={calculation.items}
            pagination={false}
            columns={[
              { title: 'Позиция', dataIndex: 'name' },
              {
                title: 'Кол-во',
                dataIndex: 'quantity',
                width: 90,
                render: (value: number, row) => `${value} ${row.unit_code ?? ''}`,
              },
              {
                title: 'Вес / объём',
                width: 160,
                render: (_, row) => (
                  <Space direction="vertical" size={0}>
                    <span>{row.weight_kg === null ? '—' : `${row.weight_kg} кг`}</span>
                    <span>{row.volume_m3 === null ? '—' : `${row.volume_m3} м³`}</span>
                  </Space>
                ),
              },
              {
                title: 'Цена поставщика',
                dataIndex: 'base_price',
                width: 140,
                render: (value: number | null) => money(value),
              },
              {
                title: 'Скидка',
                width: 190,
                render: (_, row) => (
                  <Space direction="vertical" size={0}>
                    <Tag color={row.discount_percent > 0 ? 'green' : 'default'}>
                      {row.discount_percent}%
                    </Tag>
                    <Typography.Text type="secondary">
                      {DISCOUNT_SOURCE[row.discount_source] ?? row.discount_source}
                    </Typography.Text>
                  </Space>
                ),
              },
              {
                title: 'После скидки',
                dataIndex: 'price_after_discount',
                width: 130,
                render: (value: number | null) => money(value),
              },
              {
                title: 'Цена в КП',
                width: 220,
                render: (_, row) => (
                  <Space direction="vertical" size={0}>
                    <Space size={8}>
                      <Typography.Text strong>{money(row.final_price)}</Typography.Text>
                      {row.manual_price !== null && <Tag color="orange">правка</Tag>}
                      <Button
                        size="small"
                        onClick={() => {
                          setEditing({ id: row.id, name: row.name });
                          setEditPrice(row.final_price);
                          setEditReason('');
                        }}
                      >
                        Изменить
                      </Button>
                    </Space>
                    {row.manual_reason && (
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        {row.manual_reason}
                        {row.manual_by ? ` · ${row.manual_by}` : ''}
                      </Typography.Text>
                    )}
                  </Space>
                ),
              },
              {
                title: 'Паспорт/сертификат',
                width: 320,
                render: (_, row) => {
                  if (!row.certificate_number && !row.certificate_reference) {
                    return <Typography.Text type="secondary">—</Typography.Text>;
                  }
                  const isHttp =
                    typeof row.certificate_reference === 'string' &&
                    /^https?:\/\//.test(row.certificate_reference);
                  return (
                    <Space direction="vertical" size={0}>
                      {row.certificate_number && (
                        <Typography.Text>{row.certificate_number}</Typography.Text>
                      )}
                      {row.certificate_reference && (
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          {isHttp ? (
                            <Typography.Link href={row.certificate_reference} target="_blank">
                              {row.certificate_reference}
                            </Typography.Link>
                          ) : (
                            row.certificate_reference
                          )}
                        </Typography.Text>
                      )}
                      {row.certificate_confirmed_by && (
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          подтвердил: {row.certificate_confirmed_by}
                        </Typography.Text>
                      )}
                    </Space>
                  );
                },
              },
            ]}
          />
        </Card>
      )}

      <Modal
        open={editing !== null}
        title={`Корректировка цены — ${editing?.name ?? ''}`}
        okText="Сохранить"
        cancelText="Отмена"
        onOk={() => void saveOverride()}
        onCancel={() => setEditing(null)}
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <InputNumber
            min={0}
            style={{ width: '100%' }}
            value={editPrice}
            onChange={(value) => setEditPrice(value as number | null)}
            placeholder="цена в КП"
          />
          <Input.TextArea
            rows={3}
            value={editReason}
            onChange={(event) => setEditReason(event.target.value)}
            placeholder="Причина корректировки — обязательна, попадёт в журнал"
          />
        </Space>
      </Modal>
    </Space>
  );
}
