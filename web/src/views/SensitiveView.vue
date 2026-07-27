<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import {
  createSensitiveWord,
  deleteSensitiveWords,
  importSensitiveWords,
  listSensitiveWords,
  updateSensitiveWord,
  type SensitiveItem,
} from '@/api/sensitive'
import { ApiError } from '@/api/client'

const loading = ref(false)
const items = ref<SensitiveItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const keyword = ref('')

const dialogVisible = ref(false)
const editingId = ref<string | null>(null)
const formRef = ref<FormInstance>()
const form = reactive({
  pattern: '',
  note: '',
  enabled: true,
})

const rules: FormRules = {
  pattern: [{ required: true, message: '请输入敏感词/正则', trigger: 'blur' }],
}

async function fetchList() {
  loading.value = true
  try {
    const data = await listSensitiveWords({
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
  form.pattern = ''
  form.note = ''
  form.enabled = true
  dialogVisible.value = true
}

function openEdit(row: SensitiveItem) {
  editingId.value = row.id
  form.pattern = row.pattern
  form.note = row.note
  form.enabled = row.enabled
  dialogVisible.value = true
}

async function submitForm() {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return
  try {
    if (editingId.value) {
      await updateSensitiveWord({
        id: editingId.value,
        pattern: form.pattern,
        note: form.note,
        enabled: form.enabled,
      })
      ElMessage.success('已更新')
    } else {
      await createSensitiveWord({
        pattern: form.pattern,
        note: form.note,
        enabled: form.enabled,
      })
      ElMessage.success('已创建')
    }
    dialogVisible.value = false
    await fetchList()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '保存失败')
  }
}

async function onDelete(row: SensitiveItem) {
  try {
    await ElMessageBox.confirm(`确认删除「${row.pattern}」？`, '删除确认', {
      type: 'warning',
    })
    await deleteSensitiveWords([row.id])
    ElMessage.success('已删除')
    await fetchList()
  } catch (err) {
    if (err === 'cancel' || err === 'close') return
    ElMessage.error(err instanceof ApiError ? err.message : '删除失败')
  }
}

async function onToggle(row: SensitiveItem, enabled: boolean) {
  try {
    await updateSensitiveWord({ id: row.id, enabled })
    row.enabled = enabled
  } catch (err) {
    row.enabled = !enabled
    ElMessage.error(err instanceof ApiError ? err.message : '更新失败')
  }
}

async function onImport() {
  try {
    const { value } = await ElMessageBox.prompt(
      '填写相对项目根的路径，例如 data/sensitive.txt',
      '从文件导入',
      {
        inputValue: 'data/sensitive.txt',
        confirmButtonText: '导入',
        cancelButtonText: '取消',
      },
    )
    const path = (value || '').trim()
    if (!path) return
    const data = await importSensitiveWords(path)
    ElMessage.success(`导入完成：新增 ${data.imported}，跳过 ${data.skipped}`)
    await fetchList()
  } catch (err) {
    if (err === 'cancel' || err === 'close') return
    ElMessage.error(err instanceof ApiError ? err.message : '导入失败')
  }
}

function onSearch() {
  page.value = 1
  void fetchList()
}

onMounted(() => {
  void fetchList()
})
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <div>
        <h2>敏感词配置</h2>
        <p class="sub">命中后走敏感词话术（热更新，无需重启）</p>
      </div>
      <div class="actions">
        <el-input
          v-model="keyword"
          clearable
          placeholder="搜索敏感词"
          style="width: 200px"
          @keyup.enter="onSearch"
        />
        <el-button @click="onSearch">搜索</el-button>
        <el-button type="primary" @click="openCreate">新建</el-button>
        <el-button @click="onImport">从文件导入</el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="items" stripe border>
      <el-table-column prop="pattern" label="敏感词/模式" min-width="180" show-overflow-tooltip />
      <el-table-column prop="note" label="备注" min-width="140" show-overflow-tooltip />
      <el-table-column label="启用" width="90">
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
      :title="editingId ? '编辑敏感词' : '新建敏感词'"
      width="520px"
      destroy-on-close
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-width="88px">
        <el-form-item label="敏感词" prop="pattern">
          <el-input v-model="form.pattern" placeholder="关键词或匹配模式" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="form.note" type="textarea" :rows="2" placeholder="可选" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
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
.page {
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 16px 20px 20px;
}

.toolbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 16px;
  gap: 12px;
  flex-wrap: wrap;
}

.toolbar h2 {
  margin: 0;
  font-size: 18px;
}

.sub {
  margin: 6px 0 0;
  color: #909399;
  font-size: 13px;
}

.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
