import axios from 'axios'

export const api = axios.create({ baseURL: '/' })

// Токен один на сервис и хранится в памяти модуля; интерцептор ставит его
// на каждый запрос, чтобы страницы про авторизацию не знали.
let authToken: string | null = null

export function setAuthToken(token: string | null): void {
  authToken = token
}

api.interceptors.request.use((config) => {
  if (authToken) {
    config.headers.Authorization = `Bearer ${authToken}`
  }
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401) {
      // Разлогинивание вынесено в событие: интерцептор не должен знать
      // про React-контекст.
      window.dispatchEvent(new Event('fsa-auth-expired'))
    }
    return Promise.reject(error)
  },
)

export interface RegistryImportItem {
  id: string
  filename: string
  kind: 'index' | 'numbers' | 'archive'
  status: 'queued' | 'running' | 'completed' | 'failed'
  size_bytes: number
  certificates_loaded: number
  numbers_loaded: number
  error: string
  note: string
  started_at: string | null
  finished_at: string | null
}

export type Verdict =
  | 'valid'
  | 'expired'
  | 'suspended'
  | 'terminated'
  | 'not_found'
  | 'unknown'

export interface DocumentCheck {
  verdict: Verdict
  matched_by: string
  checked_on: string
  valid_until: string | null
  needs_manual_review: boolean
  review_reasons: string[]
  successor_score: number | null
}

export interface DocumentItem {
  id: string
  sha256: string
  filename: string
  source_path: string
  supplier: string
  status: string
  doc_class: string
  text_source: string
  engine: string
  page_count: number
  mean_confidence: number | null
  registration_number: string
  error: string
  created_at: string | null
  recognized_at: string | null
  check?: DocumentCheck
}

export interface Certificate {
  registration_number: string
  status: string
  valid_until: string | null
  registered_at: string | null
  manufacturer_name: string
  manufacturer_inn: string
  product_full_name: string
  technical_regulations: string
  okpd2_tnved: string
}

export async function fetchSummary() {
  const { data } = await api.get<{
    by_status: Record<string, number>
    by_class: Record<string, number>
    by_verdict: Record<string, number>
  }>('/v1/documents/summary')
  return data
}

export async function fetchDocuments(params: {
  verdict?: string
  supplier?: string
  status?: string
  limit?: number
  offset?: number
}) {
  const { data } = await api.get<{ total: number; items: DocumentItem[] }>(
    '/v1/documents',
    { params },
  )
  return data
}

export async function fetchDocument(id: string) {
  const { data } = await api.get<
    DocumentItem & { extracted: Record<string, unknown>; ocr_text_preview: string; download_url?: string }
  >(`/v1/documents/${id}`)
  return data
}

export async function recheckDocument(id: string) {
  const { data } = await api.post(`/v1/documents/${id}/recheck`)
  return data
}

export async function uploadDocument(file: File, supplier: string) {
  const form = new FormData()
  form.append('file', file)
  form.append('supplier', supplier)
  const { data } = await api.post('/v1/documents/upload', form)
  return data as { created: boolean; recognized: boolean; reused: boolean; document: DocumentItem }
}

export async function searchRegistry(query: string) {
  const { data } = await api.get<{ items: Certificate[] }>('/v1/registry/search', {
    params: { q: query },
  })
  return data.items
}

export async function fetchRegistryStats() {
  const { data } = await api.get<{
    certificates: number
    numbers: number
    alive: number
    checked_on: string
  }>('/v1/registry/stats')
  return data
}


export async function fetchRegistryImports() {
  const { data } = await api.get<{ total: number; items: RegistryImportItem[] }>(
    '/v1/registry/imports',
  )
  return data
}

export async function uploadRegistryFile(file: File, note: string) {
  const form = new FormData()
  form.append('file', file)
  form.append('note', note)
  const { data } = await api.post<{ import: RegistryImportItem; task_id: string }>(
    '/v1/registry/imports',
    form,
  )
  return data
}

export async function recheckAllDocuments() {
  const { data } = await api.post<{ queued: boolean; task_id: string; limit: number }>(
    '/v1/documents/recheck-all',
  )
  return data
}

export type DecisionAction = 'confirm' | 'reject' | 'correct'

export interface ReviewQueueItem {
  document_id: string
  filename: string
  supplier: string
  doc_class: string
  verdict: Verdict
  review_reasons: string[]
  decided: boolean
  action: DecisionAction | ''
  reviewer: string
  decided_at: string
  stale: boolean
  stale_reason: string
  attachable: boolean
}

export interface ReviewSummary {
  documents: number
  pending: number
  stale: number
  confirmed: number
  rejected: number
  corrected: number
}

export async function fetchReviewQueue(state: 'pending' | 'decided' | 'all' = 'pending') {
  const { data } = await api.get<{ total: number; state: string; items: ReviewQueueItem[] }>(
    '/v1/review/queue',
    { params: { state, limit: 200 } },
  )
  return data
}

export async function fetchReviewSummary() {
  const { data } = await api.get<ReviewSummary>('/v1/review/summary')
  return data
}

export async function decideDocument(payload: {
  documentId: string
  action: DecisionAction
  reviewer: string
  comment?: string
  number?: string
}) {
  const { data } = await api.post(`/v1/review/documents/${payload.documentId}/decision`, {
    action: payload.action,
    reviewer: payload.reviewer,
    comment: payload.comment ?? '',
    number: payload.number ?? '',
  })
  return data as {
    decision: { id: string; action: DecisionAction; reviewer: string; corrected_verdict: string }
    effective: { verdict: Verdict; action: DecisionAction; attachable: boolean; needs_review: boolean }
  }
}

export async function fetchDecisions(documentId: string) {
  const { data } = await api.get(`/v1/review/documents/${documentId}/decisions`)
  return data as {
    document_id: string
    effective: {
      verdict: Verdict
      decided: boolean
      action: DecisionAction | ''
      reviewer: string
      decided_at: string
      stale: boolean
      stale_reason: string
      attachable: boolean
      needs_review: boolean
    }
    history: Array<{
      id: string
      action: DecisionAction
      reviewer: string
      comment: string
      verdict_at_decision: string
      corrected_number: string
      corrected_verdict: string
      created_at: string | null
    }>
  }
}
