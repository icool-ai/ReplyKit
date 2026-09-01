import axios, { type AxiosError, type InternalAxiosRequestConfig } from 'axios'
import type { ApiEnvelope } from './types'
import {
  clearTokens,
  getAccessToken,
  getRefreshToken,
  setTokens,
} from '@/auth/session'
import type { TokenData } from '@/auth/session'

export const http = axios.create({
  baseURL: '/api',
  timeout: 120_000,
  headers: {
    'Content-Type': 'application/json',
  },
})

export class ApiError extends Error {
  code: number

  constructor(code: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.code = code
  }
}

let refreshPromise: Promise<string | null> | null = null

async function refreshAccessToken(): Promise<string | null> {
  const rt = getRefreshToken()
  if (!rt) return null
  try {
    const { data: body } = await axios.post<ApiEnvelope<TokenData>>(
      '/api/auth/refresh',
      { refresh_token: rt },
      { headers: { 'Content-Type': 'application/json' } },
    )
    if (body.code !== 200 || !body.data) {
      clearTokens()
      return null
    }
    setTokens(body.data)
    return body.data.access_token
  } catch {
    clearTokens()
    return null
  }
}

function ensureRefresh(): Promise<string | null> {
  if (!refreshPromise) {
    refreshPromise = refreshAccessToken().finally(() => {
      refreshPromise = null
    })
  }
  return refreshPromise
}

http.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const url = config.url || ''
  if (
    url.includes('/auth/login') ||
    url.includes('/auth/register') ||
    url.includes('/auth/refresh')
  ) {
    return config
  }
  const token = getAccessToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

http.interceptors.response.use(
  (res) => res,
  async (error: AxiosError<ApiEnvelope<unknown>>) => {
    const status = error.response?.status
    const original = error.config as InternalAxiosRequestConfig & { _retry?: boolean }
    const url = original?.url || ''
    if (
      status === 401 &&
      original &&
      !original._retry &&
      !url.includes('/auth/login') &&
      !url.includes('/auth/register') &&
      !url.includes('/auth/refresh')
    ) {
      original._retry = true
      const token = await ensureRefresh()
      if (token) {
        original.headers.Authorization = `Bearer ${token}`
        return http.request(original)
      }
      clearTokens()
      if (!window.location.pathname.startsWith('/login')) {
        window.location.href = `/login?redirect=${encodeURIComponent(window.location.pathname)}`
      }
    }
    return Promise.reject(error)
  },
)

export async function requestData<T>(promise: Promise<{ data: ApiEnvelope<T> }>): Promise<T> {
  try {
    const { data: body } = await promise
    if (body.code !== 200) {
      throw new ApiError(body.code, body.message || '请求失败')
    }
    return body.data as T
  } catch (err) {
    const ax = err as AxiosError<ApiEnvelope<unknown>>
    if (ax.response?.data) {
      const body = ax.response.data
      // 优先取顶层 message；如果 data 里有 message/detail，拼成更友好的文案。
      // 例如 Redis 限流时 data.message = "尝试过于频繁，请 300 秒后重试"
      const dataMessage =
        (body.data && typeof body.data === 'object' && 'message' in body.data && (body.data as any).message)
        || (body.data && typeof body.data === 'object' && 'detail' in body.data && (body.data as any).detail)
      const msg = dataMessage || body.message || '请求失败'
      throw new ApiError(body.code ?? ax.response.status, msg)
    }
    if (err instanceof ApiError) throw err
    throw new ApiError(0, ax.message || '网络错误')
  }
}
