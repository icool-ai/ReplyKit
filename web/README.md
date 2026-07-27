# ReplyKit 前端（Vue 3）

与仓库根目录 FastAPI 同仓（monorepo），运行时仍前后端分离。

## 要求

- Node.js **18+**（本机可用 `nvm use 20.19.0`）
- 后端 API 已启动（默认 `http://127.0.0.1:8000`）

## 启动

```bash
cd web
npm install
npm run dev
```

浏览器打开 http://127.0.0.1:5173 ，使用后端 `.env` 中的 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录。
开发请求经 Vite proxy：`/api/*` → 后端。

## 页面

| 路径 | 说明 |
|------|------|
| `/chat` | 对话 |
| `/faqs` | FAQ 管理（导入模板 / 路径导入） |
| `/sensitive` | 敏感词 |
| `/bot-scripts` | 话术配置 |
| `/users` | 用户管理（ops） |
| `/login` `/register` | 登录 / 注册 |
