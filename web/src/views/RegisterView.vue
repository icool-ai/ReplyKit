<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { register } from '@/api/auth'
import { ApiError } from '@/api/client'
import { setTokens } from '@/auth/session'

const router = useRouter()
const loading = ref(false)
const formRef = ref<FormInstance>()
const form = reactive({
  username: '',
  password: '',
  confirm: '',
})

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    {
      pattern: /^[A-Za-z0-9_]{3,32}$/,
      message: '3–32 位字母、数字或下划线',
      trigger: 'blur',
    },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少 6 位', trigger: 'blur' },
  ],
  confirm: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      validator: (_rule, value, callback) => {
        if (value !== form.password) callback(new Error('两次密码不一致'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

async function onSubmit() {
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok || loading.value) return
  loading.value = true
  try {
    const data = await register(form.username, form.password)
    setTokens(data)
    ElMessage.success('注册成功')
    await router.replace('/chat')
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '注册失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="panel">
      <h1>ReplyKit</h1>
      <p class="sub">注册账号</p>
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" autocomplete="username" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            autocomplete="new-password"
          />
        </el-form-item>
        <el-form-item label="确认密码" prop="confirm">
          <el-input
            v-model="form.confirm"
            type="password"
            show-password
            autocomplete="new-password"
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <el-button type="primary" class="submit" :loading="loading" @click="onSubmit">
          注册
        </el-button>
        <div class="switch">
          已有账号？
          <router-link to="/login">去登录</router-link>
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
