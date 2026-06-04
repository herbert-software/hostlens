## 为什么

Hostlens 的一句话愿景是「**按调度定时把报告推送到 Telegram / 飞书**」。M1–M3 已交付「理解意图 → 并行采集 → 关联诊断 → 忠实 Report（含根因假设 + 回归 diff）」的完整链路，但目前**只能由人手动 `hostlens inspect --intent` 触发**。M4 补上「**何时自动跑**」这一环：让 Hostlens 按 cron / interval 自动跑巡检、把结果持久化成可追溯的 Run 记录、并以 daemon 形态稳定常驻、优雅停机。

这是点亮愿景端到端闭环的前半（后半 M5 Notifier 负责「推给谁」）。按 CLAUDE.md §4.6「Scheduler 不是黑盒」：调度任务 YAML 是 SOT、每次触发留痕（run_id / 触发时间 / 目标 / Inspector 集合 / 报告 hash / 结果）、`schedule list` 能看 next_fire_time、daemon 支持 SIGTERM 优雅停机。技术栈锁定 APScheduler 进程内调度（不要 cron + shell 包脚本，进程内可控可测）。

## 变更内容

- **Schedule manifest（SOT）**：新增 `scheduler/schema.py` 的 Pydantic v2 `ScheduleManifest`（`name` / `schedule`（含 cron|interval 二选一 + `timezone`，均在 `ScheduleSpec` 内）/ `targets` / `intent` 必填 / `inspectors` 可选 hint / `report` 配置（`format: Literal["md","json"]="md"` 消费；`diff_with_last: bool` **M4 仅解析不消费**，auto-diff/嵌入报告为 M5 非目标，post-hoc 用既有 `reports diff`）/ `notify` 配置**仅占位**）；`scheduler/loader.py` 扫 `schedules/*.yaml`、**加载时**校验（cron/interval 互斥且至少其一、cron 限 5 段、interval 至少一字段为正、timezone 合法、targets 在 TargetRegistry 中存在且恰 1 个、intent 非空）。
- **调度引擎**：新增 `scheduler/runner.py` 基于 APScheduler `AsyncIOScheduler`，每个 manifest 注册为一个 job；job 执行 = **复用 M3 已落地的 `run_diagnosis_pipeline`**（Planner→Diagnostician→忠实 `Report`）+ 经 **`reporting/store.py` `ReportStore.save`** 持久化 Report。固定策略：每 job `max_instances=1`（上次未跑完 → 本次记 `skipped_due_to_running`、不启动）、`coalesce=True`（积压的多次触发合并为一次）、`misfire_grace_time` 有限（超窗 → 记 `missed`）。
- **Run 记录**：新增 `scheduler/store.py` 的 `RunStatus` 枚举（**新建**，对齐 docs/ARCHITECTURE.md §7 八值：`ok` / `partial` / `budget_exhausted` / `missed` / `skipped_due_to_running` / `failed_api_unavailable` / `failed` / `daemon_stopped`）+ `Run` 模型（`run_id` / `schedule_name` / `triggered_at` / `started_at`(可空) / `finished_at`(可空) / `status` / `report_id`(可空) / `error`(可空) / `notify_results: list[object]`(预留占位、本提案恒空 `[]`；M4 用 `list[object]`——`NotifyResult` 属 M5 Notifier 契约当前不存在、裸 `list` 触 mypy §6 Any 红线，M5 收紧为 `list[NotifyResult]`) + **留痕字段**（CLAUDE.md §4.6 / ARCHITECTURE §7 第 819 行 who/when/target/inspectors/report_hash）：`targets: list[str]`(本次触发的 target 集合) / `inspectors: list[str]`(本次实际跑的 inspector 集合快照；有 Report 时从 `report.meta.inspectors_used`(`list[InspectorRun]`) 的 `.name` 投影，job 未启动状态为 `[]`) / `report_hash: str | None`(**完整性锚点**：`sha256(reporting.render_json.render(report).encode()).hexdigest()`，事后校验 reports.db 内容未被篡改/对应本 Run；同一 Report 确定性、非跨运行去重键；无 Report 时 None) / `report_storage: Literal["db","orphan"] | None`(报告存储形态，见下文 orphan 处理；无 Report 时 None)）+ Run 持久化 store。**四条硬不变量**（`model_validator` 强制，详见 design D-3 / spec）：① `Run.report_id is None` **当且仅当** `Run.status not in {ok, partial}`；② `Run.started_at is None` 当 `Run.status in {missed, skipped_due_to_running, budget_exhausted}`（对齐 §7 第 842 行三值集合）；③ `report_hash is not None` 仅 `status ∈ {ok, partial}`；④ `report_storage is not None` 当且仅当 `status ∈ {ok, partial}`。
- **Daemon CLI**：新增 `cli/schedule.py` 子命令 `run`（前台单进程跑到 Ctrl-C）/ `daemon`（常驻）/ `list`（含 `next_fire_time`）/ `trigger <name>`（手动立即触发一次）/ `status`（最近 N 次 Run 状态分布）。SIGTERM 优雅停机（机制见 design D-5）：runner 维护 in-flight job task 集合 → 收信号 `scheduler.pause()`（停止派发新触发）→ `asyncio.wait(inflight, timeout=GRACE_SECONDS)` 等当前 job 跑完 → 超 grace 的 pending `task.cancel()` → 主协程 `asyncio.gather(*pending, return_exceptions=True)` drain（让被硬切 job 的 shield 终态写跑完再关 loop）→ `scheduler.shutdown(wait=False)`。**不**用 `asyncio.wait_for(scheduler.shutdown(wait=True))`（APScheduler `shutdown()` 同步返回 None、非 awaitable，且同 loop 内 `wait=True` 会自等死锁）。仅超 grace 被强制 cancel 的 in-flight run 落 `daemon_stopped`（优雅跑完落真实状态）。被 `SIGKILL`（-9 不可捕获）中断的 in-flight job 不产生 Run 记录（已知限制，见 Failure Modes / Operational Limits）。`daemon` 与前台 `run`（同样常驻跑调度）启动时**必须**让 `is_daemon_mode(settings)` 返回 True（经 settings 注入 daemon 上下文标志），从而 `create_backend` 内部既有的 daemon 安全门自然 fire、调 `BackendDiagnostics.ensure_safe_for_daemon()`（`ClaudeSubscriptionBackend` 强制 raise，CLAUDE.md §4.11 规则 3）——复用既有 gate seam，不在 scheduler 里平行再判一次。daemon 日志写文件（默认 `~/.local/share/hostlens/logs/scheduler-daemon.log`，可配置覆盖，启动时 stderr 打印实际路径）+ structlog json 脱敏。
- **doctor 集成**：`hostlens doctor` 增 schedule 健康检查（manifest 加载错误、各 job `next_fire_time` 合理性、最近 N 次 Run 状态分布），作为 cli-foundation 既有 `checks` 命名空间下的 **add-only 新 check_id `checks.schedules`**（status 复用既有 5 值枚举：manifest 加载失败 → `error`、否则 `ok`；next_fire_time / Run 状态分布塞进该 check 的 `detail` 或 add-only optional 字段），**不**新增顶层字段、**不**破坏封闭顶层 schema `{version,timestamp,checks,ready}`、**不** bump version，`--json` 经 `.checks.schedules` 可见。

非破坏性：不改任何既有 wire / 契约（不动 `Report` 模型、不动 `ReportStore` 签名；`run_diagnosis_pipeline` 行为不变——runner 仅复用其**既有可选** `planner_result_sink` 形参拿 `terminal_status` 做无-Report 判别，该形参默认 no-op、不改既有 `--intent`/`demo` 行为；不动 ToolContext / LLMBackend）。纯新增 scheduler 子系统 + 一个新 CLI 命名空间 + 一个 add-only doctor check。

## 功能 (Capabilities)

### 新增功能

- `schedule-manifest`: `ScheduleManifest` Pydantic schema（cron/interval 互斥、timezone、targets、intent 必填、inspectors hint、report/notify 占位）+ `schedules/*.yaml` 加载器与加载时校验规则。
- `scheduler-engine`: APScheduler `AsyncIOScheduler` 封装（每 manifest 一 job、job 复用 `run_diagnosis_pipeline` 产 Report 并 `ReportStore.save`、misfire/coalesce/max_instances 策略）+ `RunStatus` 八值枚举 + `Run` 模型 + Run 记录持久化 + `report_id ⇔ status` 不变量。
- `schedule-cli-command`: `hostlens schedule` 子命令族（run / daemon / list / trigger / status）+ SIGTERM 优雅停机语义 + daemon 启动的 backend 安全门（`ensure_safe_for_daemon`）。

### 修改功能

- （doctor 的 schedule 健康检查为 additive 代码。已确认 `cli-foundation` spec 第 44–46 行规约了 doctor `--json` 封闭 schema `{version,timestamp,checks,ready}` 且其演进政策明示「在既有 `checks` 内 add-only 新增 check_id / optional 字段不 bump version、不 breaking」。本提案把 schedule 健康落为 `checks.schedules` 这一**新 check_id**（status 用既有 5 值枚举、附加信息走 optional 字段），**完全落在 add-only 政策内 ⇒ 无需 `cli-foundation` MODIFIED delta**。若实现期发现必须改 required 字段或加顶层字段，则该改动是 breaking、必须补 `cli-foundation` MODIFIED delta。）

## 影响

- Affected specs: `schedule-manifest`（ADDED）、`scheduler-engine`（ADDED）、`schedule-cli-command`（ADDED）。doctor 集成落 `checks.schedules`（cli-foundation add-only 政策内）⇒ **不**产 `cli-foundation` MODIFIED delta。
- Affected code:
  - `src/hostlens/scheduler/schema.py`（新）—— `ScheduleManifest` 及子模型
  - `src/hostlens/scheduler/loader.py`（新）—— `schedules/*.yaml` 扫描 + 加载时校验
  - `src/hostlens/scheduler/runner.py`（新）—— `AsyncIOScheduler` 封装、job 执行、misfire/coalesce/max_instances 策略、SIGTERM 协作
  - `src/hostlens/scheduler/store.py`（新）—— `RunStatus` / `Run` / Run 记录 store
  - `src/hostlens/cli/schedule.py`（新）—— `schedule` 子命令族
  - `src/hostlens/cli/doctor.py`（改）—— 增 `_check_schedules` 检查 + 接入 `_build_report` / 渲染（落 `checks.schedules`）
  - `src/hostlens/orchestration/pipeline.py`（新）+ `cli/_intent.py`（改：重导出，见 design D-2）—— `run_diagnosis_pipeline` 物理上提（零行为变更）
  - 复用（**行为不改**）：`reporting/store.py:ReportStore`（含 `SaveResult.stored_as_orphan` 边界，见 design D-11）、`agent/backend.py:BackendDiagnostics.ensure_safe_for_daemon` / `is_daemon_mode`（daemon/run 启动翻转其为 True）、`targets` registry
  - 依赖新增：`APScheduler`（pyproject runtime dep）
- 对外契约影响：新增 `schedules/<name>.yaml` manifest schema（新 SOT）、新增 `hostlens schedule *` CLI、新增 `RunStatus` 枚举与 Run 记录格式。**不**改既有 Report / ReportStore / Inspector / Agent / Notifier 契约。
- Migration: 无配置迁移（纯新增）。Run 记录是新存储；Report 仍写既有 `~/.local/share/hostlens/reports.db`。

### Failure Modes

| 故障场景 | 表现 | 降级行为 |
|---|---|---|
| 上次同名 job 还在跑 | 新触发到来 | `max_instances=1` 拒启 → 记 `Run(status=skipped_due_to_running, report_id=None)`，不并发跑同 target |
| 错过触发窗口（机器休眠 / daemon 卡顿超 `misfire_grace_time`） | APScheduler misfire | 记 `Run(status=missed, report_id=None)`，`coalesce` 把积压合并为一次 |
| Agent loop API 不可用（重试耗尽） | pipeline `planner_result.loop_result.terminal_status == "failed_api_unavailable"` 且返回 `None` | 经 `planner_result_sink` 拿到 terminal_status 判别 → 记 `Run(status=failed_api_unavailable, report_id=None)`（沿用 §7 边界，与 ReportStatus 独立） |
| pipeline 到达 API 但零 InspectorResult（模型从未调 `run_inspector`），返回 `None` | terminal_status 非 `failed_api_unavailable` 但 collector 空 | 与上一行区分（靠 terminal_status）→ 记 `Run(status=failed, error="pipeline produced no inspector results", report_id=None)`，不误记成 `failed_api_unavailable` 污染异常率 |
| pipeline 产 Report 但降级（token/turns 预算耗尽 → `degraded_token_budget` / `degraded_max_turns`） | Report 存在、`Report.meta.status=degraded_*` | **有 Report** → `ReportStore.save` 后记 `Run(status=partial, report_id=<saved>)`（§7 表：degraded_* 走 partial，**不是** `budget_exhausted`） |
| API quota 排队超时（早期失败、无 Report） | quota-queue 机制（M2/M3 pipeline 当前**不产生**此信号） | §7 保留 `budget_exhausted`（无 Report、`started_at=None`）；M4 runner 的 pipeline 结果映射**不产生**它，待 quota 排队检测落地后由调度层产出 |
| daemon 收 SIGTERM 时有 in-flight job | 优雅停机 | 停止接受新触发、`asyncio.wait` 有界 grace 等当前 job 跑完正常落 Run；超 grace 强制 cancel → shield 终态写 + 主协程 drain → 记 `daemon_stopped(report_id=None)`（D-5） |
| daemon 被 `SIGKILL`（-9 不可捕获）时有 in-flight job | 进程立即死 | **不产生任何 Run 记录**（无 finally、M4 不写 start-row 占位行）；该次触发从台账缺失。已知限制（单进程内存调度、无 start-row WAL），M4 显式接受、不假装 stale-标注/恢复；对账留后续 |
| manifest YAML 非法（cron+interval 同时给 / target 不存在 / intent 空） | 加载失败 | **加载时** fail-loud（`schedule list`/`daemon` 启动即报错指出文件与字段），不静默跳过、不到触发时才崩 |
| daemon 用 `ClaudeSubscriptionBackend` | 启动 | `ensure_safe_for_daemon()` 强制 raise，daemon 拒绝启动（CLAUDE.md §4.11 规则 3） |
| 单次 job 抛未预期异常 | job 失败 | 记 `Run(status=failed, error=...)`，不让单 job 崩掉整个 daemon（其它 job 继续调度） |

### Operational Limits

并发模型：每 job `max_instances=1`，跨 job 由 `AsyncIOScheduler` 在单事件循环内并发（沿用 M2/M3 的 async + per-run 注入，不引入新进程/线程池）。**M4 每个 manifest 恰 1 个 target**（`run_diagnosis_pipeline` 单 target + §7 一 Run↔单 target；多 target fan-out 的 Run/Report 基数与聚合未定，列为非目标、loader 强制 `targets` 恰 1 个、留后续）。Run store 是本地 SQLite（与 reports.db 同目录 `~/.local/share/hostlens/`，独立表或独立库由 design 定）。daemon 单进程；多 daemon 同时跑同一 schedules 目录不在本提案范围（无分布式锁）。SIGKILL 残留不留 Run 记录（已知限制，对账留后续）。沿用 M2 backend 的 timeout / 重试 / token 预算，不新增。

### Security & Secrets

无新增凭据暴露面。manifest 里 `notify` 配置仅占位、不解析 secret（M5 才接 `${ENV_VAR}`）。Run 记录持久化的 Report 走既有 `ReportStore` 脱敏路径；`Run.error` 文本经既有 redact 边界。daemon 日志写文件需经 structlog 既有脱敏处理器。manifest 不含 secret（targets 引用既有已配置 target，凭据不在 manifest 内）。

### Cost / Quota Impact

每次触发 = 一次完整 Agent loop（与手动 `inspect --intent` 同量级），成本由调度频率 × 单次 pipeline 决定。`max_instances=1` + `coalesce` 防止积压触发放大调用量。CI 全程 mock / cassette / `-m 'not live'`，调度测试用 APScheduler 的可控时钟 / 手动 `trigger` + FakeBackend，不起真实定时 / 不调真实 API。

### Demo Path

```bash
# 1. 放一个 manifest（interval 每分钟，便于演示）
cat > schedules/demo-local-health.yaml <<'YAML'
name: demo-local-health
schedule: { interval: { minutes: 1 }, timezone: Asia/Shanghai }
targets: [local-host]
intent: "检查这台机器的健康状况"
report: { format: md, diff_with_last: true }   # diff_with_last 为 M5 占位、M4 不消费；回归 diff 用 `hostlens reports diff`
YAML

# 2. 看调度列表（next_fire_time 应可见）
hostlens schedule list

# 3. 手动立即触发一次（不等定时），落 Run + Report
hostlens schedule trigger demo-local-health
hostlens schedule status            # 最近 Run 状态分布
hostlens reports list local-host    # 复用 M3：能看到本次 Run 产的 Report

# 4. 前台 daemon，Ctrl-C / SIGTERM 优雅停机
hostlens schedule daemon            # 起来后按 interval 自动跑；SIGTERM 等当前 job 跑完再退

# 5. doctor 看 schedule 健康（落在既有 checks 命名空间下，不破 cli-foundation 封闭顶层 schema）
hostlens doctor --json | jq '.checks.schedules'
```

CI 验收：`pytest tests/ -m 'not live'` 全绿（manifest 校验 / RunStatus 边界不变量 / SIGTERM 优雅停机用 FakeBackend + 手动 trigger 模拟 / doctor schedule 检查）；`openspec-cn validate add-scheduler` 通过。
