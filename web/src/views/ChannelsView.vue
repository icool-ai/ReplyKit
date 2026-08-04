<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { Delete, Edit } from '@element-plus/icons-vue'
import {
  createDifyApiKey,
  deleteDifyApiKey,
  getFeishuChannel,
  listDifyApiKeys,
  updateDifyApiKey,
  updateFeishuChannel,
  type DifyApiKeyItem,
  type FeishuChannel,
  type FeishuChannelUpdateBody,
} from '@/api/channels'
import { ApiError } from '@/api/client'
import { isOps } from '@/auth/session'

const loading = ref(false)
const saving = ref(false)
const formRef = ref<FormInstance>()
const meta = ref<FeishuChannel | null>(null)

const showDify = computed(() => isOps())
const difyLoading = ref(false)
const difySaving = ref(false)
const difyItems = ref<DifyApiKeyItem[]>([])
const retrievalPath = ref('/retrieval')

const createVisible = ref(false)
const createFormRef = ref<FormInstance>()
const createForm = reactive({
  name: '',
  endpoint: '',
  knowledge_id: 'faq',
})
const createRules: FormRules = {
  name: [{ required: true, message: '请输入名称', trigger: 'blur' }],
  endpoint: [{ required: true, message: '请输入 API Endpoint', trigger: 'blur' }],
  knowledge_id: [{ required: true, message: '请输入外部知识库 ID', trigger: 'blur' }],
}

const editVisible = ref(false)
const editFormRef = ref<FormInstance>()
const editId = ref('')
const editForm = reactive({
  name: '',
  endpoint: '',
  knowledge_id: '',
})

const generatedKeyVisible = ref(false)
const generatedKey = ref('')

const form = reactive({
  enabled: false,
  app_id: '',
  app_secret: '',
  verification_token: '',
  encrypt_key: '',
})

const rules: FormRules = {
  app_id: [
    {
      validator: (_r, v, cb) => {
        if (form.enabled && !String(v || '').trim()) {
          cb(new Error('启用时须填写 App ID'))
          return
        }
        cb()
      },
      trigger: 'blur',
    },
  ],
}

function applyMeta(data: FeishuChannel) {
  meta.value = data
  form.enabled = data.enabled
  form.app_id = data.app_id || ''
  form.app_secret = ''
  form.verification_token = ''
  form.encrypt_key = ''
}

function formatDate(ts: number | null | undefined) {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  if (Number.isNaN(d.getTime())) return '—'
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

async function loadCurrent() {
  loading.value = true
  try {
    applyMeta(await getFeishuChannel())
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载失败')
  } finally {
    loading.value = false
  }
}

async function loadDify() {
  if (!showDify.value) return
  difyLoading.value = true
  try {
    const data = await listDifyApiKeys()
    difyItems.value = data.items || []
    retrievalPath.value = data.retrieval_path || '/retrieval'
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载 Dify 配置失败')
  } finally {
    difyLoading.value = false
  }
}

async function onSave() {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return
  saving.value = true
  try {
    const body: FeishuChannelUpdateBody = {
      enabled: form.enabled,
      app_id: form.app_id.trim(),
    }
    if (form.app_secret.trim()) body.app_secret = form.app_secret.trim()
    if (form.verification_token.trim()) {
      body.verification_token = form.verification_token.trim()
    }
    if (form.encrypt_key.trim()) body.encrypt_key = form.encrypt_key.trim()
    applyMeta(await updateFeishuChannel(body))
    ElMessage.success('已保存')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '保存失败')
  } finally {
    saving.value = false
  }
}

function openCreate() {
  createForm.name = ''
  createForm.endpoint = ''
  createForm.knowledge_id = 'faq'
  createVisible.value = true
}

async function onCreateSubmit() {
  const ok = await createFormRef.value?.validate().catch(() => false)
  if (!ok) return
  difySaving.value = true
  try {
    const created = await createDifyApiKey({
      name: createForm.name.trim(),
      endpoint: createForm.endpoint.trim(),
      knowledge_id: createForm.knowledge_id.trim() || 'faq',
    })
    createVisible.value = false
    await loadDify()
    const key = (created.api_key || '').trim()
    if (!key) {
      ElMessage.error('未返回密钥，请重试')
      return
    }
    generatedKey.value = key
    generatedKeyVisible.value = true
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '生成失败')
  } finally {
    difySaving.value = false
  }
}

function openEdit(row: DifyApiKeyItem) {
  editId.value = row.id
  editForm.name = row.name
  editForm.endpoint = row.endpoint
  editForm.knowledge_id = row.knowledge_id
  editVisible.value = true
}

async function onEditSubmit() {
  const ok = await editFormRef.value?.validate().catch(() => false)
  if (!ok) return
  difySaving.value = true
  try {
    await updateDifyApiKey(editId.value, {
      name: editForm.name.trim(),
      endpoint: editForm.endpoint.trim(),
      knowledge_id: editForm.knowledge_id.trim(),
    })
    editVisible.value = false
    ElMessage.success('已更新')
    await loadDify()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '更新失败')
  } finally {
    difySaving.value = false
  }
}

async function onDelete(row: DifyApiKeyItem) {
  try {
    await ElMessageBox.confirm(
      `删除「${row.name}」后，使用该 Key 的 Dify 将无法检索。确认？`,
      '删除配置',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  difySaving.value = true
  try {
    await deleteDifyApiKey(row.id)
    ElMessage.success('已删除')
    await loadDify()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '删除失败')
  } finally {
    difySaving.value = false
  }
}

async function copyGeneratedKey() {
  const key = generatedKey.value
  if (!key) return
  try {
    await navigator.clipboard.writeText(key)
    ElMessage.success('已复制到剪贴板')
  } catch {
    ElMessage.warning('复制失败，请手动选中复制')
  }
}

onMounted(() => {
  void loadCurrent()
  void loadDify()
})
</script>

<template>
  <div v-loading="loading" class="page">
    <el-tabs>
      <el-tab-pane label="飞书" name="feishu">
        <div class="toolbar">
          <div>
            <h2>飞书渠道</h2>
            <p class="sub">
              同一个 App ID 仅允许绑定人修改。飞书请求地址固定为
              <code>/webhooks/feishu</code>
              。未启用或凭证不全时，机器人会在会话里提示管理员来本页处理。
            </p>
          </div>
          <div class="actions">
            <el-button @click="loadCurrent">重新加载</el-button>
            <el-button type="primary" :loading="saving" @click="onSave">保存</el-button>
          </div>
        </div>

        <el-collapse class="guide">
          <el-collapse-item title="如何获取 App ID / Secret / Token / Encrypt Key" name="guide">
            <ol class="guide-list">
              <li>
                打开
                <a href="https://open.feishu.cn/app" target="_blank" rel="noopener noreferrer"
                  >飞书开发者后台</a
                >
                ，使用企业管理员或开发者账号登录。
              </li>
              <li>
                若还没有应用：点击「创建企业自建应用」，填写应用名称与描述后创建（商店应用不适用于本接入方式）。
              </li>
              <li>
                进入该应用详情 → 左侧「凭证与基础信息」：
                <ul>
                  <li><strong>App ID</strong>：形如 <code>cli_xxx</code>，可直接复制。</li>
                  <li>
                    <strong>App Secret</strong>：同页「应用凭证」中，点击查看/复制；泄露后请在后台重置。
                  </li>
                </ul>
              </li>
              <li>
                左侧「事件与回调」（部分界面也称「事件订阅」）→「加密策略」页签：
                <ul>
                  <li>
                    <strong>Verification Token</strong>：平台自动生成，用于校验推送是否来自飞书，复制到本页即可。
                  </li>
                  <li>
                    <strong>Encrypt Key</strong>：用于事件加密与签名校验。建议点击「重置/编辑」开启并填写到本页；开启后飞书后台与本页都必须配置同一把密钥，否则收不到/解不开事件。
                  </li>
                </ul>
              </li>
              <li>
                本页保存并启用后，飞书「请求地址」填固定地址：
                <code>https://&lt;公网域名&gt;/webhooks/feishu</code>
                （指向主 API，例如 ngrok 转到
                <code>:8000</code>）。保存时飞书会发
                <code>challenge</code> 校验。
              </li>
              <li>
                添加事件：至少订阅 <code>接收消息</code>（<code>im.message.receive_v1</code>）；并在「权限管理」开通发消息等相关权限，发布应用版本并完成企业审核后，机器人才能在会话中回复。
              </li>
            </ol>
          </el-collapse-item>
        </el-collapse>

        <el-form
          ref="formRef"
          :model="form"
          :rules="rules"
          label-width="140px"
          class="form"
        >
          <el-form-item label="启用">
            <el-switch v-model="form.enabled" />
          </el-form-item>
          <el-form-item label="App ID" prop="app_id">
            <el-input v-model="form.app_id" placeholder="cli_xxxxxxxx" clearable />
          </el-form-item>
          <el-form-item label="App Secret">
            <el-input
              v-model="form.app_secret"
              type="password"
              show-password
              :placeholder="
                meta?.app_secret_set ? '已配置，留空则不修改' : '飞书应用 Secret'
              "
            />
          </el-form-item>
          <el-form-item label="Verification Token">
            <el-input
              v-model="form.verification_token"
              type="password"
              show-password
              :placeholder="
                meta?.verification_token_set
                  ? '已配置，留空则不修改'
                  : '事件与回调 → 加密策略'
              "
            />
          </el-form-item>
          <el-form-item label="Encrypt Key">
            <el-input
              v-model="form.encrypt_key"
              type="password"
              show-password
              :placeholder="
                meta?.encrypt_key_set
                  ? '已配置，留空则不修改'
                  : '建议开启；开启后必填'
              "
            />
          </el-form-item>
          <el-form-item label="回调地址">
            <el-input model-value="/webhooks/feishu" readonly />
          </el-form-item>
        </el-form>
      </el-tab-pane>

      <el-tab-pane v-if="showDify" label="Dify" name="dify">
        <div v-loading="difyLoading">
          <div class="toolbar">
            <div>
              <h2>Dify 外部知识库</h2>
              <p class="sub">
                可创建多把 API Key。新建时填写名称、Endpoint、知识库 ID，生成后明文仅展示一次。
                Dify 请求路径为
                <code>{{ retrievalPath }}</code>
                （Endpoint 填公网根地址，不要带该路径）。本期检索均走本服务 FAQ。
              </p>
            </div>
            <div class="actions">
              <el-button @click="loadDify">重新加载</el-button>
              <el-button type="primary" @click="openCreate">新建</el-button>
            </div>
          </div>

          <el-table :data="difyItems" stripe empty-text="暂无配置，点击右上角新建">
            <el-table-column prop="name" label="名称" min-width="120" />
            <el-table-column prop="api_key_masked" label="Key" min-width="200">
              <template #default="{ row }">
                <code class="key-mask">{{ row.api_key_masked }}</code>
              </template>
            </el-table-column>
            <el-table-column prop="endpoint" label="API Endpoint" min-width="200" show-overflow-tooltip />
            <el-table-column prop="knowledge_id" label="知识库 ID" width="120" />
            <el-table-column label="创建日期" width="120">
              <template #default="{ row }">
                {{ formatDate(row.created_at) }}
              </template>
            </el-table-column>
            <el-table-column label="最新使用" width="120">
              <template #default="{ row }">
                {{ formatDate(row.last_used_at) }}
              </template>
            </el-table-column>
            <el-table-column label="操作" width="100" fixed="right">
              <template #default="{ row }">
                <el-button link type="primary" :icon="Edit" @click="openEdit(row)" />
                <el-button
                  link
                  type="danger"
                  :icon="Delete"
                  :loading="difySaving"
                  @click="onDelete(row)"
                />
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane label="企业微信" name="wecom" disabled>
        <p class="hint">即将支持</p>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="createVisible" title="新建 Dify 配置" width="520px" destroy-on-close>
      <el-form
        ref="createFormRef"
        :model="createForm"
        :rules="createRules"
        label-width="130px"
      >
        <el-form-item label="名称" prop="name">
          <el-input v-model="createForm.name" placeholder="如 dify-生产" maxlength="64" />
        </el-form-item>
        <el-form-item label="API Endpoint" prop="endpoint">
          <el-input
            v-model="createForm.endpoint"
            placeholder="https://xxxx.ngrok-free.app"
          />
          <div class="field-hint">填到 Dify 的公网根地址，不要带 /retrieval</div>
        </el-form-item>
        <el-form-item label="外部知识库 ID" prop="knowledge_id">
          <el-input v-model="createForm.knowledge_id" placeholder="faq" />
          <div class="field-hint">须与 Dify「连接外部知识库」时填写的 ID 一致</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="difySaving" @click="onCreateSubmit">
          生成
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="editVisible" title="编辑配置" width="520px" destroy-on-close>
      <el-form
        ref="editFormRef"
        :model="editForm"
        :rules="createRules"
        label-width="130px"
      >
        <el-form-item label="名称" prop="name">
          <el-input v-model="editForm.name" maxlength="64" />
        </el-form-item>
        <el-form-item label="API Endpoint" prop="endpoint">
          <el-input v-model="editForm.endpoint" />
        </el-form-item>
        <el-form-item label="外部知识库 ID" prop="knowledge_id">
          <el-input v-model="editForm.knowledge_id" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="difySaving" @click="onEditSubmit">
          保存
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="generatedKeyVisible"
      title="API Key 已生成"
      width="520px"
      :close-on-click-modal="false"
    >
      <p class="dialog-tip">
        请立即复制并填到 Dify 的 API Key。关闭后无法再次查看完整密钥；需要新 Key 请再建一条。
      </p>
      <el-input :model-value="generatedKey" type="textarea" :rows="3" readonly />
      <template #footer>
        <el-button @click="generatedKeyVisible = false">关闭</el-button>
        <el-button type="primary" @click="copyGeneratedKey">复制</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page {
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 16px 20px 24px;
}
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
h2 {
  margin: 0 0 4px;
  font-size: 18px;
}
.sub {
  margin: 0;
  color: #909399;
  font-size: 13px;
  max-width: 640px;
  line-height: 1.5;
}
.sub code,
.field-hint code,
.guide-list code,
.key-mask {
  font-size: 12px;
  background: #f5f7fa;
  padding: 1px 6px;
  border-radius: 4px;
}
.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.guide {
  margin-bottom: 16px;
  border: 1px solid #ebeef5;
  border-radius: 6px;
  --el-collapse-header-height: 44px;
}
.guide :deep(.el-collapse-item__header) {
  padding: 0 14px;
  font-size: 13px;
  color: #606266;
}
.guide :deep(.el-collapse-item__wrap) {
  border-top: 1px solid #ebeef5;
}
.guide :deep(.el-collapse-item__content) {
  padding: 12px 16px 16px;
}
.guide-list {
  margin: 0;
  padding-left: 1.25em;
  color: #606266;
  font-size: 13px;
  line-height: 1.7;
}
.guide-list li + li {
  margin-top: 8px;
}
.guide-list ul {
  margin: 4px 0 0;
  padding-left: 1.2em;
}
.guide-list a {
  color: #409eff;
  text-decoration: none;
}
.form {
  max-width: 720px;
}
.field-hint {
  margin-top: 4px;
  color: #909399;
  font-size: 12px;
  line-height: 1.45;
  width: 100%;
}
.hint {
  margin-top: 16px;
  color: #909399;
  font-size: 12px;
  line-height: 1.5;
}
.dialog-tip {
  margin: 0 0 12px;
  color: #606266;
  font-size: 13px;
  line-height: 1.5;
}
</style>
