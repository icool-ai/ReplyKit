import { http, requestData } from './client'

export interface SensitiveItem {
  id: string
  pattern: string
  enabled: boolean
  note: string
  created_at: number
  updated_at: number
}

export interface SensitiveListData {
  items: SensitiveItem[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

export function listSensitiveWords(params: {
  page?: number
  page_size?: number
  keyword?: string
  enabled?: boolean | null
}) {
  return requestData<SensitiveListData>(
    http.post('/sensitive-words/list', {
      page: params.page ?? 1,
      page_size: params.page_size ?? 20,
      keyword: params.keyword || undefined,
      enabled: params.enabled ?? undefined,
    }),
  )
}

export function createSensitiveWord(body: {
  pattern: string
  note?: string
  enabled?: boolean
}) {
  return requestData<SensitiveItem>(http.post('/sensitive-words', body))
}

export function updateSensitiveWord(body: {
  id: string
  pattern?: string
  note?: string
  enabled?: boolean
}) {
  return requestData<SensitiveItem>(http.post('/sensitive-words/update', body))
}

export function deleteSensitiveWords(ids: string[]) {
  return requestData<boolean>(http.post('/sensitive-words/delete', { ids }))
}

export function importSensitiveWords(path: string) {
  return requestData<{ imported: number; skipped: number; total_in_file: number }>(
    http.post('/sensitive-words/import', { path }),
  )
}
