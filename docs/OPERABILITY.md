# Hostlens 运维约束（Operability）

> 把 Hostlens **自己**当成一个生产服务来思考：daemon 跑起来后会面对什么？
>
> 本文档定义并发约束、连接复用、API 配额、存储管理、密钥处理、调度行为、健康检查、降级与脱敏策略。
> 这些约束在 M0-M5 实施时分散落实，本文档是单一事实来源；任何 OpenSpec proposal 涉及对应主题必须引用本文档。

---

## 1. 并发约束（Concurrency Budgets）

### 1.1 默认配额

| 维度 | 默认值 | 配置键 | 说明 |
|---|---|---|---|
| 同时巡检的 target 数 | 8 | `concurrency.max_concurrent_targets` | 一次 inspect / 一次定时任务的横向并行上限 |
| 单个 target 上并行的 Inspector 数 | 4 | `concurrency.max_concurrent_inspectors_per_target` | 防止单台机器被同时压垮 |
| 单个 Inspector 的最长 wall-clock | 60s | `concurrency.inspector_timeout_seconds` | 超时取消，结果标记 `timeout` |
| 单个 Agent loop 的最大 turn 数 | 20 | `agent.max_turns` | 防失控 |
| 单个 Agent loop 的最大 token | 100K input + 30K output | `agent.token_budget` | 防失控烧钱 |

### 1.2 背压（Backpressure）

- Inspector Runner 用 `asyncio.Semaphore` 实现 per-target 与全局两级并发门
- Agent 的并行 `tool_use` 调用走同一组信号量 —— **Agent 不能绕过并发约束**
- 信号量等待超过 30s 触发 `BackpressureWarning` 进入报告 meta

---

## 2. SSH 连接复用

### 2.1 必须复用

每个 target 维护一个 **per-process SSH connection pool**：

- 一个 target 同时只开 1 个 control connection（类似 OpenSSH `ControlMaster auto`）
- channel 在该连接上多路复用
- 空闲超过 `ssh.idle_timeout_seconds`（默认 300s）才关闭

### 2.2 健壮性

- 连接中断 → 1 次自动重连（指数退避 1s→4s→16s），再失败 → 该 target 本次巡检全部 Inspector 标记 `target_unreachable`
- 不允许"每个 Inspector 重新 SSH 一次"—— 这是 M1 实施 SSH target 时必须 enforced 的硬约束
- AsyncSSH `tunnel` 配置必须显式禁用 X11 forwarding 与 agent forwarding（最小权限）

---

## 3. Anthropic API 配额与限流

### 3.1 配额预算

- 启动时读取 `agent.api_budget`：`per_minute_input_tokens` / `per_minute_output_tokens` / `per_day_total_tokens`
- 内置 token bucket，预算耗尽时**新巡检请求排队**而非失败（超 5 分钟仍未拿到配额则报 `BudgetExhausted`）
- `hostlens doctor --json` 输出当前配额剩余

### 3.2 限流（429 / 529 / 订阅软限制）处理

- **429**：必须读取 `retry-after` header 并严格 honor
- **529 (overloaded)**：无 retry-after 时固定退避 30s，最多 2 次
- **订阅模式软限制**（仅 `ClaudeSubscriptionBackend`）：无 429 信号，表现为响应变慢 / 内容长度异常 / 静默降质；不可靠重试，只能靠 timeout 兜底 + 触发后整个 Run 标 `degraded_rate_limited` + 强烈提示用户切回 API key backend
- 不允许"立即重试三次"这种暴力策略
- 超过 3 次 429 / 2 次 529 / 1 次订阅软限制检测 → 本次 Agent loop 报 `RateLimited` 并把当前已采集的 finding 写进 partial 报告（见 §6）；详见 [ARCHITECTURE.md §9 Failure Semantics 表](ARCHITECTURE.md#9-agent-loop) 与 §3.4 Backend 选型

### 3.4 Backend 选型与 ToS 风险（关键）

详见 [ARCHITECTURE.md §9 模型层](ARCHITECTURE.md#9-agent-loop)。要点：

| Backend | 生产 daemon | 临时 / dev | 备注 |
|---|---|---|---|
| `AnthropicAPIBackend` | ✓ 默认推荐 | ✓ | `ANTHROPIC_API_KEY`，最简单 |
| `BedrockBackend` | ✓✓ 企业首选 | ✓ | AWS IAM，ToS 干净，CloudTrail audit |
| `VertexBackend` | ✓✓ GCP 企业 | ✓ | GCP Service Account |
| `ClaudeSubscriptionBackend` | ❌ **强制禁止** | ⚠️ 仅 dev/demo | OAuth；`BackendDiagnostics.ensure_safe_for_daemon()` 检测 daemon 模式直接 raise；并发上限 1；账号有被封风险 |

**`ClaudeSubscriptionBackend` 运维红线**：
- daemon 模式启动时如检测到该 backend → 进程直接 exit 1（不只是 warn）
- 配置必须显式 `backend.accept_subscription_risks: true`，否则加载失败
- 不允许并发请求超过 1（单 in-flight call）
- `hostlens doctor` 必须在 banner 中显示该 backend 已激活 + 何时切回 API key

### 3.3 模型降级（待 M2 后评估）

- 计划：高峰期可选 fallback 到 Claude Haiku 跑 Planner，Opus 只在最终诊断使用
- 但默认全程 Opus 4.7，降级是可选项

---

## 4. 存储增长与保留

### 4.1 报告存储

- 默认存 SQLite `~/.local/share/hostlens/reports.db`
- 报告 JSON 用 `zstd` 压缩存 `BLOB`，预期单份 5-50KB
- **保留策略**（可配）：
  - 最近 30 天全保留
  - 30-90 天保留每天最后一份 + 所有 critical
  - 90 天以上仅保留 critical 与人工 pin 标记
- 后台清理任务 daily 00:30 跑

### 4.2 增长上限

- DB 文件超过 1GB 触发 `StorageWarning` 进入 doctor 输出
- 用户可执行 `hostlens reports prune --older-than 90d --dry-run` 手动清理

### 4.3 Audit log

- Remediation audit log 存 `~/.local/share/hostlens/audit.log`，**永远不轮转、永远不删除**
- 如需归档：用户责任，文档明示

---

## 5. Daemon 健康（schedule daemon 模式）

### 5.1 单实例锁（规划，M4 未实现）

- **M4 现状**：未实现单实例锁——M4 按非目标采用「单机单 daemon」假设，不做多 daemon 抢占同一 `schedules/` 目录的互斥（add-scheduler 提案非目标）。
- **规划（尚未实现）**：启动时获取 `~/.local/share/hostlens/scheduler.pid` 文件锁（flock）；已有实例 → 退出 1 + 提示 pid；进程崩溃留下的 stale lock 在进程不存在时自动清理。生产硬化时引入。

### 5.2 健康检查（M4 已落地: doctor + status; HTTP 端点留后续）

- **M4 实际交付的健康面**：`hostlens doctor --json` 的 `checks.schedules`（manifest 加载错误 / 各 job next_fire_time / 最近 N 次 Run 状态分布）+ `hostlens schedule status`（最近 Run 状态分布）。
- **backend 连通性探测超时**：`hostlens doctor` 对 backend 做 `health_check()` ping 时用一个硬超时包裹（默认 **10s**，配置键 `agent.health_check_timeout_seconds`，范围 `1–120s`，env `HOSTLENS_AGENT__HEALTH_CHECK_TIMEOUT_SECONDS`）。用途：防 doctor 因慢 backend（DeepSeek / Qwen via OpenRouter 等推理系）一次 ping >timeout 被误报 `timeout` 或挂死。超时是**信息性**输出（`health_check timeout after {N}s`），**不**翻转 doctor `ready` / exit code；`settings.agent` 缺省时回落默认 10s。
- **规划（尚未实现）**：可选 HTTP `:8765/healthz`（默认绑 `127.0.0.1`），返回 `{"status": "ok", "scheduler_running": bool, "next_fire": ..., "last_run_age_seconds": int, "memory_mb": int}`；内存超过 `daemon.memory_limit_mb`（默认 500MB）触发 `MemoryPressure` 警告。HTTP 端点 + 内存监控留后续里程碑。

### 5.3 优雅停机（M4 已落地）

- **机制（实现，design D-5）**：SIGTERM/SIGINT → `scheduler.pause()`（停止派发新触发）→ `asyncio.wait(in-flight, timeout=grace)` 等当前 job 完成 → 超 grace 的 pending **`task.cancel()`**（单进程 asyncio daemon **不自我 SIGKILL**；`task.cancel()` 是「超 grace 强制停」的进程内实现）→ 主协程 `gather` drain，被强制中断的 in-flight job 落 `Run(status=daemon_stopped)`（shield + drain 保证终态写不丢）。信号 handler 幂等（停机中再收信号忽略）。
- **grace 取值**：默认 `120s`，可经 `daemon.shutdown_grace_seconds`（env `HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS`，范围 `1–600s`）配置；`daemon` / `run` 启动经 `_build_runner` 把该配置值传入 runner。120s 覆盖「单 turn、无重试」的常见基线（单次 LLM API timeout 即 60s，旧的 30s 比单次 timeout 还小、会误切正在等模型的 job）——**不保证**覆盖多 turn / 命中 backend 重试退避的更长 job，那类 job 仍可能超 grace 落 `daemon_stopped`，运维按自身负载经该配置上调缓解。非法值（非数 / 超范围）经 `load_settings()` raise `ConfigError`、命令 fail-loud（不静默回退）。不暴露 CLI `--grace` 旗标（daemon 级参数走 env/config）。与 §6.1 的 `misfire_grace_time` 是独立概念、不共享代码。
- 被 `SIGKILL`（-9 不可捕获）中断的 in-flight job 不留 Run 记录（已知限制，单机内存调度无 start-row WAL）。

---

## 6. 调度器行为（APScheduler 配置）

### 6.1 错过触发（misfire）

- `misfire_grace_time` 固定默认（design D-7）：**cron job = 300s**（5 分钟）、**interval job = `max(30, interval_seconds // 2)`**（取周期一半、下限 30s）。M4 不做 per-manifest 配置。
- 错过窗口：**合并不补跑**（`coalesce=True`，避免 daemon 重启后"补"出几十次巡检洪水）
- 错过的触发记录到 run store，状态 `missed`

### 6.2 并发抑制

- 每个 job `max_instances=1`：同一 schedule 上一次还没跑完，新触发**跳过**而非排队
- 跳过的触发记录到 run store，状态 `skipped_due_to_running`

### 6.3 时区

- Schedule manifest 必须显式 `timezone`，**禁止默认 UTC 或本地时区**
- daemon 启动时打印解析后的时区，方便排错
- DST 切换边界由 APScheduler 处理，但 daily diff 必须用 `timezone-aware datetime`，否则 diff 会错位

---

## 7. 密钥管理与脱敏

### 7.1 密钥来源（优先级递减）

1. 环境变量（`${ENV_VAR}` 在 yaml 中占位）
2. macOS Keychain / Linux Secret Service（M5 后可选）
3. SOPS + age 加密文件（M10 后路线）
4. 明文 yaml（**仅开发用**，doctor 会警告）

### 7.1.1 `.env` 是 env 配置的唯一来源

CLI 根回调启动时会把 **当前工作目录的 `.env`** 一次性加载进 `os.environ`，
故 **所有** env-based 配置共享 `.env` 这一来源——`HOSTLENS_*` 强类型字段
（pydantic `Settings`）、yaml 里的 `${VAR}` 占位（notifiers.yaml / targets.yaml）、
inspector secrets 都能从同一个 `.env` 读到。**所有密钥 / env 统一放 `.env`**，
不必再区分「这个走 `.env` 还是走 `export`」。模板见仓根 `.env.example`。

- **`export` 是覆盖手段**：加载语义是 `override=False`（`os.environ.setdefault` 实现）——已经 `export` 进 shell 的同名
  变量**优先**，`.env` 只填补缺失项。要临时覆盖某个值，`export VAR=...` 即可；
  反之若「改了 `.env` 没生效」，先查是不是被一个残留的 `export` 盖住了。
- **cwd 语义**：`.env` 是 cwd-relative（与 `Settings(env_file=".env")` 一致，
  **不**向上递归查找父目录）。必须**从含 `.env` 的目录运行** `hostlens`
  （daemon 即从 `~/hostlens` 启动），否则该目录无 `.env` 时按真实进程环境运行。
- **不做 key 过滤**：若你在 `.env` 里显式放平台 key（如 `XDG_DATA_HOME`），它会
  被注入并被既有代码读到——这是预期行为，但**别误放**平台 key，除非有意覆盖。
- **缺文件静默**：无 `.env` 的环境零影响（不抛、不打印）。
- **权限**：`.env` 落盘明文密钥，建议 `chmod 0600`。
- **部署收尾**：以前在 daemon wrapper 里 `source .notifier-secrets.env` 把 notifier
  密钥桥接进 `os.environ` 的做法**不再需要**——直接把那些密钥写进 `~/hostlens/.env`，
  删掉 wrapper 里的 `source` 那行即可。

### 7.2 报告与日志脱敏

- 任何写入 SQLite / 日志 / Notifier payload 的字符串都过 `core/redact.py`
- 默认脱敏规则：
  - 任何匹配 `(password|secret|token|api[_-]?key|bearer)\s*[:=]\s*<值>` 的 `key=value` 赋值，以及 HTTP 头 `Bearer <token>`（空格分隔形）。两者的 value 均**引号感知**——`password="a b"` / `Bearer "a b"` 整体脱敏，不发生裸 `\S+` 在引号内空格截断漏尾
  - 任何形如 JWT 的 `eyJ...`
  - 任何匹配 `sk-[a-zA-Z0-9-]{20,}` 的 Anthropic / OpenAI key 形式
  - **[A] 空格分隔长 flag**：`--password <值>` / `--secret <值>` / `--token <值>` / `--api-key <值>`（关键字 casefold 比对，`--password=<值>` 等 `=` 分隔形已由上面 `key=value` 规则覆盖、本条只补空格分隔形；散文中 `--password <普通词>` 会安全侧 over-mask 下一 token，accepted）
  - **[B] 已知工具粘连 / 空格短 flag**：仅当命令头是白名单客户端时按其特有分隔语义脱敏——`mysql` / `mariadb`（仅粘连 `-p<值>`，`-p <值>` 是库名不脱）、`redis-cli`（`-a <值>` / `-a<值>` / `--pass <值>`）、`mongosh` / `mongo`（`-p <值>` / `-p<值>`）、`sshpass`（`-p <值>` / `-p<值>`）、`curl`（`-u user:<值>` / `--user user:<值>`，按 `:` 拆分脱密码段保留 user）；命令头穿透 `sudo` / `env` / `docker exec` / `ssh` 等 wrapper 前缀，未知工具同形 flag 不脱
  - **[C] URL userinfo**：`scheme://user:<密码>@host`（脱密码段保留 `scheme://user:`）与单段 `scheme://<token>@host`（无冒号、token 直接接 `@`，覆盖 `https://ghp_xxx@host` 等 PAT 嵌入形）；scheme 大小写不敏感
  - **[D] 已知 env 名白名单**：`PGPASSWORD` / `MYSQL_PWD` / `REDIS_PASSWORD` / `REDISCLI_AUTH` / `MONGODB_PASSWORD` 等精确名 `=`-锚定赋值；天然排除 `MYSQL_PASSWORD_FILE=/path`（`_FILE` 路径形）与 `PWD=`（工作目录、不在白名单），不放宽到通用 `*PWD*` / `*PASSWORD*`
  - 配置可附加自定义正则
- 脱敏后保留前 4 + 后 4 字符（`sk-xxxx...xxxx`），方便排错。**新增 A/B/C/D 规则同样保留前 4 后 4**（mask 强度分级——凭据类改全 `****`——留独立 follow-up，不在此变更范围）

### 7.3 Notifier 中的脱敏

- Webhook URL 本身不在报告里出现
- 飞书签名 secret、Telegram bot token 永不出现在任何日志（即使 debug level）
- `hostlens notify ...` 命令的 stderr 永不打印 channel 完整配置

---

## 8. Notifier 可靠性合约（M5 已落地）

详见 [ARCHITECTURE.md §6](ARCHITECTURE.md#6-notifier-适配器模式) 与
[operations/notify.md](operations/notify.md)。本节定义运维参数（值与
`notifiers/base.py` 的 `DEFAULT_*` 常量对齐）：

| 维度 | 默认值 | 说明 |
|---|---|---|
| 单 Run 多通道发送并发 | 4 | `asyncio.gather` + `Semaphore(4)`；与巡检采集并发（§1）分离、互不挤占 |
| 单次 HTTP 请求超时 | 10s | 每次 `httpx` 请求的 per-attempt timeout |
| 单通道硬超时（含重试） | 60s | `DEFAULT_CHANNEL_HARD_TIMEOUT_SECONDS`；超时计入 `failed` |
| 失败重试次数 | 3 | `DEFAULT_MAX_ATTEMPTS`（首发 + 最多 2 次重试） |
| 重试退避 | 1s, 2s, 4s + 抖动 | 指数退避 `base * 2**(n-1)` + uniform jitter |
| 限流 429 | honor `Retry-After` | 等待受 60s 硬超时剩余预算封顶；无 header 走退避 |
| 5xx | 退避重试 | 计入重试预算 |
| 4xx（非 429） | 立即 `failed` 不重试 | 400/401 等无重试价值 |
| 消息长度上限 | Telegram 4096 / Lark 卡片体量 | 超长在安全边界截断并标 `truncated=True`，不劈开转义序列 |

**失败语义**：单通道发送失败（5xx/超时/4xx/`only_if` 运行期异常/渲染异常）
仅记入 `Run.notify_results` 的 `NotifyResult(status="failed", error=...)`，
**不**冒泡到 scheduler job 体、**不**改 `RunStatus`、**不**取消其它通道
（`asyncio.gather` 失败隔离）。`error` 字段经 `redact_secret_text` 打码后才
落 `runs.db`，防 token 经异常 str 泄漏。

**重投策略**：M5 **不实现**死信队列 / 持久化重投——失败只留痕（含 error），
重投留后续里程碑（见 add-notifier-channels 提案 §非目标）。至多 3 次重试 +
无去重 = at-least-once 投递，重试可能产生重复消息（accepted risk）。

---

## 9. 降级路径

按"故障域 → 行为"明确每种降级：

| 故障 | 行为 | 用户可见状态 |
|---|---|---|
| Anthropic API 完全宕机（agent 模式） | Planner 跳过，按 manifest 显式列出的 Inspector 跑（如有），输出无根因报告 | report status: `degraded_no_planner` |
| Anthropic API 不可用（deterministic 模式 narrate 阶段） | 采集阶段不接 LLM，已采集 findings 永不丢；narrate 降级 → fleet Report 仍产出（无根因叙述），report status **反映 narrate 降级**（degraded `ReportStatus`，如后端不可用 → `degraded_no_planner`、限流 → `degraded_rate_limited`），**不**掩盖为采集的 `ok`；narrate 成功时才用采集派生的 `ok`/`partial` | run status: `partial`（degraded 类统一映射 partial） |
| deterministic 全队无 inspector 结果可组装 | 落 `failed`（error=`deterministic inspection produced no inspector results`），**不**落 `failed_api_unavailable`（采集阶段不接 LLM） | `RunStatus.FAILED` |
| 部分 Inspector 超时 | 该 Inspector 标记 `timeout`，其余照常 | finding-level: `inspector_status: timeout` |
| 单个 target 不可达 | 该 target 全部 Inspector 标记 `target_unreachable`，其他 target 不受影响 | run status: `partial` |
| Notifier 通道失败 | 其它通道不受影响；失败记入 `Run.notify_results`（不冒泡、不改 RunStatus） | notify_result.<channel>.status: `failed` |
| SQLite 写失败 | 报告临时写到 `~/.local/share/hostlens/orphan_reports/<run_id>.json`，doctor 提示 | doctor: `orphan_reports_count > 0` |
| 配额耗尽（API budget） | 新巡检请求排队 5 分钟；超时则 `RunStatus.BUDGET_EXHAUSTED`（无 Report 产出） | `RunStatus.BUDGET_EXHAUSTED`（详见 ARCHITECTURE.md §7 RunStatus enum） |

---

## 10. 已知限制（诚实清单）

记录"我们没解决"的事项，不假装覆盖：

- **不是高可用调度器**：单实例 daemon。要 HA 调度请用 Kubernetes CronJob + `hostlens inspect`，不要 `hostlens schedule daemon`
- **不是指标存储**：报告是离散事件，不适合做时序分析。把 Hostlens 摆在 Prometheus 旁边
- **不做秒级巡检**：Token 成本 + API rate limit 让秒级巡检不经济
- **离线环境受限**：Anthropic API 必须可达。1.0 后规划本地 LLM 接入，但不在 M0-M10 范围
- **多租户 / RBAC 缺失**：单用户单进程，多人共用请各自跑独立 daemon

---

**相关文档**：
- [ARCHITECTURE.md](ARCHITECTURE.md) —— 架构与设计决策
- [../CLAUDE.md](../CLAUDE.md) —— 设计约定（含写操作硬约束）
- [../TODO.md](../TODO.md) —— 10 期路线（M0-M10）
