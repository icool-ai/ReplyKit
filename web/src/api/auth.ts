import { http, requestData } from './client'
import type { TokenData } from '@/auth/session'

export function login(username: string, password: string) {
  return requestData<TokenData>(http.post('/auth/login', { username, password }))
}

export function register(username: string, password: string) {
  return requestData<TokenData>(
    http.post('/auth/register', { username, password }),
  )
}

export function refresh(refreshToken: string) {
  return requestData<TokenData>(
    http.post('/auth/refresh', { refresh_token: refreshToken }),
  )
}

export function logout(refreshToken: string) {
  return requestData<boolean>(http.post('/auth/logout', { refresh_token: refreshToken }))
}
