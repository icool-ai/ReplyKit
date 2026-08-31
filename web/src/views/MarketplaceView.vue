<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ShoppingBag, ChatDotRound } from '@element-plus/icons-vue'
import {
  listAllAgents,
  listEnabledAgents,
  updateAgent,
  type AgentItem,
} from '@/api/agents'
import { ApiError } from '@/api/client'
import { isOps } from '@/auth/session'

const router = useRouter()
const loading = ref(false)
const agents = ref<AgentItem[]>([])
const showOps = computed(() => isOps())

const editVisible = ref(false)
const editForm = reactive({
  id: '',
  name: '',
  description: '',
  enabled: true,
  sort_order: 100,
})

async function load() {
  loading.value = true
  try {
    const data = showOps.value ? await listAllAgents() : await listEnabledAgents()
    agents.value = data.items
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '加载失败')
  } finally {
    loading.value = false
  }
}

function iconComp(agent: AgentItem) {
  return agent.runtime === 'mp_agent' ? ShoppingBag : ChatDotRound
}

function openChat(agent: AgentItem) {
  if (!agent.enabled) {
    ElMessage.warning('该智能体已停用')
    return
  }
  localStorage.setItem('replykit_active_agent', agent.id)
  void router.push({ path: '/chat', query: { agent: agent.id } })
}

function openEdit(agent: AgentItem) {
  editForm.id = agent.id
  editForm.name = agent.name
  editForm.description = agent.description
  editForm.enabled = agent.enabled
  editForm.sort_order = agent.sort_order
  editVisible.value = true
}

async function saveEdit() {
  try {
    await updateAgent(editForm.id, {
      name: editForm.name,
      description: editForm.description,
      enabled: editForm.enabled,
      sort_order: editForm.sort_order,
    })
    ElMessage.success('已保存')
    editVisible.value = false
    await load()
  } catch (err) {
    ElMessage.error(err instanceof ApiError ? err.message : '保存失败')
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="head">
      <h2>智能体市场</h2>
      <p class="sub">选择官方智能体进入对话；运营可上下架与编辑简介。</p>
    </div>
    <div class="grid">
      <div v-for="a in agents" :key="a.id" class="card">
        <div class="card-top">
          <el-icon :size="28" class="ico"><component :is="iconComp(a)" /></el-icon>
          <div>
            <div class="title">{{ a.name }}</div>
            <div class="meta">
              <el-tag size="small" type="info">{{ a.category || '通用' }}</el-tag>
              <el-tag v-if="!a.enabled" size="small" type="danger">已停用</el-tag>
            </div>
          </div>
        </div>
        <p class="desc">{{ a.description }}</p>
        <div class="actions">
          <el-button type="primary" :disabled="!a.enabled" @click="openChat(a)">
            进入对话
          </el-button>
          <el-button v-if="showOps" @click="openEdit(a)">编辑</el-button>
        </div>
      </div>
    </div>

    <el-dialog v-model="editVisible" title="编辑智能体" width="480px">
      <el-form label-width="80px">
        <el-form-item label="名称">
          <el-input v-model="editForm.name" />
        </el-form-item>
        <el-form-item label="简介">
          <el-input v-model="editForm.description" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="排序">
          <el-input-number v-model="editForm.sort_order" :min="0" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="editForm.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.page {
  max-width: 960px;
}
.head h2 {
  margin: 0 0 6px;
  font-size: 22px;
}
.sub {
  margin: 0 0 20px;
  color: #909399;
  font-size: 13px;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
}
.card {
  background: #fff;
  border: 1px solid #ebeef5;
  border-radius: 10px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.card-top {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}
.ico {
  color: #409eff;
}
.title {
  font-weight: 600;
  font-size: 16px;
}
.meta {
  margin-top: 4px;
  display: flex;
  gap: 6px;
}
.desc {
  margin: 0;
  color: #606266;
  font-size: 13px;
  line-height: 1.5;
  flex: 1;
}
.actions {
  display: flex;
  gap: 8px;
}
</style>
