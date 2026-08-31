import { http, requestData } from './client'
import { getAccessToken } from '@/auth/session'

const BASE = '/agents/ecommerce-competitor'

export interface CompetitorSession {
  session_id: string
  messages: { role: string; content: string }[]
  slots: {
    platform: string | null
    brand: string | null
    count: number | null
  }
  active_run_id: string | null
}

export interface CompetitorRunRef {
  session_id: string
  run_id: string
}

export type CompetitorStreamEvent =
  | { type: 'assistant'; message: string }
  | { type: 'progress'; message?: string; [k: string]: unknown }
  | {
      type: 'artifact'
      artifact_type?: string
      summary?: string
      preview_columns?: string[]
      preview_rows?: unknown[]
      filename?: string
      download_url?: string
    }
  | { type: 'error'; message: string }
  | { type: 'done' }
  | { type: string; [k: string]: unknown }

export function createCompetitorSession() {
  return requestData<{ session_id: string }>(http.post(`${BASE}/sessions`))
}

export function getCompetitorSession(sessionId: string) {
  return requestData<CompetitorSession>(http.get(`${BASE}/sessions/${sessionId}`))
}

export function postCompetitorMessage(sessionId: string, message: string) {
  return requestData<CompetitorRunRef>(
    http.post(`${BASE}/sessions/${sessionId}/messages`, { message }),
  )
}

/** Consume SSE run stream; calls onEvent for each parsed payload. */
export async function streamCompetitorRun(
  sessionId: string,
  runId: string,
  onEvent: (ev: CompetitorStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const token = getAccessToken()
  const res = await fetch(`/api${BASE}/sessions/${sessionId}/runs/${runId}/stream`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    signal,
  })
  if (!res.ok || !res.body) {
    throw new Error(`SSE 连接失败 (${res.status})`)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const chunks = buf.split('\n\n')
    buf = chunks.pop() || ''
    for (const chunk of chunks) {
      const line = chunk
        .split('\n')
        .map((l) => l.trim())
        .find((l) => l.startsWith('data:'))
      if (!line) continue
      const raw = line.slice(5).trim()
      if (!raw) continue
      try {
        onEvent(JSON.parse(raw) as CompetitorStreamEvent)
      } catch {
        // ignore malformed
      }
    }
  }
}

export function competitorDownloadHref(filename: string) {
  return `/api${BASE}/downloads/${encodeURIComponent(filename)}`
}
