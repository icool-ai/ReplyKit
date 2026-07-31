<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import {
  getFeishuChannel,
  updateFeishuChannel,
  type FeishuChannel,
  type FeishuChannelUpdateBody,
} from '@/api/channels'
import { ApiError } from '@/api/client'

const loading = ref(false)
const saving = ref(false)
const formRef = ref<FormInstance>()
const meta = ref<FeishuChannel | null>(null)

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

onMounted(() => {
  void loadCurrent()
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
            <div class="field-hint">
              开发者后台 → 企业自建应用 →「凭证与基础信息」→ 应用凭证中的 App ID。
              若要释放给他人绑定，请清空 App ID 后保存。
            </div>
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
            <div class="field-hint">
              与 App ID 同一页；启用保存时会向飞书校验是否有效。
            </div>
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
            <div class="field-hint">
              「事件与回调」→「加密策略」中的 Verification Token，用于校验事件来源。
            </div>
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
            <div class="field-hint">
              同在「加密策略」；建议开启。后台开启后本页必须填写相同 Key。
            </div>
          </el-form-item>
          <el-form-item label="回调地址">
            <el-input model-value="/webhooks/feishu" readonly />
            <div class="field-hint">
              飞书「请求地址」填：
              <code>https://&lt;公网域名&gt;/webhooks/feishu</code>
              （须 HTTPS，ngrok 指到主 API）。服务端用你填写的 App ID / Token
              自动匹配配置，不必再带 config_id。
            </div>
          </el-form-item>
        </el-form>
      </el-tab-pane>
      <el-tab-pane label="企业微信" name="wecom" disabled>
        <p class="hint">即将支持</p>
      </el-tab-pane>
    </el-tabs>
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
  max-width: 560px;
  line-height: 1.5;
}
.sub code,
.field-hint code,
.guide-list code {
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
.guide-list a:hover {
  text-decoration: underline;
}
.form {
  max-width: 720px;
}
.field-hint {
  margin-top: 4px;
  color: #909399;
  font-size: 12px;
  line-height: 1.45;
}
.hint {
  margin-top: 16px;
  color: #909399;
  font-size: 12px;
  line-height: 1.5;
}
</style>
