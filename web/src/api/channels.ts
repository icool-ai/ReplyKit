import { http, requestData } from './client'

export interface FeishuChannel {
  id: string | null
  owner_username: string
  channel: string
  enabled: boolean
  app_id: string
  app_secret_set: boolean
  verification_token_set: boolean
  encrypt_key_set: boolean
  callback_path: string | null
  created_at: number | null
  updated_at: number | null
}

export interface FeishuChannelUpdateBody {
  enabled: boolean
  app_id: string
  app_secret?: string
  verification_token?: string
  encrypt_key?: string
}

export function getFeishuChannel() {
  return requestData<FeishuChannel>(http.get('/channels/feishu'))
}

export function updateFeishuChannel(body: FeishuChannelUpdateBody) {
  return requestData<FeishuChannel>(http.post('/channels/feishu/update', body))
}
