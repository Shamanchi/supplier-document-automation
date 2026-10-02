import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import DocumentsPage from './DocumentsPage'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return {
    ...actual,
    fetchSummary: vi.fn().mockResolvedValue({
      by_status: { recognized: 4 },
      by_class: { certificate: 3 },
      by_verdict: { valid: 2, terminated: 1, unknown: 1 },
    }),
    fetchDocuments: vi.fn().mockResolvedValue({
      total: 1,
      items: [
        {
          id: 'doc-1',
          sha256: 'a'.repeat(64),
          filename: 'sert.pdf',
          source_path: '',
          supplier: 'Supplier E',
          status: 'recognized',
          doc_class: 'certificate',
          text_source: 'ocr',
          engine: 'paddle',
          page_count: 1,
          mean_confidence: 0.93,
          registration_number: 'EAЭC RU C-RU.ПБ97.B.00344/22',
          error: '',
          created_at: null,
          recognized_at: null,
          check: {
            verdict: 'terminated',
            matched_by: 'registration_number',
            checked_on: '2026-07-27',
            valid_until: '2024-03-13',
            needs_manual_review: true,
            review_reasons: ['статус в реестре: Прекращен'],
            successor_score: 0.73,
          },
        },
      ],
    }),
  }
})

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <DocumentsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('DocumentsPage', () => {
  it('показывает сводку и переводит вердикт на русский', async () => {
    renderPage()

    expect(await screen.findByText('sert.pdf')).toBeInTheDocument()
    expect(screen.getByText('прекращён')).toBeInTheDocument()
    // Дата окончания важна не меньше вердикта: по ней видно, насколько давно
    // документ мёртв.
    expect(screen.getByText('2024-03-13')).toBeInTheDocument()
  })

  it('показывает, что документ распознан именно OCR и каким движком', async () => {
    renderPage()
    expect(await screen.findByText('OCR (paddle)')).toBeInTheDocument()
  })
})
