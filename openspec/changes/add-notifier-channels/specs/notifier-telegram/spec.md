## 新增需求

### 需求:Telegram 适配器必须渲染 MarkdownV2 并正确转义

`TelegramNotifier`（`name == "telegram"`）必须从 `notifiers/templates/telegram/` 加载 Jinja2 模板，渲染 Telegram **MarkdownV2** 文本。模板环境必须注册 `mdv2_escape` filter，对 MarkdownV2 官方保留字符 `_ * [ ] ( ) ~ \` > # + - = | { } . !` 转义；并必须**同时转义内容中的字面反斜杠 `\`**（MarkdownV2 中 `\` 是转义引导符，内容里的字面 `\` 不转义会吞掉后一字符），使报告中任意内容（target 名、finding message、根因摘要）不会破坏 MarkdownV2 结构或被解释为格式标记。`validate_config` 必须要求 `bot_token` 与 `chat_id` 两个字段存在**且非空**（空串视为缺失）。

#### 场景:含保留字符的内容被转义

- **当** 渲染一个 `message` 含 `_`、`*`、`.` 等 MarkdownV2 保留字符的 finding
- **那么** 渲染产物中这些字符必须被反斜杠转义；产物作为 MarkdownV2 发送不会触发解析错误

#### 场景:缺失配置字段 fail-loud

- **当** `validate_config({"bot_token": "x"})`（缺 `chat_id`）
- **那么** 必须 raise（配置不完整），禁止静默通过

### 需求:Telegram 适配器必须经 Bot API `sendMessage` 发送且 token 不入日志

`send` 必须 POST 到 `https://api.telegram.org/bot<token>/sendMessage`，body 含 `chat_id` / `text` / `parse_mode="MarkdownV2"`。HTTP 必须用 `httpx.AsyncClient`。日志与 `NotifyResult.detail` 中 `bot_token` 必须打码（禁止明文出现）。**成功判定必须基于响应 body 的 `ok` 字段，不仅是 HTTP 状态**：Telegram 会以 HTTP 200 + `{"ok": false, "error_code": ..., "description": ...}` 表达业务失败（如 chat not found）——此情形必须记 `failed`（error 取脱敏后的 `description`），**禁止**误记 `sent`。仅当 HTTP 2xx **且** body `ok == true` **且** 含 `result.message_id` 时记 `sent` 并把 `str(message_id)` 存入 `detail`；HTTP 2xx 但 body 非合法 JSON / 缺 `result.message_id` 视为响应异常记 `failed`（不崩溃）。非 2xx HTTP 按 notifier-protocol 的有界重试/失败语义处理。

#### 场景:成功发送记录 message_id

- **当** Bot API 返回 200 + `{"ok": true, "result": {"message_id": 42}}`
- **那么** `NotifyResult.status == "sent"` 且 `detail` 含 `message_id`（字符串化）；日志中不出现明文 bot_token

#### 场景:HTTP 200 但业务失败记 failed 而非 sent

- **当** Bot API 返回 HTTP 200 + `{"ok": false, "error_code": 400, "description": "chat not found"}`
- **那么** `NotifyResult.status == "failed"`（**非** `sent`），`error` 含脱敏后的失败原因；不崩溃
