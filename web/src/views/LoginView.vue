<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { login } from '@/api/auth'
import { ApiError } from '@/api/client'
import { setTokens } from '@/auth/session'

const router = useRouter()
const route = useRoute()
const loading = ref(false)
const formRef = ref<FormInstance>()
const form = reactive({
  username: 'admin',
  password: '',
})

const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

async function onSubmit() {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok || loading.value) return
  loading.value = true
  try {
    const data = await login(form.username, form.password)
    setTokens(data)
    ElMessage.success('登录成功')
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/chat'
    await router.replace(redirect || '/chat')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '登录失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="panel">
      <h1>ReplyKit</h1>
      <p class="sub">管理后台登录</p>
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" autocomplete="username" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            autocomplete="current-password"
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <el-button type="primary" class="submit" :loading="loading" @click="onSubmit">
          登录
        </el-button>
        <div class="switch">
          还没有账号？
          <router-link to="/register">去注册</router-link>
        </div>
      </el-form>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(160deg, #eef2f7 0%, #f7f8fa 45%, #e8eef6 100%);
}

.panel {
  width: 360px;
  padding: 32px 28px 28px;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 10px;
}

h1 {
  margin: 0;
  font-size: 22px;
  text-align: center;
}

.sub {
  margin: 6px 0 24px;
  text-align: center;
  color: #909399;
  font-size: 13px;
}

.submit {
  width: 100%;
  margin-top: 8px;
}

.switch {
  margin-top: 16px;
  text-align: center;
  font-size: 13px;
  color: #909399;
}

.switch a {
  color: #409eff;
  text-decoration: none;
}
</style>
