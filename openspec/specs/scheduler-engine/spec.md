# scheduler-engine 规范

## 目的

定义调度引擎契约——`RunStatus` 为对齐 ARCHITECTURE §7 的八值枚举、`Run` 模型强制 `report_id ⇔ status` 不变量、引擎基于 `AsyncIOScheduler` 每 manifest 一 job 固定积压策略、job 执行复用诊断 pipeline 并按结果映射 RunStatus、Run 记录持久化到独立 store 并可查询、runner 在 Report 持久化后派发 notify 并落地结果。
## 需求
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

`hostlens.scheduler` 必须定义 Pydantic v2 `Run` 模型，字段：`run_id: str` / `schedule_name: str` / `triggered_at: datetime`（tz-aware）/ `started_at: datetime | None` / `finished_at: datetime | None` / `status: RunStatus` / `report_id: str | None` / `error: str | None` / `notify_results: list[NotifyResult] = []`。**M5 起 `notify_results` 收紧为 `list[NotifyResult]`**（`NotifyResult` 由 `hostlens.notifiers` 提供，M5 起存在，不再有 M4 的 `ImportError` 约束）：有 Report 的触发经 notify 派发后填入每通道结果；无 Report 的状态恒为 `[]`。M4 写入的空数组 `[]` 反序列化仍合法（向后兼容、无迁移）。

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

#### 场景:notify_results 收紧为 NotifyResult 且 M4 空数组兼容

- **当** 反序列化一条 M4 写入的 `notify_results: []` 记录，以及构造一条 M5 含 `[NotifyResult(channel="x", status="sent")]` 的记录
- **那么** 两者必须均合法；`notify_results` 元素类型为 `NotifyResult`（非 `object`）；空数组向后兼容无需迁移

### 需求:调度引擎必须基于 `AsyncIOScheduler`，每 manifest 一 job，固定积压策略

`hostlens.scheduler.runner` 必须基于 APScheduler `AsyncIOScheduler`，把每个 `ScheduleManifest` 注册为一个 job（`job_id == manifest.name`），trigger 由 `schedule.cron`（**标准 5 段 crontab**：minute hour day-of-month month day-of-week）映射 `CronTrigger.from_crontab(cron, timezone=tz)`、`schedule.interval` 映射 `IntervalTrigger(..., timezone=tz)`。每个 job 必须配置：`max_instances=1`（同 job 不并发）、`coalesce=True`（积压触发合并为一次）、**固定默认 `misfire_grace_time`**：interval job = `max(30, interval_seconds // 2)` 秒、cron job = `300` 秒（M4 不做 per-manifest 配置）。其中 `interval_seconds` 定义为 IntervalSpec 各字段换算的总秒数：`weeks*604800 + days*86400 + hours*3600 + minutes*60 + seconds`（缺省/未提供的字段视作 `0`）。

引擎必须通过 APScheduler 事件 listener 把调度层事件翻译为 `RunStatus` 并落 Run 记录：`EVENT_JOB_MISSED → missed`；被 `max_instances` 拒启的触发 → `skipped_due_to_running`；`EVENT_JOB_ERROR`（job 体抛未捕获异常）→ `failed`。

#### 场景:上次未跑完时新触发记 skipped_due_to_running

- **当** 同名 job 的上一次执行尚未结束，下一次触发到来
- **那么** 引擎**禁止**并发启动第二个实例；必须落 `Run(status=skipped_due_to_running, report_id=None, started_at=None)`

#### 场景:错过窗口记 missed

- **当** 某次触发因超过 `misfire_grace_time` 被 APScheduler 判为 misfire
- **那么** 必须落 `Run(status=missed, report_id=None)`；积压的多次触发因 `coalesce=True` 只合并跑一次而非补跑 N 次

### 需求:Run 记录必须持久化到独立 store 并可查询

`hostlens.scheduler.store` 必须提供 `RunStore`，把 `Run` 持久化到独立的本地 SQLite 库（默认 `~/.local/share/hostlens/runs.db`，`db_path` 必须可注入以便测试用临时库），**禁止**与 `reports.db` 混表。`RunStore` 必须提供按 `schedule_name` / 时间倒序列出最近 N 条 Run 的查询，供 `schedule status` 与 doctor 消费。store 必须 async、无 module-level 连接（与 `ReportStore` 形态一致）。

#### 场景:Run 写入后可按时间倒序取回

- **当** 向 `RunStore` 写入若干 `Run` 后查询最近 N 条
- **那么** 必须按触发时间倒序返回，数量不超过 N，内容与写入一致

#### 场景:RunStore 与 ReportStore 不共享存储文件

- **当** 检视默认存储路径
- **那么** Run 记录库（`runs.db`）必须与 Report 库（`reports.db`）为不同文件，互不写对方的表

### 需求:runner 必须在 Report 持久化后派发 notify 并落地结果

当且仅当 job 体产出了 Report（`status in {ok, partial}`）时，runner 必须在 `ReportStore.save` 之后、构造终态 `Run` 之前，按 manifest 的 `notify` 路由把（已脱敏的）Report 发送到对应通道，并把每通道 `NotifyResult` 写入 `Run.notify_results`。无 Report 的状态（`failed_*` / `missed` / `skipped_due_to_running` / `budget_exhausted` / `daemon_stopped`）**禁止**派发 notify（无内容可推），`notify_results` 为 `[]`。（`budget_exhausted` 是 `RunStatus` enum 成员、在 job body 前裁定故列此；M4 runner 实际不构造它——pipeline 内 token 退化映射为 `partial`，与 proposal Failure Mode 1 正交不矛盾。）

runner 的触发入口（`trigger`）必须接受 keyword-only 参数 `dispatch_notify: bool = True`。默认 `True` 完整保留上述派发行为；daemon / `schedule run` / `schedule trigger` CLI 均**不传**该参数，行为零变更。当调用方显式传 `dispatch_notify=False`（如 `run_schedule_now` MCP 工具）时，runner 必须在产出并持久化 Report 后**跳过** notify 派发整段（连同 `only_if` 路由求值），`Run.notify_results` 必须为 `[]`，而 Run 的其余留痕（`status` / `report_id` / targets / inspectors / report_hash）必须与 `dispatch_notify=True` 路径一致。`dispatch_notify=False` **禁止**改变 `RunStatus` 裁定。

**参数穿透契约（实现完整性，非可选）**：实际的 notify 派发点不在 `trigger`，而在 job body 内部的报告映射阶段——调用链为 `trigger → _run_job → _finalize_outcome → _map_outcome`，由 `_map_outcome` 调 `_dispatch_notify`（即抑制的目标语句）。因此 `dispatch_notify` 这个 keyword-only 参数必须**逐层穿透** `_run_job` / `_finalize_outcome` / `_map_outcome` 直到 `_dispatch_notify` 调用点，且**每一层的默认值都必须为 `True`**。这一不变量直接决定 timer 路径零变更：定时触发注册的 job body 是 `_run_job`（**不经 `trigger`**），它以 `_run_job(name)` 形式被调用、不传 `dispatch_notify`，唯有每层默认 `True` 才保证 timer / daemon 行为字节不变。**禁止**只在 `trigger` 加该参数而不向下穿透（那样抑制语句永不被触及，`dispatch_notify=False` 形同失效）。

notify 阶段必须**失败隔离**：任何通道在**路由（`only_if` 求值）/ 渲染 / 发送**任一环节的异常**禁止**冒泡出 job 体（Report 已留痕），仅记为 `NotifyResult(status="failed", error=...)`；notify 整体不得改变已裁定的 `RunStatus`。隔离面**含 `only_if` 求值的任何运行期异常**（含但不限于 `TypeError` / `NameNotDefined` / `TimeoutError` / `simpleeval.InvalidExpression` 等，详见 notify-routing「`only_if` 运行期求值异常」需求），不限于渲染/发送。多通道发送必须并发执行（`asyncio.gather` + 并发上限，默认 4），通道间互不阻塞；`gather` 必须以「单通道异常不取消其它通道」的方式收集（如 `return_exceptions=True` 或每通道独立 try）。`channel_registry` 必须经 runner 构造器注入（与既有 `RunStore` / `ReportStore` / `backend_factory` 同列，无 module-level singleton），daemon / `schedule run` / `trigger` 共用同一装配。

`Run.notify_results` 持久化/反序列化沿用既有 RunStore 的 `model_validate_json` 路径。**已知可接受弱化（F15）**：`notify_results` 从 M4 的 `list[object]`（宽松）收紧为 `list[NotifyResult]`（严格）后，理论上新增「单条畸形 NotifyResult 记录拖累 `schedule status` 整表查询」的反序列化失败面——但 M5 起 `notify_results` 仅由本 runner 用强类型 `NotifyResult` 写入，正常运行不产畸形记录；单记录读取隔离继承 baseline RunStore 查询契约（本 delta 不收紧、不新增该保证），跨期 schema 演进的兼容性留后续里程碑，非本提案目标。

#### 场景:有 Report 的触发派发并记录每通道结果

- **当** job 体产出 `ok` Report，manifest 配两个通道（一个 `only_if` 真、一个假）
- **那么** `Run.notify_results` 必须含两条：真的记 `sent`（或 `failed`），假的记 `skipped`；`RunStatus` 仍为 `ok`

#### 场景:通道发送异常不改变 RunStatus 且不冒泡

- **当** 某通道 `send` 持续失败耗尽重试
- **那么** 该通道记 `NotifyResult(status="failed", error=...)`，job 体不抛异常，`Run.status` 维持 `ok`/`partial`

#### 场景:only_if 运行期求值异常不改变 RunStatus 且不冒泡

- **当** 某通道 `only_if` 运行期抛异常（如类型不匹配 / 拼错名 / 求值超时）
- **那么** 该通道记 `NotifyResult(status="failed", error=...)`，job 体不抛异常，`Run.status` 维持 `ok`/`partial`，其它通道照常派发

#### 场景:无 Report 状态不派发 notify

- **当** 触发结果为 `failed_api_unavailable`（无 Report）
- **那么** 必须不发生任何通道发送，`Run.notify_results == []`

#### 场景:dispatch_notify=False 抑制派发但仍持久化 Report 与留痕

- **当** 以 `trigger(name, dispatch_notify=False)` 触发一个**配了 notify 通道**且 job 体产出 `ok` Report 的 schedule
- **那么** Report 必须照常持久化、`Run.report_id` 非空、`Run.status` 为 `ok`，且 `Run.notify_results == []`；测试必须以 spy/mock 断言该通道的 `only_if` 求值与 `send` **调用计数均为 0**（仅凭 `notify_results == []` 不足以证明抑制真生效——空列表在「无通道配置」时也成立；故场景刻意要求**配了通道**且断言 send 从未被调，避免 vacuous 验收）

### 需求:job 执行必须按 mode 路由（agent 复用诊断 pipeline / deterministic 走确定性采集）并按结果映射 RunStatus

job 执行体**必须**按 `manifest.mode` 路由，两条路径都产出 `Report | None` 并按**同一套** RunStatus 映射落 Run:

**`mode == "agent"`（不变）**:调用交付层无关的编排函数 `run_diagnosis_pipeline`（Planner→Diagnostician→`Report | None`），注入既有 `planner_result_sink` 捕获 `terminal_status` 判别 `None` 原因（后端不可用 vs 空采集）。

**`mode == "deterministic"`（新增）**:调用 `run_deterministic_inspection`（见 `deterministic-inspection-mode` 能力):逐 target 跑固定 inspector 集（不走 Planner、不注入 LLMBackend 到采集阶段）→ 组装多 target `Report` → narrate-only Diagnostician 写根因。返回 `Report`（采集到 ≥1 个 inspector 结果）或 `None`（全部 target × inspector 均无结果可组装）。

**共享映射规则**（两 mode 一致）:

- 返回 `Report` 且 `meta.status == ok` → `ReportStore.save` 后落 `Run(status=ok, report_id=<saved>, report_hash)`
- 返回 `Report` 且 `meta.status` 为降级类——既有显式枚举**逐字保留不削**：`partial` / `degraded_no_planner` / `degraded_rate_limited` / `degraded_token_budget` / `degraded_max_turns` / `empty_response` / `stored_as_orphan`——→ `Run(status=partial, report_id=<saved>, report_hash)`;**token/turns 预算耗尽（仅 agent 可触发）产 `degraded_token_budget`/`degraded_max_turns` 的 Report 仍映射 `partial`、禁止映射无-Report 的 `budget_exhausted`**
- agent 返回 `None` 且 sink `terminal_status == "failed_api_unavailable"` → `Run(status=failed_api_unavailable, report_id=None)`
- agent 返回 `None` 且非上述（空采集）→ `Run(status=failed, error="pipeline produced no inspector results", report_id=None)`
- **deterministic 返回 `None`（全 inspector 无结果）→ `Run(status=failed, error="deterministic inspection produced no inspector results", report_id=None)`**;deterministic **不经** LLM 采集，故**不产** `failed_api_unavailable`（narrate 阶段后端不可用按 `degraded` Report 处理、不丢已采集结果）

Report 持久化、orphan 边界、`report_storage` 字段语义不变（复用 `ReportStore`）。

#### 场景:agent 模式行为不变
- **当** 一个 `mode: agent`（或省略 mode）manifest 触发、pipeline 正常产 ok Report 入库
- **那么** 行为与变更前**完全一致**:`run_diagnosis_pipeline` + sink + 落 `Run(status=ok, report_id=<saved>, report_storage="db")`

#### 场景:deterministic 模式逐 target 跑固定集产多 target Report
- **当** 一个 `mode: deterministic`、`targets: [a, b]` 的 manifest 触发
- **那么** **必须**走 `run_deterministic_inspection`（不实例化 Planner、采集阶段不注入 LLMBackend），逐 target 跑固定集、组装一份含 a/b 的 Report、narrate-only 写根因，并按共享规则落 `Run`（ok/partial）

#### 场景:deterministic 全无结果落 failed
- **当** deterministic 模式下全部 target × inspector 均无可组装结果
- **那么** **必须**落 `Run(status=failed, error="deterministic inspection produced no inspector results", report_id=None)`,**禁止**误记为 `failed_api_unavailable`
