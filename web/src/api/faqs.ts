import { http, requestData } from './client'
import type { FaqCreateBody, FaqItem, FaqListData, FaqUpdateBody } from './types'
import { getAccessToken } from '@/auth/session'
import { ApiError } from './client'

export function listFaqs(params: {
  page?: number
  page_size?: number
  keyword?: string
  category?: string
  enabled?: boolean | null
}) {
  return requestData<FaqListData>(
    http.post('/faqs/list', {
      page: params.page ?? 1,
      page_size: params.page_size ?? 20,
      keyword: params.keyword || undefined,
      category: params.category || undefined,
      enabled: params.enabled ?? undefined,
    }),
  )
}

export function createFaq(body: FaqCreateBody) {
  return requestData<FaqItem>(http.post('/faqs', body))
}

export function updateFaq(body: FaqUpdateBody) {
  return requestData<FaqItem>(http.post('/faqs/update', body))
}

export function deleteFaqs(ids: string[]) {
  return requestData<boolean>(http.post('/faqs/delete', { ids }))
}

export function importFaqs(body: { path?: string; url?: string }) {
  return requestData<{ imported: number; total_in_file: number; indexing: string }>(
    http.post('/faqs/import', body),
  )
}

export function importFaqFile(file: File) {
  const form = new FormData()
  form.append('file', file)
  return requestData<{ imported: number; total_in_file: number; indexing: string }>(
    http.post('/faqs/import-file', form, {
      transformRequest: [
        (data, headers) => {
          if (data instanceof FormData) {
            headers.delete('Content-Type')
          }
          return data
        },
      ],
    }),
  )
}

export type FaqTemplateFormat = 'json' | 'csv' | 'txt' | 'xls' | 'xlsx'

export async function downloadFaqTemplate(format: FaqTemplateFormat) {
  const token = getAccessToken()
  const res = await fetch(`/api/faqs/import-templates/${format}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (!res.ok) {
    let message = `下载失败（HTTP ${res.status}）`
    try {
      const body = await res.json()
      if (body?.message) message = body.message
      else if (body?.detail) message = String(body.detail)
    } catch {
      // ignore
    }
    throw new ApiError(res.status, message)
  }
  const blob = await res.blob()
  const cd = res.headers.get('Content-Disposition') || ''
  const match = /filename="?([^";]+)"?/i.exec(cd)
  const filename = match?.[1] || `faq_template.${format}`
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}
