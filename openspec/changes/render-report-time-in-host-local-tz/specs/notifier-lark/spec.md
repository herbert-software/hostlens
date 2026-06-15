## 新增需求

### 需求:飞书 Lark 报告渲染的时间必须用宿主机本地时区

飞书 Lark 交互卡片报告的时间（经共享 `fmt_time` 过滤器渲染 `report.meta.timestamp`）**必须**显示为运行 hostlens 的**宿主机系统本地时区**钟点,**禁止**直接打印 UTC 钟点。

- 报告时间戳**存储**仍是 UTC-aware（不变）;只在**渲染**时 `value.astimezone()` 转系统本地 TZ 后格式化。
- `fmt_time` 收到 **naive** datetime 时**必须先按 UTC 解释**再转本地。
- 既有 HMAC-SHA256 签名的**秒级 epoch `timestamp`** 是协议字段（与显示无关）,**不受影响、保持不变**。

#### 场景:卡片时间显示宿主机本地时区而非 UTC
- **当** `report.meta.timestamp` 是 UTC `2026-06-15T08:55:00+00:00`,且本地 TZ 为 `Asia/Shanghai (+0800)`,渲染飞书卡片
- **那么** 卡片时间**必须**渲染为本地钟点 `2026-06-15 16:55`,**禁止**渲染 UTC 的 `08:55`;HMAC 签名的秒级 `timestamp` 字段不变
