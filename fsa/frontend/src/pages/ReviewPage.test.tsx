import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import ReviewPage from './ReviewPage'
import { decideDocument } from '../api/client'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return {
    ...actual,
    fetchReviewSummary: vi.fn().mockResolvedValue({
      documents: 3,
      pending: 2,
      stale: 1,
      confirmed: 1,
      rejected: 0,
      corrected: 0,
    }),
    fetchReviewQueue: vi.fn().mockResolvedValue({
      total: 2,
      state: 'pending',
      items: [
        {
          document_id: 'doc-1',
          filename: 'sert.pdf',
          supplier: 'Supplier A',
          doc_class: 'certificate',
          verdict: 'valid',
          review_reasons: [],
          decided: false,
          action: '',
          reviewer: '',
          decided_at: '',
          stale: false,
          stale_reason: '',
          attachable: false,
        },
        {
          document_id: 'doc-2',
          filename: 'stale.pdf',
          supplier: 'Supplier E',
          doc_class: 'certificate',
          verdict: 'expired',
          review_reasons: ['срок действия истёк'],
          decided: true,
          action: 'confirm',
          reviewer: 'Иванов',
          decided_at: '2026-07-01T10:00:00+00:00',
          stale: true,
          stale_reason: 'вердикт изменился с valid на expired',
          attachable: false,
        },
      ],
    }),
    decideDocument: vi.fn().mockResolvedValue({
      decision: { id: 'd1', action: 'confirm', reviewer: 'Петров', corrected_verdict: '' },
      effective: { verdict: 'valid', action: 'confirm', attachable: true, needs_review: false },
    }),
  }
})

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ReviewPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ReviewPage', () => {
  it('показывает очередь и помечает устаревшее подтверждение', async () => {
    renderPage()

    expect(await screen.findByText('sert.pdf')).toBeInTheDocument()
    expect(
      await screen.findByText(/устарело: вердикт изменился с valid на expired/),
    ).toBeInTheDocument()
  })

  it('не отправляет решение без имени проверяющего', async () => {
    renderPage()
    const user = userEvent.setup()

    const buttons = await screen.findAllByRole('button', { name: 'Подтвердить' })
    await user.click(buttons[0])

    await waitFor(() => expect(decideDocument).not.toHaveBeenCalled())
  })

  it('отправляет подтверждение с именем проверяющего', async () => {
    renderPage()
    const user = userEvent.setup()

    await user.type(await screen.findByPlaceholderText('кто проверяет'), 'Петров')
    const buttons = await screen.findAllByRole('button', { name: 'Подтвердить' })
    await user.click(buttons[0])

    await waitFor(() => expect(vi.mocked(decideDocument)).toHaveBeenCalled())
    const [payload] = vi.mocked(decideDocument).mock.calls.at(-1) ?? []
    expect(payload).toMatchObject({ documentId: 'doc-1', action: 'confirm', reviewer: 'Петров' })
  })
})
