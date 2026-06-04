## 新增需求

### 需求:飞书 Lark 适配器必须渲染交互卡片 JSON

`LarkNotifier`（`name == "lark"`）必须从 `notifiers/templates/lark/` 加载 Jinja2 模板，产出飞书自定义机器人的**交互卡片**（`msg_type: "interactive"`）JSON。卡片必须按报告聚合 severity 区分视觉（如 header 模板色）并列出 findings 与根因假设。`validate_config` 必须要求 `webhook_url` 字段存在；`secret` 字段可选（提供即启用签名）。渲染的 Report 必须是脱敏后内容。

#### 场景:渲染产出合法 interactive 卡片

- **当** 对一个含 findings 的脱敏 Report 调 `render`
- **那么** `NotifyPayload.body` 必须是可被 `json.loads` 解析的飞书 interactive 卡片结构（含 `msg_type` 与 `card`）

### 需求:Lark 适配器必须按飞书规范做 HMAC-SHA256 时间戳签名

当通道配置含 `secret` 时，`send` 必须按飞书规范计算签名：`sign = base64(HMAC-SHA256(key=f"{timestamp}\n{secret}", msg=b""))`，并在 POST body 中携带 `timestamp` 与 `sign`。`timestamp` 必须是**秒级 Unix epoch**（飞书要求秒级，且与服务端校验窗口对齐；非毫秒），在 body 中以飞书要求的形态（字符串化秒数）携带。必须用标准库 `hmac` / `hashlib` / `base64`（**不引入新依赖**）。未配置 `secret` 时不附签名字段（飞书允许无签名机器人）。日志与 `detail` 中 `webhook_url` / `secret` / `sign` 必须打码。

#### 场景:配置 secret 时附带正确签名

- **当** 通道配 `secret` 且以已知 timestamp 发送
- **那么** body 必须含 `timestamp` 与 `sign`，且 `sign` 等于 `base64(HMAC-SHA256(f"{timestamp}\n{secret}", b""))`（可用固定向量逐字节核对）

#### 场景:未配置 secret 时不附签名

- **当** 通道未配 `secret`
- **那么** POST body 必须不含 `sign` 字段；请求仍正常发出
