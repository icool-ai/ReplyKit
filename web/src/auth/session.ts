export type UserRole = 'user' | 'ops'

export interface TokenData {
  access_token: string
  token_type: string
  expires_in: number
  refresh_token: string
  refresh_expires_in: number
  username?: string
  role?: UserRole
}

const ACCESS_KEY = 'ca_access_token'
const REFRESH_KEY = 'ca_refresh_token'
const USERNAME_KEY = 'ca_username'
const ROLE_KEY = 'ca_role'

function decodeJwtPayload(token: string): Record<string, unknown> | null {
  try {
    const part = token.split('.')[1]
    if (!part) return null
    const json = atob(part.replace(/-/g, '+').replace(/_/g, '/'))
    return JSON.parse(json) as Record<string, unknown>
  } catch {
    return null
  }
}

export function getAccessToken(): string | null {
  return sessionStorage.getItem(ACCESS_KEY)
}

export function getRefreshToken(): string | null {
  return sessionStorage.getItem(REFRESH_KEY)
}

export function getUsername(): string {
  return sessionStorage.getItem(USERNAME_KEY) || ''
}

export function getRole(): UserRole {
  const role = sessionStorage.getItem(ROLE_KEY)
  return role === 'ops' ? 'ops' : 'user'
}

export function isOps(): boolean {
  return getRole() === 'ops'
}

export function setTokens(data: TokenData): void {
  sessionStorage.setItem(ACCESS_KEY, data.access_token)
  sessionStorage.setItem(REFRESH_KEY, data.refresh_token)
  let username = data.username || ''
  let role: UserRole = data.role === 'ops' ? 'ops' : 'user'
  if (!username || !data.role) {
    const payload = decodeJwtPayload(data.access_token)
    if (payload) {
      if (!username && typeof payload.sub === 'string') username = payload.sub
      if (payload.role === 'ops') role = 'ops'
    }
  }
  sessionStorage.setItem(USERNAME_KEY, username)
  sessionStorage.setItem(ROLE_KEY, role)
}

export function clearTokens(): void {
  sessionStorage.removeItem(ACCESS_KEY)
  sessionStorage.removeItem(REFRESH_KEY)
  sessionStorage.removeItem(USERNAME_KEY)
  sessionStorage.removeItem(ROLE_KEY)
}

export function isLoggedIn(): boolean {
  return Boolean(getAccessToken())
}
