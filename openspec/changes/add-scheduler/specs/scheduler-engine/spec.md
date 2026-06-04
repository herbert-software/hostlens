## 新增需求

### 需求:`RunStatus` 必须是对齐 ARCHITECTURE §7 的八值枚举

`hostlens.scheduler` 必须定义 `RunStatus(StrEnum)`，取值**恰为**八个，与 docs/ARCHITECTURE.md §7 一致：

- `ok` —— pipeline 正常产出 Report
- `partial` —— pipeline 产出 Report 但部分 Inspector 降级
- `budget_exhausted` —— API quota 排队超时（如排队 5 分钟未拿到 token）、早期失败且无 Report（**对齐 §7**：token / turns 预算耗尽产出的是 `degraded_token_budget`/`degraded_max_turns` 的 **有-Report** 降级，走 `partial`，**不**走本状态；本状态由未来 quota-queue 路径产出，M4 pipeline 映射不构造它）
- `missed` —— 错过触发窗口（超 `misfire_grace_time`），无 Report
- `skipped_due_to_running` —— 上次同 job 未结束，本次跳过，无 Report
- `failed_api_unavailable` —— Agent loop 后端不可用、重试耗尽，无 Report
- `failed` —— job 抛未预期异常，无 Report
- `daemon_stopped` —— job 跑到一半被 daemon 停机强制中断，无 Report

`RunStatus` 与 `reporting.models.ReportStatus` 是**两个独立枚举**，不得合并或互相替代。

#### 场景:RunStatus 取值集合恰好八个

- **当** 枚举 `list(RunStatus)`
- **那么** 必须恰为上述八个成员，名称与字符串值严格一致

### 需求:`Run` 模型必须强制 `report_id ⇔ status` 不变量

`hostlens.scheduler` 必须定义 Pydantic v2 `Run` 模型，字段：`run_id: str` / `schedule_name: str` / `triggered_at: datetime`（tz-aware）/ `started_at: datetime | None` / `finished_at: datetime | None` / `status: RunStatus` / `report_id: str | None` / `error: str | None` / `notify_results: list[object] = []`（占位、M4 恒空）。**M4 必须用 `list[object]` 而非 `list[NotifyResult]`**——`NotifyResult` 是 M5 Notifier 子系统的类型、当前不存在（`src/hostlens/notifiers/` 为空），M4 引用它会 `ImportError`；也**不用裸 `list`**（mypy `--strict` 下等价 `list[Any]`、触 CLAUDE.md §6「不允许 Any」红线）。M5 接 Notifier 时收紧为 `list[NotifyResult]`。本字段在 M4 恒为空列表。

为满足 CLAUDE.md §4.6 / ARCHITECTURE §7 第 819 行「每次触发留痕含 who/when/target/inspectors/report_hash」，`Run` 还必须含：

- `targets: list[str]`（本次触发的 target 集合，所有状态恒有）。
- `inspectors: list[str]`（本次**实际跑**的 inspector 集合快照）。**来源契约**：有 Report 时从 `report.meta.inspectors_used`（`reporting.models.ReportMeta.inspectors_used: list[InspectorRun]`，每个 `InspectorRun.name`）投影各 inspector name；job 体未启动的状态（`missed` / `skipped_due_to_running` / `budget_exhausted`）Planner 从未运行、本就无 inspector 执行，`inspectors` 为 `[]` 是**语义正确的「本次无 inspector 执行」**而非漏记。
- `report_hash: str | None`（**完整性锚点**，非跨运行去重键）。**算法契约**：`report_hash = hashlib.sha256(reporting.render_json.render(report).encode()).hexdigest()`，其中 `reporting.render_json.render`（公开函数，`__all__=["render"]`；`ReportStore.save` 内部以 `render_report_json` 别名调它，store.py:178）产出的是 `ReportStore.save` **持久化的同一份**确定性渲染（`model_dump_json` 字段序固定、已脱敏）。**计算时机/对象**：runner 对它产出的 Report（save **之前**持有的对象）`render` 取指纹。语义：锚定本次诊断 Report 的内容、把它绑定到这条 Run，使事后可校验内容未被篡改/确实对应这条 Run。**正常 db 路径**（`report_storage="db"`）：`ReportStore.save` 持久化的就是同一份 `render(report)` 字节，故 `report_hash` 与 reports.db 落库 JSON **逐字节一致**（完整性锚点完全成立）。**orphan 路径**（`report_storage="orphan"`）：`ReportStore.save` 会先把 `meta.status` 改写为 `stored_as_orphan` 再 `render` 写 orphan 文件（store.py:234-239），故 orphan 文件字节与 `report_hash`（基于原 status 的 render）**不**逐字节相等——`report_hash` 此时锚定的是**逻辑诊断内容**而非 orphan 文件字节（这是 orphan 降级的可接受弱化）。**不**声称跨运行可复现（Report 含本次时间戳）；只保证同一 Report 对象渲染得同一 hash（确定性、可测）。无 Report 的状态 `report_hash` 为 `None`。
- `report_storage: Literal["db", "orphan"] | None = None`（报告存储形态，**确定的 orphan 注记载体**，不复用 `error` 字段）：`ReportStore.save` 正常入库 → `"db"`；降级写 orphan JSON（`SaveResult.stored_as_orphan=True`，`report_id` 指向的内容不在 reports.db、`get_run` 取不到）→ `"orphan"`；无 Report 的状态 → `None`。供 `schedule status` 区分「report_id 必可经 `get_run` 取回（db）」与「需走 orphan 文件（orphan）」。

`Run` 必须用 `model_validator(mode="after")` 强制硬不变量：

1. **`report_id is None` 当且仅当 `status not in {RunStatus.ok, RunStatus.partial}`**。违反（`ok`/`partial` 却无 `report_id`，或非这两者却挂了 `report_id`）必须 raise `ValidationError`。
2. **`started_at is None` 当 `status in {missed, skipped_due_to_running, budget_exhausted}`**——这三值 job 体在拿到执行/配额前即被裁定、从未真正 started，**对齐 ARCHITECTURE §7 第 842 行的三值集合**。其余状态（含 `failed` / `failed_api_unavailable` / `daemon_stopped`）`started_at` 允许非 None。违反必须 raise `ValidationError`。
3. `report_hash is not None` 仅允许在 `status in {ok, partial}`（有 Report 才有指纹）；其余状态 `report_hash` 必须为 `None`。
4. `report_storage is not None` 当且仅当 `status in {ok, partial}`（有 Report 才有存储形态）；其余状态 `report_storage` 必须为 `None`。

#### 场景:ok 状态必须带 report_id

- **当** 构造 `Run(status=ok, report_id=None, ...)`
- **那么** 必须 raise `ValidationError`（不变量违反）

#### 场景:missed 状态禁止带 report_id

- **当** 构造 `Run(status=missed, report_id="r1", ...)`
- **那么** 必须 raise `ValidationError`（不变量违反）

#### 场景:partial 带 report_id 合法

- **当** 构造 `Run(status=partial, report_id="r1", started_at=<dt>, ...)`
- **那么** 必须成功构造

#### 场景:skipped_due_to_running 的 started_at 为 None

- **当** 构造 `Run(status=skipped_due_to_running, report_id=None, started_at=<dt>, ...)`
- **那么** 必须 raise `ValidationError`（该状态 job 未启动，started_at 必须 None）

#### 场景:missed 的 started_at 为 None

- **当** 构造 `Run(status=missed, report_id=None, started_at=<dt>, ...)`
- **那么** 必须 raise `ValidationError`（该状态 job 未启动，started_at 必须 None）

#### 场景:budget_exhausted 的 started_at 为 None

- **当** 构造 `Run(status=budget_exhausted, report_id=None, started_at=<dt>, ...)`
- **那么** 必须 raise `ValidationError`（早期失败、job 体未启动，started_at 必须 None，对齐 §7 三值集合）

#### 场景:failed 状态允许 started_at 非 None

- **当** 构造 `Run(status=failed, report_id=None, started_at=<dt>, finished_at=<dt>, error="...", ...)`
- **那么** 必须成功构造（`failed` 是 job 体已启动后抛异常，started_at 应非 None）

#### 场景:report_hash 仅在有 Report 状态非空

- **当** 构造 `Run(status=missed, report_id=None, report_hash="abc", ...)`
- **那么** 必须 raise `ValidationError`（无 Report 状态禁止挂 report_hash）

#### 场景:report_hash 对同一 Report 确定且等于持久化字节指纹

- **当** 对同一个 `Report` 对象计算 `report_hash` 两次
- **那么** 两次必须相等，且等于 `hashlib.sha256(reporting.render_json.render(report).encode()).hexdigest()`（`render` 即 `ReportStore.save` 持久化用的同一函数、同一份渲染字节，可被测试逐字节核对）

### 需求:调度引擎必须基于 `AsyncIOScheduler`，每 manifest 一 job，固定积压策略

`hostlens.scheduler.runner` 必须基于 APScheduler `AsyncIOScheduler`，把每个 `ScheduleManifest` 注册为一个 job（`job_id == manifest.name`），trigger 由 `schedule.cron`（**标准 5 段 crontab**：minute hour day-of-month month day-of-week）映射 `CronTrigger.from_crontab(cron, timezone=tz)`、`schedule.interval` 映射 `IntervalTrigger(..., timezone=tz)`。每个 job 必须配置：`max_instances=1`（同 job 不并发）、`coalesce=True`（积压触发合并为一次）、**固定默认 `misfire_grace_time`**：interval job = `max(30, interval_seconds // 2)` 秒、cron job = `300` 秒（M4 不做 per-manifest 配置）。其中 `interval_seconds` 定义为 IntervalSpec 各字段换算的总秒数：`weeks*604800 + days*86400 + hours*3600 + minutes*60 + seconds`（缺省/未提供的字段视作 `0`）。

引擎必须通过 APScheduler 事件 listener 把调度层事件翻译为 `RunStatus` 并落 Run 记录：`EVENT_JOB_MISSED → missed`；被 `max_instances` 拒启的触发 → `skipped_due_to_running`；`EVENT_JOB_ERROR`（job 体抛未捕获异常）→ `failed`。

#### 场景:上次未跑完时新触发记 skipped_due_to_running

- **当** 同名 job 的上一次执行尚未结束，下一次触发到来
- **那么** 引擎**禁止**并发启动第二个实例；必须落 `Run(status=skipped_due_to_running, report_id=None, started_at=None)`

#### 场景:错过窗口记 missed

- **当** 某次触发因超过 `misfire_grace_time` 被 APScheduler 判为 misfire
- **那么** 必须落 `Run(status=missed, report_id=None)`；积压的多次触发因 `coalesce=True` 只合并跑一次而非补跑 N 次

### 需求:job 执行必须复用诊断 pipeline 并按结果映射 RunStatus

job 执行体必须调用交付层无关的编排函数 `run_diagnosis_pipeline`（Planner→Diagnostician→忠实 `Report`，签名 `-> Report | None`），并按其结果落 Run。**`run_diagnosis_pipeline` 返回裸 `None` 时不携带原因**（两条语义路径都返回 `None`：后端不可用、与「Planner ok 但模型从未调 `run_inspector`」的空采集），故 runner **必须注入 pipeline 的既有可选形参 `planner_result_sink` 捕获 `planner_result.loop_result.terminal_status` 作为判别信号**（该形参既有、默认 no-op，不改 pipeline 行为）。映射规则：

- pipeline 返回 `Report` 且 `Report.meta.status == ok` → `ReportStore.save(report)` 后落 `Run(status=ok, report_id=<saved run_id>, report_hash=<指纹>)`
- pipeline 返回 `Report` 且 `Report.meta.status` 为降级类（`partial` / `degraded_no_planner` / `degraded_rate_limited` / `degraded_token_budget` / `degraded_max_turns` / `empty_response` / `stored_as_orphan`）→ 落 `Run(status=partial, report_id=<saved>, report_hash=<指纹>)`。**特别地：token / turns 预算耗尽产出的是 `degraded_token_budget` / `degraded_max_turns` 的 Report，映射为 `partial`（有 Report），禁止映射为无-Report 的 `budget_exhausted`**
- pipeline 返回 `None` 且 sink 捕获 `terminal_status == "failed_api_unavailable"` → `Run(status=failed_api_unavailable, report_id=None)`
- pipeline 返回 `None` 且 `terminal_status` 非 `failed_api_unavailable`（空采集：模型从未调 `run_inspector`）→ `Run(status=failed, error="pipeline produced no inspector results", report_id=None)`，**禁止**误记为 `failed_api_unavailable` 污染异常率统计

`budget_exhausted`（无 Report）是 §7 定义的「API quota 排队 5 分钟未拿到、早期失败」语义；M2/M3 pipeline **当前不产生此信号**（`agent/loop.py` 的 `_TerminalStatus` 闭集不含 `budget_exhausted`），故 M4 runner 的 pipeline-结果映射**永不构造** `budget_exhausted`——它保留在 8 值枚举里对齐 §7、供未来 quota-queue 路径由调度层产出。

Report 持久化必须复用既有 `reporting.store.ReportStore`，**禁止** scheduler 另造 Report 存储。`Run.report_id` 是指向 reports.db 的软引用；必须在 `ReportStore.save` 成功之后才写带 `report_id` 的 Run。正常入库的 Run 必须 `report_storage="db"`。**orphan 边界**：`ReportStore.save` 在 SQLite 写失败时返回 `SaveResult(stored_as_orphan=True, orphan_path=...)`（report 落 JSON 文件、不在 reports.db、`get_run` 取不到）；runner 必须显式处理——落 `Run(status=partial, report_id=<run_id>, report_hash=<指纹>, report_storage="orphan")`（用确定的 `report_storage` 字段而非塞 `error`），**禁止**当 `ok`/`report_storage="db"` 静默写出一条 `get_run` 取不到的 Run。

#### 场景:正常巡检产 Report 并留痕

- **当** 一次触发的 pipeline 正常产出 ok 的 `Report` 且 `ReportStore.save` 正常入库（非 orphan）
- **那么** 该 Report 必须经 `ReportStore.save` 持久化；必须落 `Run(status=ok, report_id=<save 返回的 run_id>, report_hash, report_storage="db", targets, inspectors, started_at, finished_at)`，且 `report_id` 可经 `ReportStore.get_run` 取回

#### 场景:后端不可用不产 Report

- **当** pipeline 因 Agent loop 后端重试耗尽返回 `None` 且 sink 捕获 `terminal_status == "failed_api_unavailable"`
- **那么** 必须落 `Run(status=failed_api_unavailable, report_id=None)`；**禁止**写任何 Report

#### 场景:空采集与后端不可用区分

- **当** pipeline 返回 `None` 但 `terminal_status` 非 `failed_api_unavailable`（Planner ok、模型从未调 `run_inspector`，collector 空）
- **那么** 必须落 `Run(status=failed, error="pipeline produced no inspector results", report_id=None)`；**禁止**误记为 `failed_api_unavailable`

#### 场景:token 预算耗尽产 partial Report 而非 budget_exhausted

- **当** pipeline 因 token / turns 预算耗尽产出 `Report.meta.status == degraded_token_budget`（或 `degraded_max_turns`）的 Report
- **那么** 必须 `ReportStore.save` 后落 `Run(status=partial, report_id=<saved>)`；**禁止**丢弃该 Report 或落无-Report 的 `budget_exhausted`

#### 场景:save 降级 orphan 时不当 ok 静默写

- **当** pipeline 产出 ok Report 但 `ReportStore.save` 返回 `stored_as_orphan=True`（SQLite 写失败降级 JSON）
- **那么** 必须落 `Run(status=partial, report_id=<run_id>, report_storage="orphan", ...)`；**禁止**落 `status=ok` 或 `report_storage="db"` 后让 `report_id` 经 `get_run` 取回为 `None` 而无任何标注

### 需求:Run 记录必须持久化到独立 store 并可查询

`hostlens.scheduler.store` 必须提供 `RunStore`，把 `Run` 持久化到独立的本地 SQLite 库（默认 `~/.local/share/hostlens/runs.db`，`db_path` 必须可注入以便测试用临时库），**禁止**与 `reports.db` 混表。`RunStore` 必须提供按 `schedule_name` / 时间倒序列出最近 N 条 Run 的查询，供 `schedule status` 与 doctor 消费。store 必须 async、无 module-level 连接（与 `ReportStore` 形态一致）。

#### 场景:Run 写入后可按时间倒序取回

- **当** 向 `RunStore` 写入若干 `Run` 后查询最近 N 条
- **那么** 必须按触发时间倒序返回，数量不超过 N，内容与写入一致

#### 场景:RunStore 与 ReportStore 不共享存储文件

- **当** 检视默认存储路径
- **那么** Run 记录库（`runs.db`）必须与 Report 库（`reports.db`）为不同文件，互不写对方的表
