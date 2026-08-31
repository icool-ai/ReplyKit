import { http, requestData } from './client'

export interface AgentItem {
  id: string
  name: string
  description: string
  icon: string
  category: string
  runtime: string
  enabled: boolean
  sort_order: number
  updated_at: number
}

export interface AgentListData {
  items: AgentItem[]
}

export function listEnabledAgents() {
  return requestData<AgentListData>(http.get('/agents'))
}

export function listAllAgents() {
  return requestData<AgentListData>(http.post('/agents/list', {}))
}

export function getAgent(agentId: string) {
  return requestData<AgentItem>(http.get(`/agents/${agentId}`))
}

export function updateAgent(
  agentId: string,
  body: Partial<
    Pick<AgentItem, 'name' | 'description' | 'icon' | 'category' | 'enabled' | 'sort_order'>
  >,
) {
  return requestData<AgentItem>(http.post(`/agents/${agentId}/update`, body))
}
