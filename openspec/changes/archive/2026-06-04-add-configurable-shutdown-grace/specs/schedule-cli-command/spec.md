## 修改需求

### 需求:daemon 必须支持 SIGTERM 优雅停机

`schedule daemon`（及 `schedule run`）必须安装 SIGTERM（和 SIGINT）处理：收到信号后**停止接受新触发**（暂停调度派发），**等待当前在跑的 job 完成**并落其真实 RunStatus，然后退出。仅当 in-flight job 在有界 shutdown grace 内未完成而被强制中断时，该 job 必须落 `Run(status=daemon_stopped, report_id=None)`。优雅停机路径（job 在 grace 内自然跑完）**禁止**把正常完成的 job 误记为 `daemon_stopped`。

**shutdown grace 默认值必须为 `120` 秒**（不再是 `30` 秒）：一次巡检 job = 一整个 Agent loop，单次 LLM API 调用 timeout 即 60s，30s grace 会把正在等模型响应的正常 job 误切成 `daemon_stopped`，120s 修掉这个「grace 比单次 timeout 还小」的明显缺陷。120s 是「比 30s 显著更好」的默认折中（覆盖单 turn、无重试的常见基线 ~90–100s）；**它不保证覆盖多 turn / 含 backend 重试退避的更长 job**——那类 job 仍可能超 grace 被落 `daemon_stopped`，由下文的**可配性**让运维按自身负载上调来缓解。停机响应上界由该配置控制（默认 ≤2 分钟）。

shutdown grace **必须可配置**：经 `hostlens.core.config.Settings.daemon.shutdown_grace_seconds`（`float`，约束范围 `1–600` 秒）读取，env 覆盖名 `HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS`（沿用既有 `HOSTLENS_` 前缀 + `__` 嵌套分隔）。`schedule daemon` / `schedule run` 启动时必须把该配置值传入调度器作为本次停机 grace；非法值（非数 / 超范围）必须经既有 `load_settings()` 路径 raise `ConfigError` 使命令 fail-loud（退出非零、指出字段与范围），**禁止**静默回退到某个默认后继续。**禁止**为 grace 引入 CLI 命令行旗标（daemon 级参数走 env/config）。

#### 场景:SIGTERM 等待在跑的 job 完成

- **当** daemon 有一个 in-flight job，进程收到 SIGTERM，且该 job 在 shutdown grace 内完成
- **那么** 该 job 必须落其真实状态（如 `ok`/`partial`/`failed_api_unavailable`），**禁止**记为 `daemon_stopped`；daemon 在该 job 结束后退出

#### 场景:被强制中断的 in-flight job 记 daemon_stopped

- **当** daemon 收到停机信号、in-flight job 超过 shutdown grace 被强制取消
- **那么** 该 job 必须落 `Run(status=daemon_stopped, report_id=None)`，且该行必须**实际持久化到 RunStore、停机流程返回后即可查询**（终态写须 `asyncio.shield` 防 cancel **且**主停机协程在关闭事件循环前 `await` 被 cancel 的 task 跑完以 drain 该 shielded 写）——**禁止**「逻辑上记了但因 task 被 cancel 或 loop 提前关闭而从未写入 runs.db」，**禁止**靠测试端额外 sleep 才能查到

#### 场景:未配置时使用 120s 默认 grace

- **当** 未设置 `HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS` 且配置文件未给 `daemon.shutdown_grace_seconds`，启动 daemon
- **那么** 停机 grace 必须为 `120` 秒（`Settings.daemon.shutdown_grace_seconds` 的默认值），并被传入调度器用于本次停机

#### 场景:env 覆盖 shutdown grace

- **当** 设置 `HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS=60` 启动 daemon
- **那么** 本次停机 grace 必须为 `60` 秒（配置值传入调度器），daemon 行为据此

#### 场景:非法 grace 配置 fail-loud

- **当** 设置 `HOSTLENS_DAEMON__SHUTDOWN_GRACE_SECONDS=0`（或超出 `1–600` 范围 / 非数值）启动 daemon
- **那么** 必须经 `load_settings()` raise `ConfigError`、命令退出非零，错误指出字段 `daemon.shutdown_grace_seconds` 与期望范围；**禁止**静默用默认值继续启动
