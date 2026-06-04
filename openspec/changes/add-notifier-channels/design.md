## 上下文

M4 调度器把巡检报告持久化进 `reports/` 并留痕 `Run`，但 `notify` / `notify_results` 是占位（schedule-manifest spec §99、scheduler-engine spec §28）。M5 补齐「报告→推送」的最后一环。

现状关键事实（决定本设计的复用面）：
- `notifiers/` 仅有空 `__init__.py`，零实现。
- **`simpleeval>=1.0` 与 `jinja2>=3.1` 已是锁定依赖**——`only_if` 路由与模板渲染**不引入新依赖**。
- 已有 `src/hostlens/inspectors/dsl.py`：硬化的 simpleeval 表达式求值器（`validate_ast` 静态 AST 闸门 + `evaluate(expr, context, timeout)` 带超时），用于 inspector finding DSL。`only_if` 路由**直接复用**它，不另造求值器。
- `Report` 字段 frozen：`findings: list[Finding]`，`Finding.severity: Literal["info","warning","critical"]`、`Finding.tags: list[Tag]`（lowercase token）。**Report 无报告级聚合 severity**——需派生。
- `reporting/_redact.py` 已做证据脱敏；Notifier 复用脱敏后的 Report，不重新引入原始证据。
- runner（`scheduler/runner.py`）job 体在 `ReportStore.save` 后构造终态 `Run`——这是 notify 派发的唯一接线点。
- 依赖注入纪律：runner 通过构造器注入依赖（无 module-level singleton）；Notifier registry 同此模式注入。

## 目标 / 非目标

**目标：**
- Notifier Protocol + Channel registry + `NotifyPayload` / `NotifyResult` 类型，使「加一个通道 = 加一个文件」成立。
- Telegram（MarkdownV2）+ 飞书 Lark（卡片 + HMAC 签名）两个适配器。
- `only_if` 路由（复用 inspector DSL 求值器）+ `notifiers.yaml` 通道配置（`${ENV_VAR}` 解析）。
- Scheduler→Notifier 接线：report 保存后路由 + 并发发送 + 每通道结果落 `Run.notify_results`。
- `hostlens notify channels/test/render` CLI + `doctor --check-channels`。

**非目标：**
- 钉钉/企业微信/Slack/Email/Webhook 适配器（仅留扩展点）。
- 失败重投队列 / 死信持久化（失败仅记入 `Run.notify_results`）。
- render_html、auto-diff 嵌入推送、把 Notifier 塞进 Tool Registry。

## 决策

### D-1：Notifier 是 `Protocol` + 显式装配的 registry，不是 ABC 继承树
`Notifier` 用 `typing.Protocol`（structural），与 `ExecutionTarget` / `LLMBackend` 一致：

```python
class Notifier(Protocol):
    name: str                                                  # channel type key, e.g. "telegram"
    def validate_config(self, cfg: dict[str, object]) -> None: ...
    def render(self, report: Report, *, severity: Severity) -> NotifyPayload: ...
    async def send(self, payload: NotifyPayload) -> NotifyResult: ...
```

通道**类型**注册到 `ChannelTypeRegistry`（`telegram`→`TelegramNotifier`、`lark`→`LarkNotifier`），仿 inspector registry 的显式装配（`register_default_notifiers(registry)`），**禁止** import 时 mutate 全局。通道**实例**由 `notifiers.yaml` 的每个 `channels.<name>` 条目构造，注入 runner。
- **理由**：Protocol 让「加通道」零侵入主流程；显式装配避免 service-locator（CLAUDE.md §4.10 反模式）。
- **替代**：ABC + `register()` 装饰器副作用——被 §4.10 规则 3 否决（不得 import 时 mutate 全局）。

### D-2：`NotifyPayload` / `NotifyResult` 为 Pydantic v2 模型
```python
class NotifyPayload(BaseModel):       # channel-native 渲染产物
    channel: str                      # 实例名（notifiers.yaml 的 key）
    channel_type: str                 # "telegram" / "lark"
    body: str                         # 渲染后的文本/JSON（telegram=MarkdownV2 str；lark=card JSON str）
    truncated: bool = False

class NotifyResult(BaseModel):        # 单通道发送结果，进 Run.notify_results
    channel: str
    status: Literal["sent", "skipped", "failed"]
    error: str | None = None
    attempts: int = 0
    detail: dict[str, str] = {}       # 平台返回的 message_id 等（已打码）
```
- `skipped`：`only_if` 求值为假（正常路由跳过，非错误）。`failed`：路由（`only_if` 运行期求值）/ 渲染 / 发送任一环节异常。`sent`：平台接受（HTTP 2xx 且 body 业务成功，如 Telegram `ok==true`）。
- **理由**：强类型 `NotifyResult` 让 scheduler-engine spec §28 预告的「M5 收紧 `list[object]`→`list[NotifyResult]`」落地，且 Run 留痕可机读。

### D-3：`only_if` 路由复用 `inspectors.dsl.evaluate`，severity 映射为有序 rank
路由上下文：
```python
_SEV_RANK = {"info": 0, "warning": 1, "critical": 2}
context = {"severity": _SEV_RANK[report_severity], **_SEV_RANK, "tags": sorted_all_tags}
result = bool(await dsl.evaluate(only_if, context, timeout_seconds=1.0))
```
于是 `severity >= warning`（数值比较）与 `'disk_full' in tags`（成员）都能用既有硬化求值器。`only_if` 缺省（`None`）= 恒发送。
- **报告级 severity 派生**：`max(finding.severity)` over all findings；无 finding → `info`。派生函数放 `notifiers/routing.py`（非 Report 模型——Report frozen 且 §4.4 路由属 Notifier 域）。
- **加载期校验**：manifest 加载时对每个 `only_if` 跑 `dsl.validate_ast`，非法表达式 fail-loud（Failure Mode 4），不留到运行期。
- **理由**：复用已审计的 AST 闸门（防 `__import__` 等）零成本拿到安全求值；severity rank 让字符串枚举可比较。
- **替代**：自写 mini 解析器——重复造轮子且要重新做安全审计，否决。

### D-4：渲染走 Jinja2 模板文件 + 通道专属 filter，禁止硬编码模板串
每通道一套模板目录 `notifiers/templates/{telegram,lark}/`。Telegram 注册 `mdv2_escape`（转义 MarkdownV2 保留字符 `_*[]()~\`>#+-=|{}.!`）、`sev_icon` filter；Lark 模板产出卡片 JSON。`render` 用 `jinja2.Environment(loader=PackageLoader(...), autoescape=False)`（通道格式非 HTML，转义靠通道专属 filter）。
- **理由**：CLAUDE.md §7 反模式「在 Notifier 里硬编码模板字符串」；filter 把通道转义规则收敛在一处。

### D-5：HTTP 用 `httpx.AsyncClient`，显式声明依赖
`anthropic` 已传递依赖 httpx，故 httpx 事实在场；本期在 `pyproject.toml` **显式**加 `httpx>=0.27,<1`（不靠传递依赖）。适配器构造 `httpx.AsyncClient`，测试用 `httpx.MockTransport` 断言请求体/签名，**不打真实 API**。
- **替代**：stdlib `urllib`（非 async，违 §6 async-first）/ aiohttp（多一个依赖且与 anthropic 的 httpx 重复）——均否决。

### D-6：Lark 签名 = HMAC-SHA256(timestamp + "\n" + secret) 的 base64
按飞书自定义机器人规范：`sign = base64(hmac_sha256(key=f"{timestamp}\n{secret}", msg=b""))`，随 payload 发 `{"timestamp","sign",...card}`。用标准库 `hmac`/`hashlib`/`base64`，无新依赖。secret 缺省（未配 `secret`）= 不签名（飞书允许无签名机器人）。

### D-7：Scheduler 接线点 = runner job 体 `ReportStore.save` 之后
job 体保存 Report 得 `report_id` 后，调 `notify_dispatch(report, manifest.notify, channel_registry)` → 返回 `list[NotifyResult]` → 写入终态 `Run.notify_results`。
- **失败隔离**：notify 异常**绝不**冒泡到 job 体外（report 已留痕）；每通道独立 try，失败记 `NotifyResult(failed)`。整个 notify 阶段包在 `suppress`-风格边界，最坏情况 `notify_results` 记错误但 Run 仍 `ok`/`partial`。
- **并发**：`asyncio.gather` + `Semaphore(4)`（Operational Limits）；通道间不互相阻塞。
- **触发条件**：仅当 job 体产出了 Report（`ok`/`partial`）才派发；`failed_*`（无 Report）不派发（无内容可推）。
- **DI**：`channel_registry` 经 runner 构造器注入（与现有 `RunStore`/`ReportStore`/`backend_factory` 同列），daemon/`schedule run`/`trigger` 共用装配函数。

### D-8：`Run.notify_results` 类型收紧 + 历史兼容
`Run.notify_results: list[NotifyResult] = []`（替换占位 `list[object]`）。`runs.db` 仍把它序列化为 JSON 列，schema 不变。M4 写入的空数组 `[]` 反序列化合法；`NotifyResult` 仅出现于 M5 后新记录——**无迁移脚本**。
- **风险点**：若某 M4 记录里 `notify_results` 非空（不可能——M4 恒空），反序列化会 fail——但 spec 保证 M4 恒空，故安全。

### D-9：`hostlens notify` CLI 三子命令 + doctor 集成
- `notify channels [--json]`：列出 `notifiers.yaml` 已配置通道 + 类型 + 配置校验状态（不外发）。
- `notify render --report <id> --channel <name> [--only-if-skip]`：渲染并打印 payload 到 stdout，**不发送**（Demo Path 主路径）。`--dry-run` 是默认且唯一行为。
- `notify test --channel <name> [--yes]`：真实外发一条测试消息。**写/外发语义**——非交互无 TTY 缺 `--yes` 直接 `exit 1`（继承全局 §write-op 约束；但 notify 不改远端被巡检主机状态，故**不**触发 Remediation 的 EUID==0 拒绝）。
- `doctor --check-channels`：对每个通道做轻量连通性探测（Telegram `getMe` / Lark 不外发只校验配置完整性），结果进 `doctor --json` 的 `checks.channels`。

## 风险 / 权衡

- **重试导致重复消息**（无幂等去重）→ 限 3 次退避 + 仅对「发一条消息」语义；重复记为 accepted risk（文档标注），死信/去重留后续。
- **`only_if` 表达式安全** → 复用 `dsl.validate_ast` 已审计闸门；不暴露任何 callable/attribute；1s 求值超时。
- **secret 泄漏进日志** → 适配器日志对 bot_token / webhook_url / sign 强制打码；`${ENV_VAR}` 仅运行期解析，不落盘、不进 Run 记录。
- **通道长度上限**（Telegram 4096 / Lark 卡片体量）→ 渲染后超长截断并置 `truncated=True`，不抛错。
- **notify 阻塞 Run 完成** → 整通道含重试硬上限 60s + 失败隔离不冒泡；最坏拖慢单次 Run ≤ 60s，不影响下次调度（misfire_grace_time 已有）。
- **httpx 显式依赖与 anthropic 传递版本冲突** → pin `>=0.27,<1` 与 anthropic 兼容区间取交集；CI 锁文件验证。

## 迁移计划

无数据迁移（`runs.db` schema 不变、M4 记录空数组兼容）。部署即生效：用户新建/编辑 `~/.config/hostlens/notifiers.yaml` 并在 manifest `notify` 引用通道。回滚：移除 manifest `notify` 条目即停止推送；代码回滚无遗留状态。

## 待解决问题

- Telegram 长消息是「截断」还是「拆分多条」？M5 取**截断 + truncated 标记**（拆分留后续），design 已锁。
- 是否支持单通道多接收人（多 chat_id）？M5 取**一通道一接收人**，多接收人配多通道条目（保持配置模型简单）。
- `notify test` 的测试消息内容是固定模板还是渲染最近一次 Report？M5 取**固定 ping 模板**（不依赖已有 report），降低 test 前置条件。
