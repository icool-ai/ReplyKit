import { http, requestData } from './client'
import { getAccessToken } from '@/auth/session'
import type { ChatData } from './types'

export function postChat(message: string, sessionId?: string | null) {
  return requestData<ChatData>(
    http.post('/chat', {
      message,
      session_id: sessionId || undefined,
    }),
  )
}

export interface ChatRunRef {
  session_id: string
  run_id: string
}

export type ChatStreamEvent =
  | { id?: number; type: 'status'; phase?: string }
  | { id?: number; type: 'delta'; text?: string }
  | {
      id?: number
      type: 'meta'
      sources?: string[]
      images?: string[]
      clarify_options?: string[]
      route?: string
      strategy?: string
    }
  | { id?: number; type: 'error'; message?: string }
  | { id?: number; type: 'done'; session_id?: string }
  | { id?: number; type: string; [k: string]: unknown }

export function createChatRun(message: string, sessionId?: string | null) {
  return requestData<ChatRunRef>(
    http.post('/chat/runs', {
      message,
      session_id: sessionId || undefined,
    }),
  )
}

function parseSseChunk(chunk: string): ChatStreamEvent | null {
  const lines = chunk.split('\n').map((l) => l.trimEnd())
  let eventId: number | undefined
  let dataLine = ''
  for (const line of lines) {
    if (line.startsWith('id:')) {
      const raw = line.slice(3).trim()
      if (/^\d+$/.test(raw)) eventId = Number(raw)
    } else if (line.startsWith('data:')) {
      dataLine = line.slice(5).trim()
    }
  }
  if (!dataLine) return null
  try {
    const parsed = JSON.parse(dataLine) as ChatStreamEvent
    if (eventId != null && parsed.id == null) parsed.id = eventId
    return parsed
  } catch {
    return null
  }
}

/** Single SSE subscription from cursor (Last-Event-ID / after). */
export async function streamChatRunOnce(
  runId: string,
  afterId: number,
  onEvent: (ev: ChatStreamEvent) => void,
  signal?: AbortSignal,
): Promise<'done' | 'disconnected'> {
  const token = getAccessToken()
  const qs = afterId > 0 ? `?after=${afterId}` : ''
  const res = await fetch(`/api/chat/runs/${encodeURIComponent(runId)}/stream${qs}`, {
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(afterId > 0 ? { 'Last-Event-ID': String(afterId) } : {}),
    },
    signal,
  })
  if (!res.ok || !res.body) {
    const err = new Error(`SSE 连接失败 (${res.status})`) as Error & { status?: number }
    err.status = res.status
    throw err
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  let sawDone = false
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const chunks = buf.split('\n\n')
    buf = chunks.pop() || ''
    for (const chunk of chunks) {
      if (!chunk.trim() || chunk.trim().startsWith(':')) continue
      const ev = parseSseChunk(chunk)
      if (!ev) continue
      onEvent(ev)
      if (ev.type === 'done') sawDone = true
    }
  }
  return sawDone ? 'done' : 'disconnected'
}

/**
 * Consume chat SSE with auto-reconnect + Last-Event-ID resume (capability A).
 * Stops on done, abort, or exhausted retries / terminal HTTP status.
 */
export async function streamChatRun(
  runId: string,
  onEvent: (ev: ChatStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  let lastId = 0
  let attempt = 0
  const seen = new Set<number>()

  const handle = (ev: ChatStreamEvent) => {
    const id = typeof ev.id === 'number' ? ev.id : undefined
    if (id != null) {
      if (seen.has(id)) return
      seen.add(id)
      lastId = Math.max(lastId, id)
    }
    onEvent(ev)
  }

  while (true) {
    if (signal?.aborted) return
    try {
      const result = await streamChatRunOnce(runId, lastId, handle, signal)
      if (result === 'done') return
      // disconnected without done → reconnect
    } catch (err) {
      if (signal?.aborted) return
      const status = (err as { status?: number }).status
      if (status === 404 || status === 410 || status === 403) throw err
      if (status === 401) throw err
    }
    attempt += 1
    if (attempt > 5) {
      throw new Error('连接中断，请刷新会话或重试')
    }
    onEvent({ type: 'status', phase: 'reconnecting' })
    const delay = Math.min(1000 * 2 ** (attempt - 1), 8000)
    const jitter = delay * (0.8 + Math.random() * 0.4)
    await new Promise<void>((resolve, reject) => {
      const t = window.setTimeout(resolve, jitter)
      signal?.addEventListener(
        'abort',
        () => {
          window.clearTimeout(t)
          reject(new DOMException('Aborted', 'AbortError'))
        },
        { once: true },
      )
    }).catch((e) => {
      if ((e as Error).name === 'AbortError') return
      throw e
    })
    if (signal?.aborted) return
  }
}
