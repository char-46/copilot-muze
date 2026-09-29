---
name: doc-sources
description: 从 docx/在线文档(腾讯文档、金山文档、小程序卡片)提取内容的标准流程
---

# 文档信息获取流程

## 1. 本地文件（QQ 收到的 docx/txt/…）
1. `cli({"command": "msg <scene/peer> <seq>"})` 查看消息详情 → 媒体行有落盘路径（如 `data/media/2026-09/...docx`）。
   不知道 seq 就先 `cli({"command": "search <文件名或关键词>"})`。
2. 用 `read_document({"path": ...})` 读取全文。
3. `.doc`/`.pdf` 会报不支持 → 建议发送方转存 docx 或复制文本。

## 2. QQ 小程序卡片（腾讯文档/金山文档分享）
1. `cli({"command": "msg <scene/peer> <seq>"})` 找到该消息，`raw` 里有 `light_app` 段，
   其 `json_payload` 是 JSON 字符串。
2. 从 json_payload 里找链接字段（常见键：`url`、`docUrl`、`pagePath`、`jumpUrl`、`qqdocurl`、
   `link`，以及嵌套在 `meta`/`extra` 里的）。可多级 JSON.parse。
3. 拿到直链后用 `fetch_url` 抓取。
4. 如果 fetch_url 返回"需要 JS 渲染或登录"：
   - 腾讯文档 docs.qq.com：让分享者设置「链接分享-任何人可查看」后重试；
   - 仍不行：请用户在文档页导出 docx 发到 QQ（回到流程 1），或直接复制粘贴正文。

## 3. 普通网页链接（消息里的文本 URL）
直接 `fetch_url({"url": ...})`。

## 原则
- 先看消息本身：image 段的 summary、light_app 的预览文本常常已经包含标题/摘要。
- 抓不到时明确告诉用户原因和替代方案，不要编造文档内容。
