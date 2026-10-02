import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import ReviewProgress from './ReviewProgress';
import ValidationDiff from './ValidationDiff';

describe('review presentation', () => {
  it('renders queue totals', () => {
    render(<ReviewProgress totals={{ pending: 104, accepted: 1 }} />);
    expect(screen.getByText('104')).toBeVisible();
    expect(screen.getByText('Приняты')).toBeVisible();
  });

  it('renders an empty and a coordinate diff state', () => {
    const { rerender } = render(<ValidationDiff validation={null} />);
    expect(screen.getByText('Текущая revision ещё не проверена')).toBeVisible();
    rerender(<ValidationDiff validation={{
      revision: 2,
      passed: false,
      at: '2026-07-19T00:00:00Z',
      state_hash: 'hash',
      metrics: { item_count: 1 },
      diff: { added: [{ sheet: 'Прайс', row: 2, panel: 0, name: 'Товар' }] },
    }} />);
    expect(screen.getByText('Dry-run содержит блокирующие ошибки')).toBeVisible();
    expect(screen.getByText('Прайс!2 · panel 1')).toBeVisible();
  });

  it('renders a successful validation without coordinate changes', () => {
    render(<ValidationDiff validation={{
      revision: 3,
      passed: true,
      at: '2026-07-19T00:00:00Z',
      state_hash: 'hash',
      metrics: {},
      diff: {},
    }} />);
    expect(screen.getByText('Dry-run прошёл')).toBeVisible();
    expect(screen.getByText('Coordinate diff не содержит построчных изменений.')).toBeVisible();
  });
});
