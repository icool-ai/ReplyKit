<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { createFaq, deleteFaqs, downloadFaqTemplate, importFaqFile, listFaqs, updateFaq } from '@/api/faqs'
import type { FaqTemplateFormat } from '@/api/faqs'
import type { FaqItem } from '@/api/types'
import { ApiError } from '@/api/client'
import type { UploadRequestOptions } from 'element-plus'

const loading = ref(false)
const items = ref<FaqItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const keyword = ref('')

const dialogVisible = ref(false)
const editingId = ref<string | null>(null)
const formRef = ref<FormInstance>()
const form = reactive({
  question: '',
  answer: '',
  category: '',
  similarText: '',
  enabled: true,
  visibility: 'public' as 'public' | 'private',
  owner_username: '',
  allow_egress: true,
})

const ownerRequired = computed(() => form.visibility === 'private')

const rules = computed<FormRules>(() => ({
  question: [{ required: true, message: '请输入标准问', trigger: 'blur' }],
  answer: [{ required: true, message: '请输入答案', trigger: 'blur' }],
  owner_username: ownerRequired.value
    ? [{ required: true, message: '私有知识请填写所属用户名', trigger: 'blur' }]
    : [],
}))

function visibilityLabel(v: string | undefined) {
  return v === 'private' ? '私有' : '公开'
}

function resetFormAcl() {
  form.visibility = 'public'
  form.owner_username = ''
  form.allow_egress = true
}

async function fetchList() {
  loading.value = true
  try {
    const data = await listFaqs({
      page: page.value,
      page_size: pageSize.value,
      keyword: keyword.value.trim() || undefined,
    })
    items.value = data.items
    total.value = data.total
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载失败')
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingId.value = null
  form.question = ''
  form.answer = ''
  form.category = ''
  form.similarText = ''
  form.enabled = true
  resetFormAcl()
  dialogVisible.value = true
}

function openEdit(row: FaqItem) {
  editingId.value = row.id
  form.question = row.question
  form.answer = row.answer
  form.category = row.category
  form.similarText = row.similar.join('\n')
  form.enabled = row.enabled
  form.visibility = row.visibility === 'private' ? 'private' : 'public'
  form.owner_username = row.owner_username || ''
  form.allow_egress = row.allow_egress !== false
  dialogVisible.value = true
}

function parseSimilar(text: string): string[] {
  return text
    .split(/\n/)
    .map((s) => s.trim())
    .filter(Boolean)
}

async function submitForm() {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return

  const acl = {
    visibility: form.visibility,
    owner_username: form.owner_username.trim(),
    allow_egress: form.allow_egress,
  }

  try {
    if (editingId.value) {
      await updateFaq({
        id: editingId.value,
        question: form.question,
        answer: form.answer,
        category: form.category,
        similar: parseSimilar(form.similarText),
        enabled: form.enabled,
        ...acl,
      })
      ElMessage.success('已更新')
    } else {
      await createFaq({
        question: form.question,
        answer: form.answer,
        category: form.category,
        similar: parseSimilar(form.similarText),
        enabled: form.enabled,
        ...acl,
      })
      ElMessage.success('已创建')
    }
    dialogVisible.value = false
    await fetchList()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '保存失败')
  }
}

async function onDelete(row: FaqItem) {
  try {
    await ElMessageBox.confirm(`确认删除 FAQ「${row.question}」？`, '删除确认', {
      type: 'warning',
    })
    await deleteFaqs([row.id])
    ElMessage.success('已删除')
    await fetchList()
  } catch (err) {
    if (err === 'cancel' || err === 'close') return
    ElMessage.error(err instanceof ApiError ? err.message : '删除失败')
  }
}

async function onToggle(row: FaqItem, enabled: boolean) {
  try {
    await updateFaq({ id: row.id, enabled })
    row.enabled = enabled
  } catch (err) {
    row.enabled = !enabled
    ElMessage.error(err instanceof ApiError ? err.message : '更新失败')
  }
}

function onSearch() {
  page.value = 1
  void fetchList()
}

async function onDownloadTemplate(format: FaqTemplateFormat) {
  try {
    await downloadFaqTemplate(format)
    ElMessage.success('模板已开始下载')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '下载失败')
  }
}

const importing = ref(false)

async function onUploadImport(option: UploadRequestOptions) {
  const raw = option.file
  const file = raw instanceof File ? raw : (raw as { raw?: File }).raw
  if (!file) {
    ElMessage.error('未选择文件')
    option.onError?.(new Error('未选择文件') as never)
    return
  }
  importing.value = true
  try {
    const data = await importFaqFile(file)
    ElMessage.success(
      `已导入 ${data.imported} 条（文件内有效 ${data.total_in_file} 条，索引异步）`,
    )
    option.onSuccess?.(data as never)
    await fetchList()
  } catch (err) {
    const message = err instanceof ApiError ? err.message : '导入失败'
    ElMessage.error(message)
    option.onError?.(err as never)
  } finally {
    importing.value = false
  }
}

onMounted(() => {
  void fetchList()
})
</script>

<template>
  <div class="faqs-page">
    <div class="toolbar">
      <h2>FAQ 管理</h2>
      <div class="actions">
        <el-input
          v-model="keyword"
          clearable
          placeholder="搜索问题 / 答案"
          style="width: 220px"
          @keyup.enter="onSearch"
        />
        <el-button @click="onSearch">搜索</el-button>
        <el-button type="primary" @click="openCreate">新建</el-button>
        <el-dropdown trigger="click" @command="onDownloadTemplate">
          <el-button>
            下载导入模板
            <span class="caret">▾</span>
          </el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="json">JSON（含字段说明）</el-dropdown-item>
              <el-dropdown-item command="csv">CSV（中文表头）</el-dropdown-item>
              <el-dropdown-item command="txt">文本 Q/A（.txt）</el-dropdown-item>
              <el-dropdown-item command="xlsx">Excel（中文表头+说明）</el-dropdown-item>
              <el-dropdown-item command="xls">Excel 97-2003（中文表头+说明）</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <el-upload
          :show-file-list="false"
          :disabled="importing"
          accept=".json,.csv,.txt,.text,.xls,.xlsx"
          :http-request="onUploadImport"
        >
          <el-button :loading="importing">导入 FAQ 文件</el-button>
        </el-upload>
      </div>
    </div>

    <el-table v-loading="loading" :data="items" stripe border>
      <el-table-column prop="category" label="分类" width="100" show-overflow-tooltip />
      <el-table-column prop="question" label="标准问" min-width="160" show-overflow-tooltip />
      <el-table-column prop="answer" label="答案" min-width="180" show-overflow-tooltip />
      <el-table-column label="可见范围" width="88">
        <template #default="{ row }">
          <el-tag :type="row.visibility === 'private' ? 'warning' : 'info'" size="small">
            {{ visibilityLabel(row.visibility) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="所属用户" width="110" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.visibility === 'private' ? row.owner_username || '—' : '—' }}
        </template>
      </el-table-column>
      <el-table-column label="送公网模型" width="100">
        <template #default="{ row }">
          <el-tag :type="row.allow_egress === false ? 'danger' : 'success'" size="small">
            {{ row.allow_egress === false ? '禁止' : '允许' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="相似问" width="80">
        <template #default="{ row }">{{ row.similar.length }}</template>
      </el-table-column>
      <el-table-column label="启用" width="80">
        <template #default="{ row }">
          <el-switch
            :model-value="row.enabled"
            @change="(v: string | number | boolean) => onToggle(row, Boolean(v))"
          />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="140" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" @click="onDelete(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="pager">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        background
        layout="total, prev, pager, next, sizes"
        :total="total"
        :page-sizes="[10, 20, 50]"
        @current-change="fetchList"
        @size-change="
          () => {
            page = 1
            fetchList()
          }
        "
      />
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '编辑 FAQ' : '新建 FAQ'"
      width="680px"
      destroy-on-close
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="108px">
        <el-form-item label="标准问" prop="question">
          <el-input v-model="form.question" />
        </el-form-item>
        <el-form-item label="答案" prop="answer">
          <el-input v-model="form.answer" type="textarea" :rows="5" />
        </el-form-item>
        <el-form-item label="分类">
          <el-input v-model="form.category" placeholder="可选" />
        </el-form-item>
        <el-form-item label="相似问">
          <el-input
            v-model="form.similarText"
            type="textarea"
            :rows="4"
            placeholder="每行一条相似问"
          />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>

        <el-divider content-position="left">访问控制</el-divider>
        <p class="acl-hint">
          「送公网模型」指：命中本条后，是否允许把内容发给通义等公网大模型做润色。
          关闭后仍可问答，一般会直接返回标准答案。与用户能否打开网页无关。
        </p>
        <el-form-item label="可见范围">
          <el-radio-group v-model="form.visibility">
            <el-radio label="public">公开（登录用户可检索）</el-radio>
            <el-radio label="private">私有（仅所属用户 + 运营）</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item
          v-if="form.visibility === 'private'"
          label="所属用户"
          prop="owner_username"
        >
          <el-input
            v-model="form.owner_username"
            placeholder="填写系统登录用户名，如 alice"
          />
        </el-form-item>
        <el-form-item label="送公网模型">
          <el-switch
            v-model="form.allow_egress"
            active-text="允许"
            inactive-text="禁止"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submitForm">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.faqs-page {
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 16px 20px 20px;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  gap: 12px;
  flex-wrap: wrap;
}

.toolbar h2 {
  margin: 0;
  font-size: 18px;
}

.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}

.caret {
  margin-left: 4px;
  font-size: 12px;
  opacity: 0.7;
}

.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

.acl-hint {
  margin: -4px 0 12px 108px;
  font-size: 12px;
  color: #909399;
  line-height: 1.5;
}
</style>
