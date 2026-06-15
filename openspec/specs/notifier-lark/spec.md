# notifier-lark 规范

## 目的

定义飞书 Lark 通知适配器契约——渲染交互卡片 JSON、按飞书规范做 HMAC-SHA256 时间戳签名。
## 需求
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

### 需求:飞书 Lark 报告卡片必须采用与 Telegram 同构的结构化布局

Lark 交互卡片**必须**以与 Telegram **同构**的信息结构渲染（卡片 JSON 形态）:

- **抬头**:severity 配色的标题区,`Hostlens 巡检 · {target_name} · {中文 severity}`,**禁止**用 `report.intent` 当标题。
- **覆盖行**:`{ok}/{total} 项检查 · {skipped} 项跳过 · {failed} 项失败` + 时间（计数规则与 Telegram 一致:`ok`→ok、`requires_unmet`→skipped、`timeout`/`target_unreachable`/`exception`→failed;不变量 `ok + skipped + failed == total`;`{failed}` 仅 `failed > 0` 时渲染）。
- **根因分析置顶**:有 `hypotheses` 时放在「发现」之前,含每条 `description` 与其 `suggested_actions`。
- **发现**:**去重**(去重键为 `(target_name, inspector_name, message, severity)` **四元组全字段相等才合并**,**禁止**仅 `(inspector_name, message)`——否则误并同 message 不同 severity / target 的独立发现)+ **按 severity 排序** + 每条**带来源** `inspector_name`。
- **健康态**:无 findings 时为「✅ 未发现异常」卡片(不渲空发现区)。
- **多 target**:**按 `finding.target_name` 分组分节**(字段由提案 B 的 add-only `Finding.target_name` 提供,**多 target 分节显式依赖提案 B 落地**)。**退化判据(渲染层自持)**:`distinct(non-None target_name) ≤ 1`（去重后非 None 来源至多一个,含混合盖值/None）**必须无分节**(与既有单 target 一致);**禁**用「全相同或全 None」（混合盖值会误判）。

既有 HMAC-SHA256 时间戳签名、`validate_config`、发送需求**不变**。

#### 场景:卡片与 Telegram 同构
- **当** 渲染同一份报告到 Lark
- **那么** 卡片**必须**含抬头(非 intent)/ 覆盖行 / 根因(置顶)/ 去重排序带来源的发现,信息结构与 Telegram 一致;去重键与多 target 分组逻辑与 Telegram 一致(`(target_name, inspector_name, message, severity)` 四元组去重、按 `finding.target_name` 分节)

#### 场景:同 message 不同 severity 不去重
- **当** 卡片渲染含两条 `inspector_name` 与 `message` 相同、`severity` 不同的 finding
- **那么** 两条**必须各自保留**(去重键含 `severity`,不合并)

#### 场景:去重 × 分节组合（跨主机同 finding 不合并、主机内重复合并）
- **当** 卡片渲染一份含三条 finding 的 report:hostA 与 hostB 各一条 `inspector_name` / `message` / `severity` **相同但 `target_name` 不同**的 finding,外加 hostA 内一条与其首条**四元组完全相同**的重复
- **那么** 去重以 **`(target_name, inspector_name, message, severity)` 四元组**为键、**先于**分节执行:hostA 两条重复**合并为一条**;hostA 与 hostB 的同问题**各自保留**(`target_name` 不同 → 不跨主机合并);hostA 节 1 条、hostB 节 1 条,**禁止**因 message 相同把跨主机两条误并(与 Telegram 同序)

#### 场景:单主机退化为无分节（distinct non-None ≤ 1）
- **当** report 的 finding `target_name` 去重后非 None 值至多一个（含全 None、全同值、或混合盖值/None）
- **那么** 卡片**禁止**渲染主机分节,**必须**与既有单 target 行为一致

#### 场景:健康态卡片
- **当** report 无 findings
- **那么** **必须**渲染「✅ 未发现异常」健康态卡片,**禁止**渲染空的发现区

### 需求:飞书 Lark 报告渲染的时间必须用宿主机本地时区

飞书 Lark 交互卡片报告的时间（经共享 `fmt_time` 过滤器渲染 `report.meta.timestamp`）**必须**显示为运行 hostlens 的**宿主机系统本地时区**钟点,**禁止**直接打印 UTC 钟点。

- 报告时间戳**存储**仍是 UTC-aware（不变）;只在**渲染**时 `value.astimezone()` 转系统本地 TZ 后格式化。
- `fmt_time` 收到 **naive** datetime 时**必须先按 UTC 解释**再转本地。
- 既有 HMAC-SHA256 签名的**秒级 epoch `timestamp`** 是协议字段（与显示无关）,**不受影响、保持不变**。

#### 场景:卡片时间显示宿主机本地时区而非 UTC
- **当** `report.meta.timestamp` 是 UTC `2026-06-15T08:55:00+00:00`,且本地 TZ 为 `Asia/Shanghai (+0800)`,渲染飞书卡片
- **那么** 卡片时间**必须**渲染为本地钟点 `2026-06-15 16:55`,**禁止**渲染 UTC 的 `08:55`;HMAC 签名的秒级 `timestamp` 字段不变
