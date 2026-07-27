<script setup lang="ts">
import { nextTick, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { postChat } from '@/api/chat'
import { ApiError } from '@/api/client'
import { deleteSession, getSession, listSessions, type SessionSummary } from '@/api/sessions'

interface Bubble {
  role: 'user' | 'assistant'
  content: string
  images?: string[]
  clarifyOptions?: string[]
}

const sessionId = ref<string | null>(null)
const input = ref('')
const loading = ref(false)
const messages = ref<Bubble[]>([])
const listRef = ref<HTMLElement | null>(null)
const history = ref<SessionSummary[]>([])
const historyLoading = ref(false)

async function scrollBottom() {
  await nextTick()
  const el = listRef.value
  if (el) el.scrollTop = el.scrollHeight
}

async function loadHistory() {
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
  try {
    const detail = await getSession(item.session_id)
    sessionId.value = detail.session_id
    messages.value = detail.messages.map((m) => ({
      role: m.role === 'user' ? 'user' : 'assistant',
      content: m.content,
    }))
    await scrollBottom()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '打开会话失败')
  }
}

async function removeSession(item: SessionSummary, e: Event) {
  e.stopPropagation()
  try {
    await ElMessageBox.confirm(`删除会话「${item.title}」？`, '确认删除', { type: 'warning' })
    await deleteSession(item.session_id)
    if (sessionId.value === item.session_id) resetSession(false)
    await loadHistory()
    ElMessage.success('已删除')
  } catch (err) {
    if (err === 'cancel' || err === 'close') return
    ElMessage.error(err instanceof ApiError ? err.message : '删除失败')
  }
}

async function send(text?: string) {
  const message = (text ?? input.value).trim()
  if (!message || loading.value) return

  messages.value.push({ role: 'user', content: message })
  if (!text) input.value = ''
  loading.value = true
  await scrollBottom()

  try {
    const data = await postChat(message, sessionId.value)
    sessionId.value = data.session_id
    messages.value.push({
      role: 'assistant',
      content: data.answer,
      images: data.images,
      clarifyOptions: data.clarify_options,
    })
    await loadHistory()
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

function resetSession(notify = true) {
  sessionId.value = null
  messages.value = []
  if (notify) ElMessage.success('已开启新会话')
}

onMounted(() => {
  void loadHistory()
})
</script>

<template>
  <div class="chat-layout">
    <aside class="history">
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
          <h2>对话</h2>
          <p class="sub">
            session:
            <code>{{ sessionId || '（发送后自动生成）' }}</code>
          </p>
        </div>
        <el-button @click="resetSession()">新会话</el-button>
      </div>

      <div ref="listRef" class="messages">
        <div v-if="!messages.length" class="empty">输入问题开始对话，例如「退货几天」</div>
        <div v-for="(m, i) in messages" :key="i" class="row" :class="m.role">
          <div class="bubble">
            <div class="text">{{ m.content }}</div>
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
          placeholder="输入消息，Enter 发送（Shift+Enter 换行）"
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
