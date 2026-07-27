# 机器人话术配置

编辑本文件后**重启** 服务即可生效（无需改代码）。

| 字段 | 说明 |
|------|------|
| `welcome` | 进入页面时的欢迎语 |
| `no_answer` | 知识库未命中时的兜底话术 |
| `sensitive_reply` | 命中敏感词时的回复话术（词表见 `data/sensitive.db`，用 `POST /sensitive-words*` 管理） |
| `handoff_keywords` / `handoff_reply` | 转人工触发词与回复 |
| `handoff_after_no_answer` | 连续无答案（拒识）达到 N 次 → 自动转人工（默认 3） |
| `handoff_after_repeat` | 连续重复同一问题达到 N 次 → 自动转人工（默认 3） |
| `chitchat_phrases` / `chitchat_reply` | 闲聊白名单与回复 |
| `history_turns` | 追问改写时最多参考最近几轮对话（默认 3） |
| `history_user_chars` / `history_assistant_chars` | 改写上下文里单轮用户/助手截断长度 |
| `rewrite_enabled` | 追问时是否优先用模型改写成完整问句（关闭则只拼 last_topic） |
| `followup_markers` | 判定「像追问」的关键词；空则用内置默认 |

敏感词列表示例文件：`data/sensitive.txt`（一行一词）。导入：

```http
POST /sensitive-words/import
{"path": "data/sensitive.txt"}
```

加词后**无需重启**，立即参与匹配。
