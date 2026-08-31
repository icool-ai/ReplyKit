import { getUsername } from '@/auth/session'

const TOKEN = import.meta.env.VITE_DIFY_TOKEN || 'MQP3zXx7K0njl93v'
const BASE_URL = import.meta.env.VITE_DIFY_BASE_URL || 'https://udify.app'
const SCRIPT_SRC = `${BASE_URL.replace(/\/$/, '')}/embed.min.js`

declare global {
  interface Window {
    difyChatbotConfig?: {
      token: string
      baseUrl: string
      /** 动态插入脚本时必须为 true，否则会挂在已触发过的 body.onload 上，气泡永不出现 */
      dynamicScript?: boolean
      inputs?: Record<string, string>
      systemVariables?: {
        user_id?: string
        conversation_id?: string
      }
      userVariables?: {
        avatar_url?: string
        name?: string
      }
    }
  }
}

/** 防止退出后异步脚本仍注入气泡 */
let mountEpoch = 0

function removeBubbleDom() {
  document.getElementById('dify-chatbot-bubble-button')?.remove()
  document.getElementById('dify-chatbot-bubble-window')?.remove()
}

function removeEmbedScript() {
  document.getElementById(TOKEN)?.remove()
}

/** 登录后加载 Dify 气泡；重复调用会先清掉再重建。 */
export function mountDifyChatbot() {
  const epoch = ++mountEpoch
  const username = getUsername()
  window.difyChatbotConfig = {
    token: TOKEN,
    baseUrl: BASE_URL,
    dynamicScript: true,
    inputs: {},
    systemVariables: {
      ...(username ? { user_id: username } : {}),
    },
    userVariables: {
      ...(username ? { name: username } : {}),
    },
  }

  removeBubbleDom()
  removeEmbedScript()

  const script = document.createElement('script')
  script.src = SCRIPT_SRC
  script.id = TOKEN
  script.async = true
  script.addEventListener('load', () => {
    if (epoch !== mountEpoch) {
      removeBubbleDom()
      removeEmbedScript()
    }
  })
  document.body.appendChild(script)
}

/** 退出登录时移除气泡与脚本。 */
export function unmountDifyChatbot() {
  mountEpoch += 1
  removeBubbleDom()
  removeEmbedScript()
  delete window.difyChatbotConfig
}
