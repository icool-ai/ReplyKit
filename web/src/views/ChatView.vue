<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { listEnabledAgents, type AgentItem } from '@/api/agents'
import { getBotScripts } from '@/api/botScripts'
import { postChat } from '@/api/chat'
import { ApiError } from '@/api/client'
import {
  createCompetitorSession,
  postCompetitorMessage,
  streamCompetitorRun,
  competitorDownloadHref,
} from '@/api/competitor'
import { deleteSession, getSession, listSessions, type SessionSummary } from '@/api/sessions'
import { getAccessToken } from '@/auth/session'

interface Bubble {
  role: 'user' | 'assistant'
  content: string
  images?: string[]
  clarifyOptions?: string[]
  downloadUrl?: string
  downloadName?: string
}

const AGENT_KEY = 'replykit_active_agent'

const route = useRoute()
const router = useRouter()

const sessionId = ref<string | null>(null)
const input = ref('')
const loading = ref(false)
const messages = ref<Bubble[]>([])
const listRef = ref<HTMLElement | null>(null)
const history = ref<SessionSummary[]>([])
const historyLoading = ref(false)
const welcomeText = ref('')
const agents = ref<AgentItem[]>([])
const activeAgentId = ref('customer_service')
/** 视图世代：作废过期的欢迎语 / 打开会话回调，避免异步回写互相覆盖 */
let viewEpoch = 0

const isCompetitor = computed(() => activeAgentId.value === 'ecommerce_competitor')
const activeAgentName = computed(() => {
  const a = agents.value.find((x) => x.id === activeAgentId.value)
  return a?.name || (isCompetitor.value ? '电商竞品分析' : '智能客服')
})

async function scrollBottom() {
  await nextTick()
  const el = listRef.value
  if (el) el.scrollTop = el.scrollHeight
}

async function loadAgents() {
  try {
    const data = await listEnabledAgents()
    agents.value = data.items
    const fromQuery = typeof route.query.agent === 'string' ? route.query.agent : ''
    const fromStore = localStorage.getItem(AGENT_KEY) || ''
    const preferred = fromQuery || fromStore || 'customer_service'
    if (agents.value.some((a) => a.id === preferred)) {
      activeAgentId.value = preferred
    } else if (agents.value.length) {
      activeAgentId.value = agents.value[0].id
    }
    localStorage.setItem(AGENT_KEY, activeAgentId.value)
  } catch {
    agents.value = [
      {
        id: 'customer_service',
        name: '智能客服',
        description: '',
        icon: '',
        category: '',
        runtime: 'replykit_chat',
        enabled: true,
        sort_order: 10,
        updated_at: 0,
      },
    ]
  }
}

function onAgentChange(id: string) {
  activeAgentId.value = id
  localStorage.setItem(AGENT_KEY, id)
  void router.replace({ query: { ...route.query, agent: id } })
  void resetSession(false)
}

async function ensureWelcomeText(): Promise<string> {
  if (welcomeText.value) return welcomeText.value
  try {
    const data = await getBotScripts()
    welcomeText.value = (data.welcome || '').trim()
  } catch {
    welcomeText.value = ''
  }
  return welcomeText.value
}

async function showWelcome() {
  const epoch = ++viewEpoch
  if (isCompetitor.value) {
    if (epoch !== viewEpoch) return
    messages.value = [
      {
        role: 'assistant',
        content:
          '你好，我是电商竞品分析助手。可以说：帮我分析一下 doogee 在 Amazon 上的竞品，要 5 个',
      },
    ]
    await scrollBottom()
    return
  }
  const text = await ensureWelcomeText()
  if (epoch !== viewEpoch) return
  if (sessionId.value !== null) return
  if (messages.value.some((m) => m.role === 'user')) return
  messages.value = text ? [{ role: 'assistant', content: text }] : []
  await scrollBottom()
}

async function loadHistory() {
  if (isCompetitor.value) {
    history.value = []
    return
  }
  historyLoading.value = true
  try {
    const data = await listSessions(1, 50)
    history.value = data.items
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载历史失败')
  } finally {
    historyLoading.value = false
  }
}

async function openSession(item: SessionSummary) {
  if (isCompetitor.value) return
  const epoch = ++viewEpoch
  try {
    const detail = await getSession(item.session_id)
    if (epoch !== viewEpoch) return
    sessionId.value = detail.session_id
    messages.value = detail.messages.map((m) => ({
      role: m.role === 'user' ? 'user' : 'assistant',
      content: m.content,
    }))
    await scrollBottom()
  } catch (err) {
    if (epoch !== viewEpoch) return
    ElMessage.error(err instanceof ApiError ? err.message : '打开会话失败')
  }
}

async function removeSession(item: SessionSummary, e: Event) {
  e.stopPropagation()
  try {
    await ElMessageBox.confirm(`删除会话「${item.title}」？`, '确认删除', { type: 'warning' })
    await deleteSession(item.session_id)
    if (sessionId.value === item.session_id) await resetSession(false)
    await loadHistory()
    ElMessage.success('已删除')
  } catch (err) {
    if (err === 'cancel' || err === 'close') return
    ElMessage.error(err instanceof ApiError ? err.message : '删除失败')
  }
}

async function sendCustomer(message: string) {
  const data = await postChat(message, sessionId.value)
  sessionId.value = data.session_id
  messages.value.push({
    role: 'assistant',
    content: data.answer,
    images: data.images,
    clarifyOptions: data.clarify_options,
  })
  await loadHistory()
}

async function sendCompetitor(message: string) {
  if (!sessionId.value) {
    const created = await createCompetitorSession()
    sessionId.value = created.session_id
  }
  const sid = sessionId.value!
  const run = await postCompetitorMessage(sid, message)
  let assistantBuf = ''
  const pushOrUpdate = (text: string, extra?: Partial<Bubble>) => {
    const last = messages.value[messages.value.length - 1]
    if (last && last.role === 'assistant' && last.content.startsWith('…')) {
      last.content = text
      if (extra?.downloadUrl) last.downloadUrl = extra.downloadUrl
      if (extra?.downloadName) last.downloadName = extra.downloadName
    } else {
      messages.value.push({
        role: 'assistant',
        content: text,
        ...extra,
      })
    }
  }
  messages.value.push({ role: 'assistant', content: '…分析中' })
  await streamCompetitorRun(sid, run.run_id, (ev) => {
    if (ev.type === 'assistant' && typeof ev.message === 'string') {
      assistantBuf = ev.message
      pushOrUpdate(assistantBuf)
    } else if (ev.type === 'progress') {
      const msg = typeof ev.message === 'string' ? ev.message : '处理中…'
      pushOrUpdate(`…${msg}`)
    } else if (ev.type === 'artifact') {
      const summary = typeof ev.summary === 'string' ? ev.summary : '已生成分析结果'
      const filename = typeof ev.filename === 'string' ? ev.filename : ''
      const href = filename
        ? competitorDownloadHref(filename)
        : typeof ev.download_url === 'string'
          ? `/api${ev.download_url}`
          : undefined
      pushOrUpdate(summary, {
        downloadUrl: href,
        downloadName: filename || 'download.csv',
      })
    } else if (ev.type === 'error') {
      pushOrUpdate(`错误：${ev.message || '任务失败'}`)
    }
  })
  if (assistantBuf) pushOrUpdate(assistantBuf)
}

async function send(text?: string) {
  const message = (text ?? input.value).trim()
  if (!message || loading.value) return

  viewEpoch += 1
  messages.value.push({ role: 'user', content: message })
  if (!text) input.value = ''
  loading.value = true
  await scrollBottom()

  try {
    if (isCompetitor.value) await sendCompetitor(message)
    else await sendCustomer(message)
  } catch (err) {
    const msg = err instanceof ApiError ? err.message : '发送失败'
    ElMessage.error(msg)
    messages.value.push({ role: 'assistant', content: `错误：${msg}` })
  } finally {
    loading.value = false
    await scrollBottom()
  }
}

function onClarify(option: string) {
  void send(option)
}

async function resetSession(notify = true) {
  sessionId.value = null
  welcomeText.value = ''
  messages.value = []
  await showWelcome()
  if (notify) ElMessage.success('已开启新会话')
}

function downloadWithAuth(url: string, name: string) {
  const token = getAccessToken()
  void fetch(url, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
    .then(async (res) => {
      if (!res.ok) throw new Error('下载失败')
      const blob = await res.blob()
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = name
      a.click()
      URL.revokeObjectURL(a.href)
    })
    .catch(() => ElMessage.error('下载失败'))
}

watch(isCompetitor, () => {
  void loadHistory()
})

onMounted(async () => {
  await loadAgents()
  await loadHistory()
  await showWelcome()
})
</script>

<template>
  <div class="chat-layout">
    <aside v-if="!isCompetitor" class="history">
      <div class="history-head">
        <span>我的会话</span>
        <el-button size="small" @click="resetSession()">新建</el-button>
      </div>
      <div v-loading="historyLoading" class="history-list">
        <div v-if="!history.length" class="history-empty">暂无历史</div>
        <div
          v-for="item in history"
          :key="item.session_id"
          class="history-item"
          :class="{ active: item.session_id === sessionId }"
          @click="openSession(item)"
        >
          <div class="history-title">{{ item.title || '新会话' }}</div>
          <div class="history-preview">{{ item.preview || '—' }}</div>
          <el-button
            class="history-del"
            link
            type="danger"
            size="small"
            @click="removeSession(item, $event)"
          >
            删除
          </el-button>
        </div>
      </div>
    </aside>

    <div class="chat-page">
      <div class="toolbar">
        <div>
          <h2>对话 · {{ activeAgentName }}</h2>
          <p class="sub">
            session:
            <code>{{ sessionId || '（发送后自动生成）' }}</code>
          </p>
        </div>
        <div class="toolbar-right">
          <el-select
            :model-value="activeAgentId"
            style="width: 180px"
            @change="onAgentChange"
          >
            <el-option
              v-for="a in agents"
              :key="a.id"
              :label="a.name"
              :value="a.id"
            />
          </el-select>
          <el-button @click="resetSession()">新会话</el-button>
        </div>
      </div>

      <div ref="listRef" class="messages">
        <div v-if="!messages.length" class="empty">
          {{
            isCompetitor
              ? '输入竞品分析需求，例如「分析 Amazon 上 doogee 竞品 5 个」'
              : '输入问题开始对话，例如「退货几天」'
          }}
        </div>
        <div v-for="(m, i) in messages" :key="i" class="row" :class="m.role">
          <div class="bubble">
            <div class="text">{{ m.content }}</div>
            <div v-if="m.downloadUrl" class="dl">
              <el-button
                size="small"
                type="primary"
                link
                @click="downloadWithAuth(m.downloadUrl!, m.downloadName || 'export.csv')"
              >
                下载 CSV{{ m.downloadName ? `（${m.downloadName}）` : '' }}
              </el-button>
            </div>
            <div v-if="m.images?.length" class="images">
              <a
                v-for="(url, j) in m.images"
                :key="j"
                :href="url"
                target="_blank"
                rel="noreferrer"
              >
                <img :src="url" alt="配图" />
              </a>
            </div>
            <div v-if="m.clarifyOptions?.length" class="clarify">
              <el-button
                v-for="(opt, j) in m.clarifyOptions"
                :key="j"
                size="small"
                @click="onClarify(opt)"
              >
                {{ opt }}
              </el-button>
            </div>
          </div>
        </div>
      </div>

      <div class="composer">
        <el-input
          v-model="input"
          type="textarea"
          :rows="2"
          :placeholder="
            isCompetitor
              ? '输入竞品分析需求，Enter 发送'
              : '输入消息，Enter 发送（Shift+Enter 换行）'
          "
          :disabled="loading"
          @keydown.enter.exact.prevent="send()"
        />
        <el-button type="primary" :loading="loading" @click="send()">发送</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat-layout {
  display: flex;
  gap: 12px;
  height: calc(100vh - 40px);
}

.history {
  width: 240px;
  flex-shrink: 0;
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.history-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px;
  border-bottom: 1px solid #ebeef5;
  font-weight: 600;
}

.history-list {
  flex: 1;
  overflow: auto;
}

.history-empty {
  padding: 24px 12px;
  text-align: center;
  color: #909399;
  font-size: 13px;
}

.history-item {
  position: relative;
  padding: 10px 12px 28px;
  border-bottom: 1px solid #f2f3f5;
  cursor: pointer;
}

.history-item:hover,
.history-item.active {
  background: #f5f7fa;
}

.history-title {
  font-size: 13px;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.history-preview {
  margin-top: 4px;
  font-size: 12px;
  color: #909399;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.history-del {
  position: absolute;
  right: 8px;
  bottom: 4px;
}

.chat-page {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  overflow: hidden;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid #ebeef5;
  gap: 12px;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.toolbar h2 {
  margin: 0;
  font-size: 18px;
}

.sub {
  margin: 4px 0 0;
  font-size: 12px;
  color: #909399;
}

.messages {
  flex: 1;
  overflow: auto;
  padding: 16px 20px;
  background: #fafafa;
}

.empty {
  color: #909399;
  text-align: center;
  margin-top: 48px;
}

.row {
  display: flex;
  margin-bottom: 12px;
}

.row.user {
  justify-content: flex-end;
}

.bubble {
  max-width: 80%;
  padding: 10px 14px;
  border-radius: 10px;
  background: #fff;
  border: 1px solid #ebeef5;
  white-space: pre-wrap;
  word-break: break-word;
}

.row.user .bubble {
  background: #ecf5ff;
  border-color: #d9ecff;
}

.dl {
  margin-top: 8px;
}

.images {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
}

.images img {
  max-width: 180px;
  max-height: 120px;
  border-radius: 4px;
  object-fit: cover;
}

.clarify {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}

.composer {
  display: flex;
  gap: 12px;
  align-items: flex-end;
  padding: 12px 16px;
  border-top: 1px solid #ebeef5;
}

.composer .el-textarea {
  flex: 1;
}
</style>
