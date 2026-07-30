import { createRouter, createWebHistory } from 'vue-router'
import { isLoggedIn, isOps } from '@/auth/session'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { title: '登录', public: true },
    },
    {
      path: '/register',
      name: 'register',
      component: () => import('@/views/RegisterView.vue'),
      meta: { title: '注册', public: true },
    },
    {
      path: '/',
      component: () => import('@/layouts/AppLayout.vue'),
      meta: { requiresAuth: true },
      children: [
        { path: '', redirect: '/chat' },
        {
          path: 'chat',
          name: 'chat',
          component: () => import('@/views/ChatView.vue'),
          meta: { title: '对话' },
        },
        {
          path: 'faqs',
          name: 'faqs',
          component: () => import('@/views/FaqsView.vue'),
          meta: { title: 'FAQ 管理', ops: true },
        },
        {
          path: 'sensitive',
          name: 'sensitive',
          component: () => import('@/views/SensitiveView.vue'),
          meta: { title: '敏感词', ops: true },
        },
        {
          path: 'bot-scripts',
          name: 'bot-scripts',
          component: () => import('@/views/BotScriptsView.vue'),
          meta: { title: '话术配置', ops: true },
        },
        {
          path: 'channels',
          name: 'channels',
          component: () => import('@/views/ChannelsView.vue'),
          meta: { title: '渠道配置' },
        },
        {
          path: 'users',
          name: 'users',
          component: () => import('@/views/UsersView.vue'),
          meta: { title: '用户管理', ops: true },
        },
      ],
    },
  ],
})

router.beforeEach((to) => {
  if (to.meta.public) {
    if (isLoggedIn() && (to.name === 'login' || to.name === 'register')) {
      return { path: '/chat' }
    }
    return true
  }
  if (to.matched.some((r) => r.meta.requiresAuth) && !isLoggedIn()) {
    return {
      path: '/login',
      query: { redirect: to.fullPath },
    }
  }
  if (to.matched.some((r) => r.meta.ops) && !isOps()) {
    return { path: '/chat' }
  }
  return true
})

router.afterEach((to) => {
  const title = (to.meta.title as string) || 'ReplyKit'
  document.title = `${title} · ReplyKit`
})

export default router
