import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import PlaygroundPage from './PlaygroundPage';
import { createCalculation, overrideCalculationPrice, rememberSynonym, runPlayground } from '../api/client';

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client');
  return {
    ...actual,
    fetchCanonicalStats: vi.fn().mockResolvedValue({
      canonicals: 12,
      auto: 10,
      confirmed: 1,
      disputed: 1,
      sources: 30,
    }),
    rebuildCanonical: vi.fn().mockResolvedValue({ items: 30, canonicals: 12, disputed: 1 }),
    rememberSynonym: vi.fn().mockResolvedValue(undefined),
    createCalculation: vi.fn().mockResolvedValue({
      id: 'calc-1',
      title: 'Расчёт из песочницы',
      status: 'draft',
      total_amount: 11500,
      items: [
        {
          id: 'row-1',
          ordinal: 1,
          source_line: 'Огнетушитель ОП-4 — 25 шт',
          name: 'ОП-4 (з) ВСЕ ЗПУ Россия',
          supplier_id: 'sup-1',
          quantity: 25,
          unit_code: 'шт',
          base_price: 460,
          discount_percent: 10,
          discount_source: 'supplier',
          price_after_discount: 414,
          manual_price: null,
          manual_reason: '',
          manual_by: '',
          manual_at: null,
          final_price: 414,
        },
      ],
      totals: { base: 11500, discounted: 10350, final: 10350, by_supplier: {} },
    }),
    overrideCalculationPrice: vi.fn().mockResolvedValue({
      id: 'row-1',
      ordinal: 1,
      source_line: 'Огнетушитель ОП-4 — 25 шт',
      name: 'ОП-4 (з) ВСЕ ЗПУ Россия',
      supplier_id: 'sup-1',
      quantity: 25,
      unit_code: 'шт',
      base_price: 460,
      discount_percent: 10,
      discount_source: 'supplier',
      price_after_discount: 414,
      manual_price: 400,
      manual_reason: 'согласовано с клиентом',
      manual_by: 'оператор',
      manual_at: '2026-07-31T10:00:00Z',
      final_price: 400,
    }),
    runPlayground: vi.fn().mockResolvedValue({
      checked_on: '2026-07-30',
      certificates_configured: true,
      thresholds: { auto: 0.85, review: 0.6 },
      lines: [
        {
          ordinal: 1,
          source_line: 'Огнетушитель ОП-4 — 25 шт',
          query_name: 'Огнетушитель ОП-4',
          quantity: 25,
          unit_code: 'шт',
          warnings: [],
          decision: 'auto',
          stage: 'article',
          candidates: [
            {
              canonical_id: 'canon-1',
              title: 'Огнетушитель ОП-4(з)-АВСЕ',
              article: 'ОП4',
              merge_status: 'auto',
              stage: 'article',
              score: 0.95,
            },
          ],
          designations: ['ОП-4'],
          variants: [
            {
              canonical_id: 'canon-1',
              title: 'ОП-4 (з) ВСЕ ЗПУ Россия',
              article: 'ОП4',
              merge_status: 'auto',
              matched_designation: 'ОП-4',
              relevance: 1,
              offers: [
                {
                  supplier: 'Supplier A',
                  price_item_id: 'item-1',
                  name: 'ОП-4 (з) ВСЕ ЗПУ Россия',
                  unit: 'шт',
                  price: 460,
                  currency: 'RUB',
                },
              ],
            },
            {
              canonical_id: 'canon-2',
              title: 'ОП-4 (з) АВСЕ ЗПУ Китай',
              article: 'ОП4',
              merge_status: 'auto',
              matched_designation: 'ОП-4',
              relevance: 0.5,
              offers: [
                {
                  supplier: 'Supplier A',
                  price_item_id: 'item-2',
                  name: 'ОП-4 (з) АВСЕ ЗПУ Китай',
                  unit: 'шт',
                  price: 448,
                  currency: 'RUB',
                },
              ],
            },
          ],
          certificate: {
            available: true,
            verdict: 'valid',
            registration_number: 'ЕАЭС RU С-RU.ПБ97.В.00272/21',
            valid_until: '2026-11-14',
            manufacturer: 'ООО Системы Пожаротушения',
            matched_by: 'product_key',
            candidates: 1,
            needs_manual_review: false,
            review_reasons: [],
            confirmed_by: 'Иванов',
            attachable: true,
            reason_unavailable: '',
          },
        },
        {
          ordinal: 2,
          source_line: 'Сервер стоечный 2U',
          query_name: 'Сервер стоечный 2U',
          quantity: null,
          unit_code: null,
          warnings: ['количество не распознано'],
          decision: 'not_found',
          stage: 'none',
          candidates: [],
        },
      ],
    }),
  };
});

describe('PlaygroundPage', () => {
  it('показывает разбор строки, вердикт сертификата и «не найдено»', async () => {
    render(<PlaygroundPage />);
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: 'Подобрать' }));

    expect(await screen.findByText('Огнетушитель ОП-4(з)-АВСЕ')).toBeInTheDocument();
    expect(screen.getByText('принято автоматически')).toBeInTheDocument();
    expect(screen.getByText('действует')).toBeInTheDocument();
    expect(screen.getByText('можно приложить')).toBeInTheDocument();
    expect(screen.getByText('не найдено')).toBeInTheDocument();
    expect(screen.getByText('количество не распознано')).toBeInTheDocument();
    expect(vi.mocked(runPlayground)).toHaveBeenCalled();
  });

  it('раскрывает весь ряд исполнений по обозначению', async () => {
    render(<PlaygroundPage />);
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: 'Подобрать' }));
    await user.click(await screen.findByRole('button', { name: 'Expand row' }));

    expect(await screen.findByText(/Весь ряд по обозначению ОП-4/)).toBeInTheDocument();
    expect(screen.getByText('ОП-4 (з) АВСЕ ЗПУ Китай')).toBeInTheDocument();
    expect(screen.getByText(/Supplier A: 448 RUB/)).toBeInTheDocument();
  });

  it('по умолчанию ищет во всех проектах', async () => {
    render(<PlaygroundPage />);
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: 'Подобрать' }));

    await waitFor(() =>
      expect(vi.mocked(runPlayground)).toHaveBeenLastCalledWith(
        expect.any(String),
        true,
        '',
        'all',
      ),
    );
  });

  it('позволяет сузить поиск до текущего проекта', async () => {
    render(<PlaygroundPage />);
    const user = userEvent.setup();

    // У antd Segmented кликабельна подпись, а сам radio скрыт под ней и имеет
    // pointer-events: none.
    await user.click(screen.getByText('Текущий проект'));
    await user.click(screen.getByRole('button', { name: 'Подобрать' }));

    await waitFor(() =>
      expect(vi.mocked(runPlayground)).toHaveBeenLastCalledWith(
        expect.any(String),
        true,
        '',
        'current',
      ),
    );
  });

  it('запоминает выбор менеджера как синоним', async () => {
    render(<PlaygroundPage />);
    const user = userEvent.setup();

    await user.click(screen.getByRole('button', { name: 'Подобрать' }));
    await user.click(await screen.findByRole('button', { name: 'Это оно' }));

    await waitFor(() =>
      expect(vi.mocked(rememberSynonym)).toHaveBeenCalledWith('Огнетушитель ОП-4', 'canon-1'),
    );
  });

  it('собирает расчёт из выбранного предложения и показывает скидку', async () => {
    render(<PlaygroundPage />);
    // `delay: null` — без него каждый клик ждёт таймеров antd, и сценарий из
    // пяти шагов упирается в таймаут теста, а не в логику страницы.
    const user = userEvent.setup({ delay: null });

    await user.click(screen.getByRole('button', { name: 'Подобрать' }));
    await user.click(await screen.findByRole('button', { name: 'Expand row' }));
    await user.click((await screen.findAllByRole('button', { name: 'В расчёт' }))[0]);
    await user.click(await screen.findByRole('button', { name: 'Посчитать со скидками' }));

    await waitFor(() => expect(vi.mocked(createCalculation)).toHaveBeenCalled());
    expect(vi.mocked(createCalculation).mock.calls[0][0]).toEqual([
      expect.objectContaining({ price_item_id: 'item-1', quantity: 25 }),
    ]);
    expect(await screen.findByText('10%')).toBeInTheDocument();
    expect(screen.getByText('по поставщику')).toBeInTheDocument();
  }, 60000);

  it('не даёт скорректировать цену без причины', async () => {
    render(<PlaygroundPage />);
    const user = userEvent.setup({ delay: null });

    await user.click(screen.getByRole('button', { name: 'Подобрать' }));
    await user.click(await screen.findByRole('button', { name: 'Expand row' }));
    await user.click((await screen.findAllByRole('button', { name: 'В расчёт' }))[0]);
    await user.click(await screen.findByRole('button', { name: 'Посчитать со скидками' }));
    await user.click(await screen.findByRole('button', { name: 'Изменить' }));
    await user.click(await screen.findByRole('button', { name: 'Сохранить' }));

    expect(vi.mocked(overrideCalculationPrice)).not.toHaveBeenCalled();

    // `fireEvent.change`, а не посимвольный ввод: под общим прогоном сюиты
    // печать длинной причины через user-event упирается в таймаут теста.
    fireEvent.change(screen.getByPlaceholderText(/Причина корректировки/), {
      target: { value: 'согласовано с клиентом' },
    });
    await user.click(screen.getByRole('button', { name: 'Сохранить' }));

    await waitFor(() =>
      expect(vi.mocked(overrideCalculationPrice)).toHaveBeenCalledWith(
        'row-1',
        414,
        'согласовано с клиентом',
      ),
    );
    // Сценарий длинный (подбор → ряд → расчёт → диалог), и под общим прогоном
    // сюиты он идёт втрое дольше, чем в одиночку.
  }, 60000);
});
