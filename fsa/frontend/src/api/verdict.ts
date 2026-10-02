import type { Verdict } from './client'

// Подписи и цвета вынесены отдельно: вердикт показывается в трёх местах,
// и расхождение формулировок сбивало бы менеджера.
export const VERDICT_LABEL: Record<Verdict, string> = {
  valid: 'действует',
  expired: 'срок истёк',
  suspended: 'приостановлен',
  terminated: 'прекращён',
  not_found: 'нет в реестре',
  unknown: 'неизвестно',
}

export const VERDICT_COLOR: Record<Verdict, string> = {
  valid: 'green',
  expired: 'volcano',
  suspended: 'orange',
  terminated: 'red',
  not_found: 'default',
  unknown: 'gold',
}

export function isAttachable(verdict: Verdict): boolean {
  // Прикладывать к коммерческому предложению можно только действующий
  // документ: остальные вердикты означают ручную проверку.
  return verdict === 'valid'
}
