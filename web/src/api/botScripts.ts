import { http, requestData } from './client'

export interface BotScripts {
  welcome: string
  no_answer: string
  sensitive_reply: string
  handoff_reply: string
  handoff_keywords: string[]
  chitchat_reply: string
  chitchat_phrases: string[]
}

export function getBotScripts() {
  return requestData<BotScripts>(http.get('/bot-scripts'))
}

export function getBotScriptsTemplate() {
  return requestData<BotScripts>(http.get('/bot-scripts/template'))
}

export function updateBotScripts(body: BotScripts) {
  return requestData<BotScripts>(http.post('/bot-scripts/update', body))
}

export function resetBotScriptsTemplate() {
  return requestData<BotScripts>(http.post('/bot-scripts/reset-template'))
}
