import { describe, expect, it } from 'vitest';
import { extractApiError, extractApiErrorDetails, extractApiErrorEnvelope } from './client';

function axiosError(data: unknown, status = 422) {
  return {
    isAxiosError: true,
    message: `Request failed with status code ${status}`,
    response: { status, data },
  };
}

describe('разбор ошибок API', () => {
  it('читает конверт контура Фазы 3 вместе с координатами отказа', () => {
    const error = axiosError({
      error: {
        code: 'UNSAFE_NON_PRICE_DECISION',
        message: 'Решение скрывает строку с ценовым признаком',
        details: {
          sheet: 'Счёт',
          row: 1,
          column: 3,
          value: '7707083893',
          evidence: [{ sheet: 'Счёт', row: 1, column: 3, value: '7707083893' }],
        },
      },
    });

    expect(extractApiError(error)).toBe('Решение скрывает строку с ценовым признаком');
    expect(extractApiErrorEnvelope(error)?.code).toBe('UNSAFE_NON_PRICE_DECISION');
    expect(extractApiErrorDetails(error)?.sheet).toBe('Счёт');
    expect(extractApiErrorDetails(error)?.evidence).toEqual([
      { sheet: 'Счёт', row: 1, column: 3, value: '7707083893' },
    ]);
  });

  it('не ломает Problem Details контура Фазы 2', () => {
    const error = axiosError(
      {
        type: 'urn:docprice:error:validation_error',
        title: 'VALIDATION_ERROR',
        status: 422,
        detail: 'Некорректный статус версии',
      },
      409,
    );

    expect(extractApiError(error)).toBe('Некорректный статус версии');
    expect(extractApiErrorEnvelope(error)).toBeNull();
    expect(extractApiErrorDetails(error)).toBeUndefined();
  });

  it('сохраняет прежний разбор строкового detail и словаря без message', () => {
    expect(
      extractApiError(axiosError({ detail: 'Лист решения не найден в исходном файле' })),
    ).toBe('Лист решения не найден в исходном файле');
    expect(extractApiError(axiosError({ detail: { current_revision: 3 } }))).toBe(
      '{"current_revision":3}',
    );
    expect(extractApiError(new Error('Network Error'))).toBe('Network Error');
  });
});
