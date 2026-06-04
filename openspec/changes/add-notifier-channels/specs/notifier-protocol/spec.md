## 新增需求

### 需求:Notifier 必须是 Protocol 抽象 + 显式装配的通道类型 registry

`hostlens.notifiers` 必须定义 `Notifier`（`typing.Protocol`，structural）抽象，至少含：`name: str`（通道类型 key，如 `"telegram"`）、`validate_config(cfg) -> None`（启动期校验，配置缺字段/类型错必须 raise）、`render(report, *, severity) -> NotifyPayload`（用 Jinja2 模板产出 channel-native payload）、`async send(payload) -> NotifyResult`（处理重试/限流/签名）。

通道**类型**必须经显式装配函数（如 `register_default_notifiers(registry)`）注册到 `ChannelTypeRegistry` 实例，**禁止** import 时 mutate module-level/global registry（对齐 CLAUDE.md §4.10 规则 3）。**禁止**把 Notifier 注册进 Tool Registry 或塞进 `ToolContext`（它是输出通道、非 Agent capability，CLAUDE.md §4.10 规则 4）。

#### 场景:通过 registry 解析通道类型

- **当** 调用 `register_default_notifiers(registry)` 后用 `registry.get("telegram")`
- **那么** 必须返回对应 Notifier 实现；未注册类型必须 raise（KeyError / 自定义异常），禁止静默返回 None

#### 场景:import notifiers 包不产生注册副作用

- **当** 仅 `import hostlens.notifiers`（不调用装配函数）
- **那么** 全局/module-level 不得出现已注册通道（registry 必须为空或不存在共享单例）

### 需求:`NotifyPayload` / `NotifyResult` 必须是 Pydantic v2 强类型模型

`NotifyPayload` 必须含：`channel: str`（实例名）/ `channel_type: str` / `body: str`（渲染产物）/ `truncated: bool = False`。`NotifyResult` 必须含：`channel: str` / `status: Literal["sent", "skipped", "failed"]` / `error: str | None = None` / `attempts: int = 0` / `detail: dict[str, str] = {}`（平台返回标识如 message_id，**值统一字符串化**——非 str 平台标识如 int message_id 必须 `str()` 后存入以满足 `dict[str, str]`）。`skipped` 表示 `only_if` 求值为假（正常跳过、非错误）；`failed` 表示该通道在**路由（`only_if` 运行期求值）/ 渲染 / 发送**任一环节发生异常（与 notify-routing「`only_if` 运行期求值异常」、scheduler-engine notify 派发隔离面一致，不限于渲染/发送）；`sent` 表示平台接受。**`error` 字段是持久化路径**（落 `Run.notify_results` 进 runs.db），故其内容必须 secret 打码：**禁止**把内嵌 token 的 URL（如 Telegram `…/bot<token>/…`）、webhook URL、或异常 repr 中的 secret 原样塞进 `error`；实现必须先打码再赋值（防 secret 经 exception-derived 字符串泄漏进持久化记录，违 §4.5/§7 与 notify-routing「secret 不入 Run 记录」）。打码**不得**仅依赖通用文本扫描器（如既有 `core/redact.py:redact_text` 只覆盖 `keyword=value` / `Bearer` / JWT / `sk-` 形态，**不**覆盖 URL path 段内嵌的 `bot<token>` / webhook 路径 secret）；实现必须**额外**针对各通道的 URL/secret 形态打码（首选：在异常→error 前对已知的 token/URL 做结构化擦除，而非寄望通用扫描命中），确保 `error` 持久化后**逐字节不含**明文 token / webhook secret。

#### 场景:NotifyResult 区分跳过与失败

- **当** 构造 `NotifyResult(channel="x", status="skipped")` 与 `NotifyResult(channel="x", status="failed", error="timeout")`
- **那么** 两者必须均合法构造且 `status` 字段可机读区分；非枚举值（如 `"ok"`）必须 raise `ValidationError`

#### 场景:error 字段不得泄漏 secret

- **当** 发送因内嵌 token 的请求 URL 超时，异常 repr 含该 URL，构造对应 `NotifyResult(status="failed", error=...)`
- **那么** `error` 中必须不出现明文 token（已打码）；该记录持久化进 runs.db 后仍不含明文 secret

### 需求:render 必须走 Jinja2 模板文件，禁止硬编码模板字符串

每个 Notifier 的 `render` 必须从通道专属模板目录（`notifiers/templates/<channel_type>/`）加载 Jinja2 模板，**禁止**在 Python 代码里硬编码模板字符串（CLAUDE.md §7 反模式）。渲染的 Report 必须是经 `reporting/_redact.py` 脱敏后的内容，Notifier 层**禁止**重新引入未脱敏原始证据。渲染产物超出通道长度上限时必须截断并置 `NotifyPayload.truncated=True`，禁止抛错或发送半成品。**截断必须产出该通道格式上仍合法的产物**：截断点**禁止**劈开转义序列（如 MarkdownV2 的 `\x` 不得截成孤立 `\`）或破坏结构化产物（Lark 卡片 JSON 截断须保持可解析）——即截断与「不破坏通道格式结构」两条约束在边界处必须一致，由各适配器在格式边界对齐截断。长度上限的**计长单位**以各通道 API 实际口径为准（如 Telegram 按 UTF-16 code unit 计 4096），适配器须按该口径而非 Python `len()` code point 判断。极端边界——当不存在任何 ≤上限的合法截断点（如单个不可分原子或通道结构化产物的最小合法骨架本身即超上限）时，**决胜规则为「合法性优先于长度上限」**：适配器必须产出该通道的**最小合法骨架**（如 Lark 空卡片骨架 / Telegram 截到首个合法边界）并置 `truncated=True`，**即使该最小骨架仍超过通道长度上限**（此时容许产物超限——这是「禁止发送非法产物」与「截到上限内」在该病态边界冲突时的明确取舍）；`render` **禁止** raise。若该超限的最小骨架最终被通道 API 拒绝，按发送失败记 `NotifyResult(status="failed")`（不崩溃、不冒泡）。常规场景（存在合法截断点）仍须截到上限内，本决胜规则只适用于无合法截断点的病态边界。

#### 场景:超长内容截断为合法产物而非报错

- **当** 渲染产出超过通道字符上限的 body，且上限边界恰落在一个转义序列/多字节字符中间
- **那么** payload 必须被截断到上限内、`truncated=True`、且产物在该通道格式上仍合法（不含被劈开的孤立转义符）；`render` 不得 raise

### 需求:send 必须有界重试且失败隔离，不向调用方冒泡

`send` 对可达性失败必须有界重试（默认 3 次指数退避，429 尊重 `Retry-After`，但 `Retry-After` 仍受单通道硬超时上限封顶）。**可重试集**：5xx、请求超时（`TimeoutError`）、429、以及网络层瞬态错误（连接被拒 / 重置 / DNS 解析失败等 `httpx` 传输异常）——这类多为瞬态、重试可能恢复。**不可重试集（立即记 `failed`、不重试）**：4xx 中除 429 外（如 400 bad request / 401 invalid token，重试不改结果徒耗预算）、以及非预期的 3xx（通知 endpoint 不应重定向，视为配置/契约异常）。重试耗尽或不可重试失败后返回 `NotifyResult(status="failed", error=...)` 而**非**抛异常。单通道（含重试）必须有硬超时上限（默认 60s）。日志与 `error` 字段中 bot token / webhook URL / 签名等 secret 必须打码。

#### 场景:发送失败返回 failed 而非抛异常

- **当** 目标 API 持续返回 5xx 直到耗尽重试
- **那么** `send` 必须返回 `NotifyResult(status="failed", error=...)`，`attempts` 反映实际尝试次数，且不向调用方抛异常
