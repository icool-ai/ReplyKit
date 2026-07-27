<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import {
  getBotScripts,
  getBotScriptsTemplate,
  resetBotScriptsTemplate,
  updateBotScripts,
  type BotScripts,
} from '@/api/botScripts'
import { ApiError } from '@/api/client'

const loading = ref(false)
const saving = ref(false)
const formRef = ref<FormInstance>()

/** 关键词在表单里用多行文本编辑，每行一个；保存时再拆成数组 */
const form = reactive({
  welcome: '',
  no_answer: '',
  sensitive_reply: '',
  handoff_reply: '',
  handoff_keywords_text: '',
  chitchat_reply: '',
  chitchat_phrases_text: '',
})

const rules: FormRules = {
  welcome: [{ required: true, message: '请填写欢迎语', trigger: 'blur' }],
  no_answer: [{ required: true, message: '请填写无答案话术', trigger: 'blur' }],
  sensitive_reply: [{ required: true, message: '请填写敏感词回复', trigger: 'blur' }],
  handoff_reply: [{ required: true, message: '请填写转人工话术', trigger: 'blur' }],
  chitchat_reply: [{ required: true, message: '请填写闲聊回复', trigger: 'blur' }],
}

function listToText(items: string[] | undefined) {
  return (items || []).join('\n')
}

function textToList(text: string) {
  const seen = new Set<string>()
  const out: string[] = []
  for (const line of text.split(/[\n,，;；]+/)) {
    const item = line.trim()
    if (!item || seen.has(item)) continue
    seen.add(item)
    out.push(item)
  }
  return out
}

function applyScripts(data: BotScripts) {
  form.welcome = data.welcome
  form.no_answer = data.no_answer
  form.sensitive_reply = data.sensitive_reply
  form.handoff_reply = data.handoff_reply
  form.handoff_keywords_text = listToText(data.handoff_keywords)
  form.chitchat_reply = data.chitchat_reply
  form.chitchat_phrases_text = listToText(data.chitchat_phrases)
}

async function loadCurrent() {
  loading.value = true
  try {
    applyScripts(await getBotScripts())
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载失败')
  } finally {
    loading.value = false
  }
}

async function loadTemplateToForm() {
  loading.value = true
  try {
    applyScripts(await getBotScriptsTemplate())
    ElMessage.success('已加载模板到表单（尚未保存）')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载模板失败')
  } finally {
    loading.value = false
  }
}

async function onSave() {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return
  saving.value = true
  try {
    applyScripts(
      await updateBotScripts({
        welcome: form.welcome.trim(),
        no_answer: form.no_answer.trim(),
        sensitive_reply: form.sensitive_reply.trim(),
        handoff_reply: form.handoff_reply.trim(),
        handoff_keywords: textToList(form.handoff_keywords_text),
        chitchat_reply: form.chitchat_reply.trim(),
        chitchat_phrases: textToList(form.chitchat_phrases_text),
      }),
    )
    ElMessage.success('已保存（热更新，无需重启）')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '保存失败')
  } finally {
    saving.value = false
  }
}

async function onResetTemplate() {
  try {
    await ElMessageBox.confirm(
      '将用内置模板覆盖当前话术并立即保存，是否继续？',
      '一键初始化',
      { type: 'warning', confirmButtonText: '确认初始化', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  saving.value = true
  try {
    applyScripts(await resetBotScriptsTemplate())
    ElMessage.success('已用模板初始化并保存')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '初始化失败')
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
    <div class="toolbar">
      <div>
        <h2>话术配置</h2>
        <p class="sub">欢迎语、无答案、敏感回复、转人工 / 闲聊关键词与回复；保存后热更新</p>
      </div>
      <div class="actions">
        <el-button @click="loadCurrent">重新加载</el-button>
        <el-button @click="loadTemplateToForm">加载模板到表单</el-button>
        <el-button type="warning" plain @click="onResetTemplate">一键恢复模板并保存</el-button>
        <el-button type="primary" :loading="saving" @click="onSave">保存</el-button>
      </div>
    </div>

    <el-form ref="formRef" :model="form" :rules="rules" label-width="120px" class="form">
      <el-form-item label="欢迎语" prop="welcome">
        <el-input v-model="form.welcome" type="textarea" :rows="2" />
      </el-form-item>
      <el-form-item label="无答案话术" prop="no_answer">
        <el-input v-model="form.no_answer" type="textarea" :rows="2" />
      </el-form-item>
      <el-form-item label="敏感词回复" prop="sensitive_reply">
        <el-input v-model="form.sensitive_reply" type="textarea" :rows="2" />
      </el-form-item>
      <el-form-item label="转人工话术" prop="handoff_reply">
        <el-input v-model="form.handoff_reply" type="textarea" :rows="2" />
      </el-form-item>
      <el-form-item label="转人工关键词" prop="handoff_keywords_text">
        <el-input
          v-model="form.handoff_keywords_text"
          type="textarea"
          :rows="6"
          placeholder="每行一个关键词，也可逗号分隔；直接换行即可新增"
        />
        <div class="hint">用户消息命中任一关键词 → 转人工</div>
      </el-form-item>
      <el-form-item label="闲聊回复" prop="chitchat_reply">
        <el-input v-model="form.chitchat_reply" type="textarea" :rows="2" />
      </el-form-item>
      <el-form-item label="闲聊关键词" prop="chitchat_phrases_text">
        <el-input
          v-model="form.chitchat_phrases_text"
          type="textarea"
          :rows="8"
          placeholder="每行一个关键词，也可逗号分隔；直接换行即可新增"
        />
        <div class="hint">命中任一关键词 → 闲聊回复（不检索 FAQ）</div>
      </el-form-item>
    </el-form>
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
  margin-bottom: 20px;
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
}
.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.form {
  max-width: 880px;
}
.hint {
  margin-top: 4px;
  color: #909399;
  font-size: 12px;
  line-height: 1.4;
}
</style>
