import { http, requestData } from './client'
import type { UserRole } from '@/auth/session'

export interface UserItem {
  username: string
  role: UserRole
  enabled: boolean
  created_at: number
}

export interface UserListData {
  items: UserItem[]
  total: number
  page: number
  page_size: number
  total_pages: number
}

export function listUsers(body: {
  page?: number
  page_size?: number
  keyword?: string
  role?: string
  enabled?: boolean | null
}) {
  return requestData<UserListData>(http.post('/users/list', body))
}

export function createUser(body: { username: string; password: string; role: UserRole }) {
  return requestData<UserItem>(http.post('/users', body))
}

export function updateUser(body: {
  username: string
  role?: UserRole
  enabled?: boolean
  password?: string
}) {
  return requestData<UserItem>(http.post('/users/update', body))
}

export function resetUserPassword(body: { username: string; new_password: string }) {
  return requestData<UserItem>(http.post('/users/reset-password', body))
}
