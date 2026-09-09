import { http, requestData } from './client'

export interface SessionSummary {
  session_id: string
  title: string
  preview: string
  updated_at: number
  created_at: number
}

export interface SessionListData {
  items: SessionSummary[]
  total: number
  page: number
  page_size: number
  total_pages: number
  has_next: boolean
  has_prev: boolean
}

export interface SessionDetail {
  session_id: string
  title: string
  messages: { role: string; content: string }[]
  updated_at: number
  created_at: number
  active_run_id?: string | null
}

export function listSessions(page = 1, pageSize = 50) {
  return requestData<SessionListData>(
    http.get('/sessions', { params: { page, page_size: pageSize } }),
  )
}

export function getSession(sessionId: string) {
  return requestData<SessionDetail>(http.get(`/sessions/${encodeURIComponent(sessionId)}`))
}

export function deleteSession(sessionId: string) {
  return requestData<boolean>(http.post('/sessions/delete', { session_id: sessionId }))
}
