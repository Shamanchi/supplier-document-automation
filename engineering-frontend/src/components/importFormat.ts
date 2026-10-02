import type { ImportItemPrice } from '../api/types';

export function formatDate(value: string | null): string {
  if (!value) return '—';
  return new Date(value).toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function formatNumber(value: string | number | null): string {
  if (value == null) return '—';
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 3 }).format(numeric);
}

export function formatPrice(price: ImportItemPrice): string {
  if (price.is_on_request || price.qualifier === 'on_request') return 'по запросу';
  let amount: string;
  if (price.qualifier === 'range') {
    amount = `${formatNumber(price.amount_from)}–${formatNumber(price.amount_to)}`;
  } else if (price.qualifier === 'from') {
    amount = `от ${formatNumber(price.amount_from ?? price.amount)}`;
  } else {
    amount = formatNumber(price.amount ?? price.amount_from);
  }
  const currency = price.currency_symbol ?? price.currency ?? 'валюта не определена';
  const basis = price.basis_unit
    ? ` за ${formatNumber(price.basis_quantity ?? 1)} ${price.basis_unit}`
    : '';
  let tier = '';
  if (price.min_quantity != null && price.max_quantity != null) {
    tier = ` · ${formatNumber(price.min_quantity)}–${formatNumber(price.max_quantity)} ед.`;
  } else if (price.min_quantity != null) {
    tier = ` · от ${formatNumber(price.min_quantity)} ед.`;
  } else if (price.max_quantity != null) {
    tier = ` · до ${formatNumber(price.max_quantity)} ед.`;
  }
  return `${amount} ${currency}${basis}${tier}`;
}

export function metric(record: Record<string, unknown> | null, key: string): number {
  const value = record?.[key];
  return typeof value === 'number' ? value : Number(value ?? 0) || 0;
}
