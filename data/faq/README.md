# FAQ 数据说明

- **运行时主数据**：SQLite `data/faqs.db`（表 `faqs`），通过 `POST /faqs*` 增删改
- **导入**：`POST /faqs/import`，支持本地 `path` 或 `url`
- **支持格式**：`.json` / `.csv` / `.txt`（Q/A 模板）/ `.xls` / `.xlsx`
- **模板下载**：`GET /faqs/import-templates`、`GET /faqs/import-templates/{format}`（含 1～2 条示例）

## 文本模板示例（.txt）

```text
Q: 如何修改收货地址？
A: 未发货订单可在「我的订单」中修改。
S: 收货地址能改吗
C: 订单物流

---

Q: 支持哪些支付方式？
A: 支持微信、支付宝、银联。
```

## CSV 列

`id,category,question,answer,similar`（相似问用 `|` 分隔）
