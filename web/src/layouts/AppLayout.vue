<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  ChatDotRound,
  ChatLineSquare,
  Connection,
  Document,
  Goods,
  User,
  Warning,
} from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { logout as apiLogout } from "@/api/auth";
import {
  clearTokens,
  getRefreshToken,
  getRole,
  getUsername,
  isOps,
} from "@/auth/session";
import { mountDifyChatbot, unmountDifyChatbot } from "@/dify/embed";

const route = useRoute();
const router = useRouter();

const username = computed(() => getUsername());
const roleLabel = computed(() => (getRole() === "ops" ? "运营" : "用户"));
const showOps = computed(() => isOps());

const active = computed(() => {
  if (route.path.startsWith("/marketplace")) return "/marketplace";
  if (route.path.startsWith("/faqs")) return "/faqs";
  if (route.path.startsWith("/sensitive")) return "/sensitive";
  if (route.path.startsWith("/bot-scripts")) return "/bot-scripts";
  if (route.path.startsWith("/channels")) return "/channels";
  if (route.path.startsWith("/users")) return "/users";
  return "/chat";
});

onMounted(() => {
  mountDifyChatbot();
});

onBeforeUnmount(() => {
  unmountDifyChatbot();
});

function onSelect(index: string) {
  void router.push(index);
}

async function onLogout() {
  const rt = getRefreshToken();
  try {
    if (rt) await apiLogout(rt);
  } catch {
    // ignore
  }
  unmountDifyChatbot();
  clearTokens();
  ElMessage.success("已退出");
  await router.replace("/login");
}
</script>

<template>
  <el-container class="layout">
    <el-aside width="200px" class="aside">
      <div class="brand">ReplyKit</div>
      <div class="who">{{ username }} · {{ roleLabel }}</div>
      <el-menu :default-active="active" @select="onSelect">
        <el-menu-item index="/chat">
          <el-icon><ChatDotRound /></el-icon>
          <span>对话</span>
        </el-menu-item>
        <el-menu-item index="/marketplace">
          <el-icon><Goods /></el-icon>
          <span>智能体市场</span>
        </el-menu-item>
        <el-menu-item index="/channels">
          <el-icon><Connection /></el-icon>
          <span>渠道配置</span>
        </el-menu-item>
        <template v-if="showOps">
          <el-menu-item index="/faqs">
            <el-icon><Document /></el-icon>
            <span>FAQ 管理</span>
          </el-menu-item>
          <el-menu-item index="/sensitive">
            <el-icon><Warning /></el-icon>
            <span>敏感词</span>
          </el-menu-item>
          <el-menu-item index="/bot-scripts">
            <el-icon><ChatLineSquare /></el-icon>
            <span>话术配置</span>
          </el-menu-item>
          <el-menu-item index="/users">
            <el-icon><User /></el-icon>
            <span>用户管理</span>
          </el-menu-item>
        </template>
      </el-menu>
      <div class="footer">
        <el-button text type="primary" @click="onLogout">退出登录</el-button>
      </div>
    </el-aside>
    <el-main class="main">
      <router-view />
    </el-main>
  </el-container>
</template>

<style scoped>
.layout {
  min-height: 100vh;
  background: #f5f7fa;
}

.aside {
  display: flex;
  flex-direction: column;
  background: #fff;
  border-right: 1px solid #ebeef5;
}

.brand {
  padding: 20px 16px 4px;
  font-size: 18px;
  font-weight: 600;
  color: #303133;
}

.who {
  padding: 0 16px 12px;
  font-size: 12px;
  color: #909399;
}

.footer {
  margin-top: auto;
  padding: 12px 16px 20px;
}

.main {
  padding: 20px 24px;
}
</style>
