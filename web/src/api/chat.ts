import { http, requestData } from './client'
import type { ChatData } from './types'

export function postChat(message: string, sessionId?: string | null) {
  return requestData<ChatData>(
    http.post('/chat', {
      message,
      session_id: sessionId || undefined,
    }),
  )
}
