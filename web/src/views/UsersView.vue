<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import {
  createUser,
  listUsers,
  resetUserPassword,
  updateUser,
  type UserItem,
} from '@/api/users'
import type { UserRole } from '@/auth/session'
import { ApiError } from '@/api/client'

const loading = ref(false)
const items = ref<UserItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const keyword = ref('')

const dialogVisible = ref(false)
const formRef = ref<FormInstance>()
const form = reactive({
  username: '',
  password: '',
  role: 'user' as UserRole,
})

const resetVisible = ref(false)
const resetFormRef = ref<FormInstance>()
const resetForm = reactive({
  username: '',
  new_password: '',
  confirm: '',
})

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { pattern: /^[A-Za-z0-9_]{3,32}$/, message: '3–32 位字母数字下划线', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '至少 6 位', trigger: 'blur' },
  ],
}

const resetRules: FormRules = {
  new_password: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 6, message: '至少 6 位', trigger: 'blur' },
  ],
  confirm: [
    { required: true, message: '请再次输入新密码', trigger: 'blur' },
    {
      validator: (_r, value, callback) => {
        if (value !== resetForm.new_password) callback(new Error('两次密码不一致'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

async function fetchList() {
  loading.value = true
  try {
    const data = await listUsers({
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
  form.username = ''
  form.password = ''
  form.role = 'user'
  dialogVisible.value = true
}

function openReset(row: UserItem) {
  resetForm.username = row.username
  resetForm.new_password = ''
  resetForm.confirm = ''
  resetVisible.value = true
}

async function submitCreate() {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return
  try {
    await createUser({
      username: form.username,
      password: form.password,
      role: form.role,
    })
    ElMessage.success('已创建')
    dialogVisible.value = false
    await fetchList()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '创建失败')
  }
}

async function submitReset() {
  const ok = await resetFormRef.value?.validate().catch(() => false)
  if (!ok) return
  try {
    await resetUserPassword({
      username: resetForm.username,
      new_password: resetForm.new_password,
    })
    ElMessage.success('密码已重置，请通知用户使用新密码登录')
    resetVisible.value = false
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '重置失败')
  }
}

async function onRoleChange(row: UserItem, role: UserRole) {
  try {
    await updateUser({ username: row.username, role })
    row.role = role
    ElMessage.success('角色已更新')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '更新失败')
    await fetchList()
  }
}

async function onToggle(row: UserItem, enabled: boolean) {
  try {
    await updateUser({ username: row.username, enabled })
    row.enabled = enabled
  } catch (err) {
    row.enabled = !enabled
    ElMessage.error(err instanceof ApiError ? err.message : '更新失败')
  }
}

function formatTime(ts: number) {
  return ts ? new Date(ts * 1000).toLocaleString() : '—'
}

onMounted(() => {
  void fetchList()
})
</script>

<template>
  <div class="page">
    <div class="toolbar">
      <h2>用户管理</h2>
      <div class="actions">
        <el-input
          v-model="keyword"
          clearable
          placeholder="搜索用户名"
          style="width: 180px"
          @keyup.enter="fetchList"
        />
        <el-button @click="fetchList">搜索</el-button>
        <el-button type="primary" @click="openCreate">新建用户</el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="items" stripe border>
      <el-table-column prop="username" label="用户名" min-width="120" />
      <el-table-column label="角色" width="140">
        <template #default="{ row }">
          <el-select
            :model-value="row.role"
            size="small"
            @change="(v: string) => onRoleChange(row, v as UserRole)"
          >
            <el-option label="普通用户" value="user" />
            <el-option label="运营" value="ops" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="90">
        <template #default="{ row }">
          <el-switch
            :model-value="row.enabled"
            @change="(v: string | number | boolean) => onToggle(row, Boolean(v))"
          />
        </template>
      </el-table-column>
      <el-table-column label="创建时间" width="180">
        <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="120" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openReset(row)">重置密码</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="pager">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        background
        layout="total, prev, pager, next"
        :total="total"
        @current-change="fetchList"
      />
    </div>

    <el-dialog v-model="dialogVisible" title="新建用户" width="420px" destroy-on-close>
      <el-form ref="formRef" :model="form" :rules="rules" label-width="80px">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="form.password" type="password" show-password />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="form.role" style="width: 100%">
            <el-option label="普通用户" value="user" />
            <el-option label="运营" value="ops" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="resetVisible"
      :title="`重置密码 · ${resetForm.username}`"
      width="420px"
      destroy-on-close
    >
      <el-form ref="resetFormRef" :model="resetForm" :rules="resetRules" label-width="90px">
        <el-form-item label="新密码" prop="new_password">
          <el-input
            v-model="resetForm.new_password"
            type="password"
            show-password
            autocomplete="new-password"
          />
        </el-form-item>
        <el-form-item label="确认密码" prop="confirm">
          <el-input
            v-model="resetForm.confirm"
            type="password"
            show-password
            autocomplete="new-password"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="resetVisible = false">取消</el-button>
        <el-button type="primary" @click="submitReset">确认重置</el-button>
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
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
h2 { margin: 0; font-size: 18px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
.pager { display: flex; justify-content: flex-end; margin-top: 16px; }
</style>
