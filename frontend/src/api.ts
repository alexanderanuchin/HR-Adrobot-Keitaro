export type Item = { id: number; name: string }
export type Session = { username: string | null; csrf_token: string }
export type Catalog = { domains: Item[]; groups: Item[]; sources: Item[] }
export type CampaignInput = {
  name: string
  alias: string
  geo: string
  offer_id: number
  domain_id: number
  group_id: number
  traffic_source_id: number
}
export type CampaignResult = {
  alias: string
  status: 'COMPLETED' | 'UNCERTAIN'
  campaign_id: number | null
  stage: string
}
export type Stream = {
  id: string
  keitaro_id: number
  name: string
  type: string
  schema: string
  position: number | null
  present: boolean
  editable: boolean
  draft_status: string | null
}
export type Campaign = { id: number; name: string | null; streams: Stream[] }
export type Position = {
  position_id: string
  keitaro_offer_id: number
  status: 'ACTIVE' | 'REMOVED'
  pinned_percent: number | null
  calculated_percent: number
}
export type Draft = {
  draft_id: string
  stream_id: string
  revision: number
  status: string
  reason: string | null
  upstream_status: number | null
  reconciliation_checked: boolean
  publication_error: string | null
  positions: Position[]
}

let csrfToken = ''

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
  }
}

async function request<T>(path: string, body?: object, signal?: AbortSignal, query?: URLSearchParams): Promise<T> {
  const response = await fetch(`/api/v1/${path}/${query ? `?${query}` : ''}`, {
    credentials: 'same-origin',
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? undefined : {
      'Content-Type': 'application/json',
      'X-CSRFToken': csrfToken,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  })
  const data = await response.json().catch(() => null)
  if (!response.ok) {
    const fallback = response.status === 403
      ? 'Нет доступа. Проверьте вход и обновите страницу.'
      : `Запрос завершился ошибкой HTTP ${response.status}.`
    throw new ApiError(typeof data?.detail === 'string' ? data.detail : fallback, response.status)
  }
  return data as T
}

export async function session(body?: { username: string; password: string }): Promise<Session> {
  const data = await request<Session>('session', body)
  csrfToken = data.csrf_token
  return data
}

export async function signOut() {
  const data = await request<Session>('session/logout', {})
  csrfToken = data.csrf_token
}

export function workspace<T>(resource: string, body?: object, signal?: AbortSignal, query?: URLSearchParams): Promise<T> {
  return request<T>(`workspace/${resource}`, body, signal, query)
}

export function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message
  return 'Не удалось связаться с приложением. Проверьте подключение и повторите запрос.'
}
