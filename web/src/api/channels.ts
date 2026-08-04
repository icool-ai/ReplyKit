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

export interface DifyApiKeyItem {
  id: string
  name: string
  endpoint: string
  knowledge_id: string
  api_key_masked: string
  api_key_set: boolean
  created_at: number
  updated_at: number
  last_used_at: number | null
  api_key?: string | null
}

export interface DifyApiKeyListData {
  items: DifyApiKeyItem[]
  retrieval_path: string
}

export interface DifyApiKeyCreateBody {
  name: string
  endpoint: string
  knowledge_id?: string
}

export interface DifyApiKeyUpdateBody {
  name?: string
  endpoint?: string
  knowledge_id?: string
}

export function getFeishuChannel() {
  return requestData<FeishuChannel>(http.get('/channels/feishu'))
}

export function updateFeishuChannel(body: FeishuChannelUpdateBody) {
  return requestData<FeishuChannel>(http.post('/channels/feishu/update', body))
}

export function listDifyApiKeys() {
  return requestData<DifyApiKeyListData>(http.get('/integrations/dify/keys'))
}

export function createDifyApiKey(body: DifyApiKeyCreateBody) {
  return requestData<DifyApiKeyItem>(http.post('/integrations/dify/keys', body))
}

export function updateDifyApiKey(id: string, body: DifyApiKeyUpdateBody) {
  return requestData<DifyApiKeyItem>(
    http.post(`/integrations/dify/keys/${encodeURIComponent(id)}/update`, body),
  )
}

export function deleteDifyApiKey(id: string) {
  return requestData<boolean>(
    http.post(`/integrations/dify/keys/${encodeURIComponent(id)}/delete`),
  )
}
