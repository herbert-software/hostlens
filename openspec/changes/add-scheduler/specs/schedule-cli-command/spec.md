## 新增需求

### 需求:`hostlens schedule` 必须注册 run / daemon / list / trigger / status 子命令

`hostlens` CLI 必须注册 `schedule` 子命令组，含：

- `schedule list` —— 列出 `schedules/*.yaml` 加载到的所有 manifest，每条显示 `name` / target(s) / 调度表达式 / **`next_fire_time`**
- `schedule run` —— 前台单进程跑调度循环，直到 Ctrl-C（开发/调试用）
- `schedule daemon` —— 常驻 daemon 模式
- `schedule trigger <name>` —— 手动立即触发一次指定 manifest，落 Run + Report，不等定时
- `schedule status` —— 显示最近 N 次 Run 的状态分布（按 manifest 或全局）

所有子命令在执行前必须先经加载器加载 + 校验 manifest（见 schedule-manifest 加载需求）；任一 manifest 非法时这些命令必须 fail-loud 退出非零、stderr 指出文件与原因，**禁止**静默继续。沿用 cli-foundation 约定：非交互环境（无 TTY）禁止 ANSI / spinner、禁止 hang 等 stdin；写操作语义的命令（daemon/run/trigger 启动调度）在缺显式确认的非交互场景必须行为可预测。

#### 场景:list 显示 next_fire_time

- **当** `schedules/` 有合法 manifest，执行 `hostlens schedule list`
- **那么** 必须 exit 0；输出每条 manifest 的 name 与其 `next_fire_time`（人类可读）

#### 场景:非法 manifest 使命令 fail-loud

- **当** `schedules/` 含一个非法 manifest（如 target 未注册），执行 `hostlens schedule list`
- **那么** 必须 exit 非零；stderr 指出出错文件与字段；**禁止** exit 0 静默忽略该文件

### 需求:`schedule trigger` 必须与定时 job 走同一执行体

`schedule trigger <name>` 手动触发一次的执行路径必须**复用与定时 job 完全相同的执行体**（同一 `run_diagnosis_pipeline` 调用 + 同一 RunStatus 映射 + 同一 `ReportStore.save` / `RunStore` 写入），保证手动触发与定时触发产出语义一致。`trigger` 必须在 `<name>` 不存在于已加载 manifest 时 fail-loud 报错。

#### 场景:trigger 立即产 Run 与 Report

- **当** 对一个其 pipeline 会产出 ok Report 的 manifest 执行 `hostlens schedule trigger <name>`
- **那么** 必须落一条 `Run(status=ok, report_id=...)`，且该 Report 可经既有 `hostlens reports show <report_id>` / `reports list <target>` 取回

#### 场景:trigger 未知 name 报错

- **当** `hostlens schedule trigger no-such-name`
- **那么** 必须 exit 非零，错误指出该 name 不存在

### 需求:`schedule status` 必须有明确的输出与选项契约

`schedule status` 必须从 `RunStore` 读取最近 N 条 Run 并展示状态分布：

- 选项：`--name <manifest>`（可选，过滤单个 manifest；缺省为全局所有 manifest）、`--limit <N>: int = 20`（最近条数，默认 20）、`--json`（机器可读输出）。
- 默认（人类可读）：按触发时间倒序列出最近 N 条 Run（含 `run_id` / `schedule_name` / `triggered_at` / `status` / `report_id`）+ 一个状态分布汇总（各 `RunStatus` 计数）。
- `--json`：输出 `{"runs": [...], "status_counts": {<RunStatus>: int}}` 结构，可 `jq` 解析。
- **空历史**（`RunStore` 无记录或该 `--name` 无 Run）：exit 0，人类模式打印「无 Run 记录」提示、`--json` 输出 `{"runs": [], "status_counts": {}}`（**禁止** exit 非零或报错——空历史是正常态）。
- `--name` 指向**未加载的 manifest 名**：fail-loud exit 非零，错误指出该 name 不在已加载 manifest 中（与 `trigger` 未知 name 一致）。

#### 场景:status 默认列最近 N 条 + 状态分布

- **当** `RunStore` 有若干 Run，执行 `hostlens schedule status`
- **那么** 必须 exit 0，按触发时间倒序列出最近 ≤20 条 Run 并给出各 `RunStatus` 计数汇总

#### 场景:status 空历史 exit 0

- **当** `RunStore` 无任何 Run，执行 `hostlens schedule status`（或 `--json`）
- **那么** 必须 exit 0；人类模式打印「无 Run 记录」、`--json` 输出 `{"runs": [], "status_counts": {}}`；**禁止** exit 非零

#### 场景:status --name 未知 manifest 报错

- **当** `hostlens schedule status --name no-such-name`（该 name 不在已加载 manifest）
- **那么** 必须 exit 非零，错误指出该 name 不存在

### 需求:daemon 必须支持 SIGTERM 优雅停机

`schedule daemon`（及 `schedule run`）必须安装 SIGTERM（和 SIGINT）处理：收到信号后**停止接受新触发**（暂停调度派发），**等待当前在跑的 job 完成**并落其真实 RunStatus，然后退出。仅当 in-flight job 在有界 shutdown grace 内未完成而被强制中断时，该 job 必须落 `Run(status=daemon_stopped, report_id=None)`。优雅停机路径（job 在 grace 内自然跑完）**禁止**把正常完成的 job 误记为 `daemon_stopped`。

#### 场景:SIGTERM 等待在跑的 job 完成

- **当** daemon 有一个 in-flight job，进程收到 SIGTERM，且该 job 在 shutdown grace 内完成
- **那么** 该 job 必须落其真实状态（如 `ok`/`partial`/`failed_api_unavailable`），**禁止**记为 `daemon_stopped`；daemon 在该 job 结束后退出

#### 场景:被强制中断的 in-flight job 记 daemon_stopped

- **当** daemon 收到停机信号、in-flight job 超过 shutdown grace 被强制取消
- **那么** 该 job 必须落 `Run(status=daemon_stopped, report_id=None)`，且该行必须**实际持久化到 RunStore、停机流程返回后即可查询**（终态写须 `asyncio.shield` 防 cancel **且**主停机协程在关闭事件循环前 `await` 被 cancel 的 task 跑完以 drain 该 shielded 写）——**禁止**「逻辑上记了但因 task 被 cancel 或 loop 提前关闭而从未写入 runs.db」，**禁止**靠测试端额外 sleep 才能查到

### 需求:daemon 启动必须通过 backend daemon 安全门（经既有 `is_daemon_mode` seam）

`schedule daemon`（及前台常驻的 `schedule run`）启动时必须使 `agent.backend.is_daemon_mode(settings)` 返回 `True`（经 settings 注入 daemon 上下文标志），从而 `create_backend(settings)` 内部**既有的** daemon 安全门自然触发、调用 `BackendDiagnostics.ensure_safe_for_daemon()`。当配置的 backend 为 daemon 不安全者（如 `ClaudeSubscriptionBackend`）时必须 **raise / 拒绝启动**（`BackendDaemonUnsafe` 传播至启动边界、exit 非零），**禁止**仅打印 warning 后继续。**禁止** scheduler 绕过 `is_daemon_mode` seam 在别处平行再调一次 `ensure_safe_for_daemon()`（否则 `create_backend` 既有 gate 成死代码、两处判定可能分叉）。

#### 场景:订阅 backend 下 daemon 拒绝启动

- **当** 配置 backend 为 `ClaudeSubscriptionBackend`，执行 `hostlens schedule daemon`
- **那么** `is_daemon_mode(settings)` 必须为 `True`、`create_backend` 既有 gate 触发、`ensure_safe_for_daemon()` raise；命令必须 exit 非零、明确报错（daemon 模式禁用订阅 backend），**禁止**进入调度循环

#### 场景:前台 run 同样过门

- **当** 配置 backend 为 `ClaudeSubscriptionBackend`，执行 `hostlens schedule run`
- **那么** 同样必须 exit 非零、拒绝进入调度循环（`run` 也常驻跑调度，与 daemon 同等约束）

### 需求:SIGKILL 残留必须有明确的「无 Run 记录」契约（M4 不做 start-row 占位）

Run 记录**必须只在终态写入**——一条 Run 行只在其 `status` 已是某个终态值时落库（job 体跑完落 `ok`/`partial`/`failed*`，或被硬切落 `daemon_stopped`；listener 落 `missed`/`skipped_due_to_running`）。M4 **禁止**在 job 启动时先写一行「进行中 / running」占位 Run（`RunStatus` 恰八值、无「进行中」态，写占位行会破坏「八值对齐 §7」与 `report_id ⇔ status` 不变量）。其直接后果是**契约级限定**：进程被 `SIGKILL`（不可捕获、无 finally）中断的 in-flight job **不产生任何 Run 记录**，该次触发从台账缺失。这是单进程内存调度在无 start-row WAL 下的已知限制，M4 显式接受：`schedule status` / doctor **禁止**声称能标注或恢复此类丢失触发（既不写 start-row，自然也无「进行中」残行可供 stale 标注——故不引入任何 stale/dangling 检测）。SIGKILL 残留的对账/恢复留后续（需 start-row WAL，超出 M4 范围）。

> `finished_at`（`datetime | None`）的取值不在本需求约束内：它由 §7 模型保留 Optional 语义，本需求只约束「不写进行中占位行」，不对各状态的 `finished_at` 是否为 None 作断言（避免与 scheduler-engine spec 的 Optional 字段声明冲突）。

#### 场景:被 SIGKILL 的 in-flight job 不留 Run 记录

- **当** daemon 有一个 in-flight job，进程被 `SIGKILL`（-9）直接杀死
- **那么** 该次触发**不**产生 Run 记录（无法在被杀前持久化）；`schedule status` 输出中**不得**出现该次触发的任何行，也**不得**伪造 `stale` 行——M4 接受此台账缺失为已知限制

#### 场景:不写「进行中」占位行

- **当** 一个 job 正在执行（尚未到达任何终态），此时检视 `RunStore`
- **那么** `RunStore` 中**不得**存在该次执行的「进行中」占位行（Run 行只在终态写入）；`schedule status` 在该 job 完成前看不到它对应的 Run 行

### 需求:daemon 日志必须落文件且经脱敏

`schedule daemon` 运行期日志必须写文件（而非仅 stdout）并经既有 structlog json + 脱敏处理器，确保 API key / 凭据值不进日志（沿用 cli-foundation「doctor 不泄露密钥」「structlog 不打印环境变量值」的同等约束）。日志文件路径必须**确定且可发现**：默认 `~/.local/share/hostlens/logs/scheduler-daemon.log`（与 reports.db / runs.db 同 data 根下的 `logs/` 子目录），路径可经配置/CLI 选项覆盖；daemon 启动时必须在 stderr 打印实际日志文件路径，便于运维定位。

#### 场景:daemon 日志不含密钥原值

- **当** daemon 运行期产生日志
- **那么** 日志文件中**禁止**出现 `ANTHROPIC_API_KEY` 等凭据的真实值
