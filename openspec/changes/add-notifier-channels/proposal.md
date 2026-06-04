## 为什么

M4 调度器已能定时巡检、留痕 Run，但**报告产出后没有出口**：`ScheduleManifest.notify` 与 `Run.notify_results` 都是「parse 早、不消费」的占位（schedule-manifest spec §需求:`notify` 配置在 M4 为惰性占位）。用户的核心诉求是「定时把带根因假设的报告推送到 Telegram / 飞书」——没有 Notifier，整条「意图→采集→诊断→**推送**」链路缺最后一环。

M5 落地 Notifier 抽象 + 适配器，是 CLAUDE.md §4.4 标注的「业务通用化、可扩展」核心证明点：**加一个新通道 = 只新增一个文件、不改主流程**。

## 变更内容

- 新增 **Notifier Protocol** + Channel registry + `NotifyPayload` / `NotifyResult` 类型（host-agnostic 核心）；`validate_config` / `render`（Jinja2）/ `send`（重试 + 限流 + 错误语义）四段契约。
- 新增 **Telegram 适配器**（MarkdownV2 转义、`sendMessage`、bot token + chat_id）。
- 新增 **飞书 Lark 适配器**（交互卡片 payload、HMAC-SHA256 签名校验）。
- 新增 **通道配置加载 + `only_if` 路由**：`~/.config/hostlens/notifiers.yaml`（`${ENV_VAR}` 占位解析）+ 基于报告聚合 severity / finding tags 的路由表达式求值。
- 新增 **`hostlens notify` CLI**：`notify channels`（列出已配置通道）/ `notify test`（向通道发测试消息）/ `notify render`（`--dry-run` 预览渲染产物，不发送）；`doctor --check-channels` 接入通道连通性自检。
- **修改 Scheduler↔Notifier 接线**：runner 在 Report 持久化后消费 manifest 的 `notify`，按 `only_if` 路由到通道并把每通道结果写入 `Run.notify_results`。
- **修改 `Run.notify_results`** 从占位 `list[object]` 收紧为 `list[NotifyResult]`（scheduler-engine spec §28 已预告 M5 收紧）。

## 功能 (Capabilities)

### 新增功能
- `notifier-protocol`: Notifier Protocol、Channel registry、`NotifyPayload` / `NotifyResult` 模型、render(Jinja2)/send 契约与重试/限流/错误降级语义。
- `notifier-telegram`: Telegram 通道适配器（MarkdownV2 渲染 + 转义、Bot API `sendMessage`）。
- `notifier-lark`: 飞书 Lark 通道适配器（交互卡片模板 + HMAC 签名）。
- `notify-routing`: 通道配置加载（`notifiers.yaml` + `${ENV_VAR}` 解析）与 `only_if` 路由表达式求值（报告聚合 severity / finding tags）。
- `notify-cli-command`: `hostlens notify channels/test/render` CLI 与 `doctor --check-channels` 集成。

### 修改功能
- `schedule-manifest`: `notify` 从「M4 惰性占位」改为「M5 被消费」——**校验分两时机**：manifest 加载期只校验 `only_if` 语法（不读 `notifiers.yaml`，保 `schedule list` 不依赖通道配置）；channel 存在性在调度装配期（daemon/run/trigger 注入 channel_registry 时）校验、未知 channel fail-loud。`NotifyConfig` 收紧为 `extra="forbid"`。
- `scheduler-engine`: `Run.notify_results` 由 `list[object]`（占位、恒空）收紧为 `list[NotifyResult]`；runner 在 Report 保存后执行路由 + 发送并落地每通道结果。

## 非目标 (Non-Goals)

- **不实现**钉钉 / 企业微信 / Slack / Email / 通用 Webhook——仅靠 Protocol + registry 为其预留扩展点，本期不写适配器文件。
- **不做** `report.diff_with_last` 的 auto-diff / 把 diff 嵌进推送报告——regression diff 仍是 `hostlens reports diff` 的 post-hoc 操作（保持 M4 约定）。
- **不做** render_html（M3.4 deferred）——通知渲染走各通道 native 格式（MarkdownV2 / Lark 卡片），不依赖 HTML。
- **不把 Notifier 放进 Tool Registry**——它是 Scheduler/Reporter 触发的输出通道，不是 Agent 主动调用的能力（CLAUDE.md §4.10 规则 4）。
- **不动 Agent loop / LLMBackend / prompt caching**——Notifier 不调 LLM，无 token 消耗、无 cache 影响。
- **不实现**通道级失败的自动重投队列 / 持久化死信——失败仅记录进 `Run.notify_results`（含 error），重投留后续里程碑。

## 对外契约影响

- **Notifier Protocol**（新增对外契约）：`Notifier` Protocol + `NotifyPayload` / `NotifyResult` Pydantic 模型，定义在 `notifiers/base.py`。
- **Schedule manifest schema**（行为变更）：`notify` 字段语义从占位变为消费；`only_if` 语法在 manifest 加载期校验，`channel` 在调度装配期解析到已注册通道、未知即 fail-loud（不进入调度，**非**触发后单通道静默跳过）；`NotifyConfig` 收紧为 `extra="forbid"`（M4 的「带 notify 必加载成功且不发送」场景被本期替换）。
- **Scheduler Run schema**（类型收紧）：`Run.notify_results: list[NotifyResult]`（M4 历史 run 记录里该字段为 `[]`，向后兼容，见下）。
- **CLI**（新增命令）：`hostlens notify channels/test/render`、`doctor --check-channels` 扩展。
- 不涉及 Inspector schema / Agent tool schema / MCP tool schema 变更。

### 调度器历史 run 兼容性

M4 写入的 `Run` 记录 `notify_results` 恒为 `[]`（JSON 中为空数组）。收紧为 `list[NotifyResult]` 后，反序列化空数组仍合法；`NotifyResult` 仅出现在 M5 后新写入的记录。`runs.db` schema 不变（`notify_results` 仍序列化为 JSON 列），无迁移。

### 通道配置示例（`~/.config/hostlens/notifiers.yaml`）

```yaml
channels:
  ops-telegram:
    type: telegram
    bot_token: ${TELEGRAM_BOT_TOKEN}
    chat_id: ${TELEGRAM_CHAT_ID}
  ops-lark:
    type: lark
    webhook_url: ${LARK_WEBHOOK_URL}
    secret: ${LARK_SIGN_SECRET}        # 启用签名校验
```

manifest 侧引用 + 路由：

```yaml
# schedules/nightly-cpu.yaml 片段
notify:
  - channel: ops-telegram
    only_if: "severity >= warning"      # 仅 warning/critical 才推
  - channel: ops-lark
    only_if: "'disk_full' in tags"      # 命中特定 finding tag 才推
```

### Jinja2 模板示例（`notifiers/templates/telegram/report.md.j2`）

```jinja2
*{{ report.intent | default("巡检报告") | mdv2_escape }}*
目标 `{{ report.target_name | mdv2_escape }}` · severity *{{ severity }}*

{% for f in findings %}
{{ f.severity | sev_icon }} {{ f.message | mdv2_escape }}
{%- endfor %}

{% if report.hypotheses %}*根因假设*
{% for h in report.hypotheses %}• {{ h.description | mdv2_escape }} ({{ h.confidence }})
{% endfor %}{% endif %}
```

## 影响

- **新增代码**：`src/hostlens/notifiers/{base.py,telegram.py,lark.py,routing.py,config.py}` + `notifiers/templates/{telegram,lark}/*.j2`；`cli/notify.py`。
- **改动代码**：`scheduler/runner.py`（保存 Report 后接 notify 派发）、`scheduler/store.py`（`Run.notify_results` 类型收紧 + 反序列化）、`cli/doctor.py`（`--check-channels`）、`core/config.py`（`notifiers_config_path`）。
- **新增依赖**：显式新增 `httpx>=0.27,<1`（事实上已由 `anthropic` 传递依赖在场，本期在 `pyproject.toml` 显式声明、不靠传递；见 design D-5）。`Jinja2` / `simpleeval` 已在锁定技术栈复用；HMAC 走标准库 `hmac`/`hashlib`/`base64`，无其它新增。
- **测试**：Telegram / Lark 适配器用 `httpx` mock transport（不打真实 API）；签名校验用已知 secret 的固定向量；路由表达式用真实 Report fixture。

## Failure Modes

1. **通道配置缺失 / `${ENV_VAR}` 未设置 / 未知 channel 或 type**：在**调度装配期**（加载 `notifiers.yaml` + 注入 channel_registry）`validate_config` / 解析报错 fail-loud（未配置的通道不进入调度，而非触发后静默跳过），`doctor --check-channels` 标红。注意 `schedule list`（纯加载、不读 notifiers.yaml）不受此影响。装配成功后的**运行期**发送失败（5xx/超时等）才记入 `Run.notify_results` 的 `error`，不影响其它通道与 Report 持久化。
2. **目标 API 不可达 / 超时**（Telegram/Lark 服务端 5xx 或网络超时）：适配器按有限次退避重试（默认 3 次），仍失败则记录 `NotifyResult(status="failed", error=...)`，**不**阻塞 Run 完成、**不**抛到 scheduler job 体（report 已留痕）。
3. **限流 429**：尊重 `Retry-After`，退避重试；超出重试预算降级为 `failed` 并记录。
4. **`only_if` 表达式非法**：表达式解析失败 → 加载期报错（manifest 校验失败），不在运行期静默跳过（避免「以为会推其实没推」）。
5. **模板渲染异常**（字段缺失 / 模板语法错）：渲染失败记 `failed` + error，不发送半成品；其它通道不受影响。

## Operational Limits

- **并发**：单次 Run 的多通道发送并发上限默认 4（`asyncio.gather` + semaphore）；与巡检采集并发预算（docs/OPERABILITY.md §1）分离，互不挤占。
- **超时**：每通道单次 HTTP 请求默认 10s，整通道（含重试）硬上限 60s；超时计入 `failed`。
- **重试**：默认 3 次指数退避（1s/2s/4s + 抖动），仅对幂等的「发送一条消息」语义安全（无去重，重试可能导致重复消息——记为 accepted risk）。
- **内存**：渲染产物为字符串，单条消息受各通道长度上限约束（Telegram 4096 字符、Lark 卡片体量限制）——超长截断并标注 `truncated`。

## Security & Secrets

- **新增密钥**：`TELEGRAM_BOT_TOKEN` / `LARK_WEBHOOK_URL` / `LARK_SIGN_SECRET` 等，**只**经 `${ENV_VAR}` 占位从环境注入，**禁止**写进 `notifiers.yaml` 明文或 commit（继承 §7 反模式）。
- **脱敏**：推送的报告内容复用既有 `reporting/_redact.py` 脱敏后的 Report，不在 Notifier 层重新引入未脱敏证据；日志中 bot token / webhook URL 必须打码。
- **签名**：Lark 适配器实现 HMAC-SHA256 时间戳签名（防伪造），是飞书自定义机器人的安全要求。
- **攻击面**：仅新增**出站** HTTPS 请求到用户配置的固定 endpoint，不开监听端口、不接收入站。Notifier **不是写操作**（不改远端被巡检主机状态），故不触发 §4.5 的 plan→approve→execute 与 EUID==0 拒绝（那是 Remediation 的约束）。`notify test` 会真实外发一条消息——非交互无 TTY 缺 `--yes` 直接退出 1。

## Cost / Quota Impact

- **零 Anthropic token 消耗**：Notifier 不调 LLM（CLAUDE.md §4.2 红线：输出通道不推理）。无 prompt caching 涉及。
- **外部 API 配额**：Telegram Bot API / 飞书机器人有各自速率限制（Telegram ~30 msg/s 全局、Lark webhook 每秒数十次），M5 单 Run 单通道单消息远低于阈值；限流由 §Operational Limits 的退避兜底。

## Demo Path

5 分钟、无真实 token、无付费 API 的本地复现：

1. `hostlens notify render --report <已有 report_id> --channel <fake-telegram> --dry-run` → 用 `reports/` 下既有持久化 Report，渲染出 Telegram MarkdownV2 / Lark 卡片 JSON 到 stdout，**不外发**，肉眼验证模板与 `only_if` 路由判定。
2. 单元/集成测试用 `httpx` mock transport 断言适配器构造的请求体与签名正确（CI 必跑，不依赖网络）。
3. 可选真实烟测：设 `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` 后 `hostlens notify test --channel ops-telegram --yes` 向自己的测试群发一条，验证端到端。
