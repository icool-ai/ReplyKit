/** 与后端统一信封对齐：{ code, message, data } */
export interface ApiEnvelope<T> {
  code: number
  message: string
  data: T
}

export interface ChatData {
  session_id: string
  answer: string
  sources: string[]
  images: string[]
  clarify_options: string[]
  route: string
  strategy: string
}

export interface FaqItem {
  id: string
  category: string
  question: string
  answer: string
  similar: string[]
  enabled: boolean
  created_at: number
  updated_at: number
}

export interface FaqListData {
  items: FaqItem[]
  total: number
  page: number
  page_size: number
  total_pages: number
  has_next: boolean
  has_prev: boolean
}

export interface FaqCreateBody {
  question: string
  answer: string
  similar?: string[]
  category?: string
  id?: string
  enabled?: boolean
}

export interface FaqUpdateBody {
  id: string
  question?: string
  answer?: string
  similar?: string[]
  category?: string
  enabled?: boolean
}
