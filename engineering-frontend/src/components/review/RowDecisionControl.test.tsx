import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import RowDecisionControl from './RowDecisionControl';

const blocked = [{ sheet: 'Счёт', row: 1, column: 3, value: '7707083893' }];

describe('RowDecisionControl', () => {
  it('requires a concrete reason and a separate click to override the guard', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(
      <RowDecisionControl
        open
        sheet="Счёт"
        excelRow={1}
        panel={null}
        pending={false}
        blocked={blocked}
        onCancel={() => {}}
        onConfirm={onConfirm}
      />,
    );

    expect(await screen.findByText('Решение скрывает ценовой признак')).toBeInTheDocument();
    expect(screen.getByText(/лист «Счёт», строка 1, колонка 3: 7707083893/)).toBeInTheDocument();
    // Обычного «Сохранить решение» в этом состоянии нет: обход подтверждают явно.
    expect(screen.queryByRole('button', { name: 'Сохранить решение' })).toBeNull();
    const override = screen.getByRole('button', { name: 'Обойти защиту и отклонить' });
    expect(override).toBeDisabled();

    await user.click(screen.getByRole('combobox', { name: 'Код причины' }));
    await user.click(await screen.findByText('Техническая таблица'));
    const comment = screen.getByPlaceholderText('Почему эта строка не должна создавать позицию');
    fireEvent.change(comment, { target: { value: 'Требование к шапке счёта' } });
    expect(override).toBeEnabled();
    expect(onConfirm).not.toHaveBeenCalled();

    await user.click(override);
    expect(onConfirm.mock.calls[0][0]).toMatchObject({
      sheet: 'Счёт',
      row_start: 1,
      row_end: 1,
      reason_code: 'technical_table',
      override_guard: true,
    });
  }, 30_000);

  it('saves a plain decision without the override flag when nothing is blocked', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(
      <RowDecisionControl
        open
        sheet="Прайс"
        excelRow={2}
        panel={null}
        pending={false}
        onCancel={() => {}}
        onConfirm={onConfirm}
      />,
    );

    expect(screen.queryByText('Решение скрывает ценовой признак')).toBeNull();
    const comment = await screen.findByPlaceholderText('Почему эта строка не должна создавать позицию');
    fireEvent.change(comment, { target: { value: 'Техническое примечание' } });
    await user.click(screen.getByRole('button', { name: 'Сохранить решение' }));

    expect(onConfirm.mock.calls[0][0].override_guard).toBeUndefined();
  });
});
