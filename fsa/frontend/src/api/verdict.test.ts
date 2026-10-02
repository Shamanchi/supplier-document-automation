import { describe, expect, it } from 'vitest'

import { VERDICT_LABEL, isAttachable } from './verdict'

describe('вердикт', () => {
  it('к КП прикладывается только действующий документ', () => {
    expect(isAttachable('valid')).toBe(true)
    for (const verdict of ['expired', 'suspended', 'terminated', 'not_found', 'unknown'] as const) {
      expect(isAttachable(verdict)).toBe(false)
    }
  })

  it('у каждого вердикта есть русская подпись', () => {
    for (const label of Object.values(VERDICT_LABEL)) {
      expect(label.length).toBeGreaterThan(0)
    }
  })
})
