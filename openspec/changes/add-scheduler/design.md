## 上下文

M1–M3 已交付完整诊断链路：`run_diagnosis_pipeline`（`cli/_intent.py:450`，输入 target+intent → Planner→Diagnostician → 忠实 `Report|None`）+ `ReportStore`（`reporting/store.py:138`，`save`/`list_runs`/`get_run`，库 `~/.local/share/hostlens/reports.db`）。唯一触发入口是人手敲 `hostlens inspect --intent`。M4 加「自动按时跑 + 留痕 + daemon 常驻」这一层。

关键既有设施（复用，不改）：
- `run_diagnosis_pipeline`（M3 编排函数，当前物理位于 `cli/_intent.py`）。
- `ReportStore.save(report) -> SaveResult`（Report 持久化 + 脱敏 + orphan 降级）。
- `BackendDiagnostics.ensure_safe_for_daemon()`（`agent/backend.py:329`）+ `is_daemon_mode(settings)`（:453）—— daemon 模式禁 `ClaudeSubscriptionBackend` 的现成门。
- `ReportStatus`（`reporting/models.py:209`，**已存在**）。其 docstring（:224）明示「无 Report 的状态属 M4 `RunStatus`」——`RunStatus` **本提案新建**，与 `ReportStatus` 是两个独立枚举。
- doctor 检查为函数式装配（`cli/doctor.py` `_check_*` → `_build_report` → `_render_human`），无独立 spec。

技术栈锁定：APScheduler 进程内调度（CLAUDE.md §2，不要 cron+shell）、async-first、Pydantic v2 manifest。

## 目标 / 非目标

**目标：**
- `schedules/*.yaml` 是调度 SOT；`ScheduleManifest` 强类型、加载时 fail-loud 校验。
- 按 cron / interval 自动触发，job 执行 = 复用 `run_diagnosis_pipeline` 产 Report 并经 `ReportStore.save` 持久化。
- 每次触发留一条 `Run` 记录（8 值 `RunStatus`），满足硬不变量 `report_id is None ⇔ status ∉ {ok, partial}`。
- daemon 常驻 + SIGTERM 优雅停机（停止接受新触发、等当前 job 跑完再退、强制中断落 `daemon_stopped`）。
- `hostlens schedule list/run/daemon/trigger/status` + `doctor` schedule 健康检查。

**非目标：**
- 真实 Notifier 实现与发送、`only_if` 路由表达式求值（均 M5——本提案 `notify` 配置仅占位、`Run.notify_results` 字段预留恒空、不解析其中 secret）。
- 把 regression diff 作为巡检报告 section（M5）。
- Remediation（M9）、MCP server（M7）。
- 分布式 / 多 daemon 抢占同一 schedules 目录的锁（单机单 daemon 假设）。
- `misfire_grace_time` 的 per-manifest 可配置（M4 用固定默认，配置化留后续）。
- **多 target fan-out**：一个 manifest 一次触发跑多个 target。`run_diagnosis_pipeline` 是单 target，且 §7「一次触发 0/1 Report」隐含一 Run↔单 target；多 target 涉及未定的 Run/Report 基数、状态聚合、report_id 生成规则。**M4 加载器强制 `targets` 恰 1 个**（字段保留 `list[str]` 形态以便后续 fan-out 不破 schema），多 target 留后续提案。
- SIGKILL 残留 Run 的对账/恢复（需 start-row WAL；M4 接受 SIGKILL 不留记录）。

## 决策

### D-1：调度器 = `AsyncIOScheduler`，单事件循环，每 manifest 一 job
APScheduler 的 `AsyncIOScheduler` 与 M2/M3 的 async-first 模型同构（job coroutine 直接 `await run_diagnosis_pipeline`），无需新进程/线程池。每个 `ScheduleManifest` 注册为一个 job，`job_id = manifest.name`（名唯一，加载器校验重名）。**否决** `BackgroundScheduler`（另起线程，与 async pipeline 阻抗不匹配）。

### D-2（关键）：把 `run_diagnosis_pipeline` 上提到交付层无关的编排模块
现 `run_diagnosis_pipeline` 物理在 `cli/_intent.py`。让 `scheduler/runner.py` 直接 `from hostlens.cli._intent import ...` 会让 Scheduler 反向依赖 CLI 交付层（且加载 typer 等），违背 ARCHITECTURE「CLI 与 Scheduler 都是触发入口、各自汇聚到 Planner」的对等关系。⇒ **新建 `src/hostlens/orchestration/pipeline.py`，把 `run_diagnosis_pipeline` 迁入**；`cli/_intent.py` 改为重导出（**零行为变更**，既有 `cli/_intent` 测试与导入路径不破）；`scheduler/runner.py` 从 `orchestration` 导入。两个交付层都依赖 orchestration、互不依赖。**否决**：scheduler 直接 import `cli._intent`（交付层互相耦合 + 拖入 CLI 依赖）。

**迁移清单（tasks 1.1 据此划界，不让实现者自行猜测哪些跟着走）**——核验 `cli/_intent.py` 后确定的边界：
- **迁入 `orchestration/pipeline.py`**：`run_diagnosis_pipeline` 本身 + 它私有调用的纯编排辅助 `_seed_findings_from_snapshot` / `_seeding_sort_key` / `_assemble_report` / `_sum_loop_usage`（这些不依赖 typer / Rich / CLI 上下文，只依赖 agent 层）。
- **留在 `cli/_intent.py`**：Rich UI / rendering / `run_intent_diagnosis`（CLI wrapper，exit-code 与 stderr note 语义）/ 其它 CLI-only helper。
- **重导出兼容**：凡被外部直接 import 的迁移符号（`run_diagnosis_pipeline` 及任何被 `cli/demo.py` / incidents generator / 既有 `tests/cli/test_run_diagnosis_pipeline.py` 等直接引用的私有 helper），在 `cli/_intent.py` 保留 `from hostlens.orchestration.pipeline import ...` 重导出名，保证旧 import 路径不断。
- **验收**：全量回归 + 确认 `cli/_intent` / `demo` / incidents generator 既有测试**零改动**通过（tasks 1.3）。

**风险**：迁移触及 M3 代码——以「移动函数 + 重导出」最小化；若迁移牵连过广，回退为 scheduler 直接 import `cli._intent` 并在 design 记债。

### D-2b：无-Report 的 RunStatus 判别经既有 `planner_result_sink` seam，不改 pipeline 行为
核验 `run_diagnosis_pipeline`（`cli/_intent.py:450`，签名 `-> Report | None`）：`None` 在**两条不同语义路径**产生且**裸 `None` 无判别信息**——① Planner `terminal_status == "failed_api_unavailable"`（:545-549，API 不可用、collector 空）；② Planner ok 但 collector 空（模型从未调 `run_inspector`，:578-584）。runner 必须区分二者，否则把②误记成 `failed_api_unavailable` 会污染 doctor 异常率。⇒ runner 注入 pipeline **既有可选形参** `planner_result_sink: Callable[[PlannerResult], None]`（已存在于签名，默认 None、不改既有行为）捕获 `planner_result.loop_result.terminal_status`，据此映射：
- 返回 `Report` → 按 `Report.meta.status` 映射（见 D-3b）。
- 返回 `None` 且 sink 捕获到 `terminal_status == "failed_api_unavailable"` → `Run(failed_api_unavailable, report_id=None)`。
- 返回 `None` 且 terminal_status 非该值（②空采集）→ `Run(failed, error="pipeline produced no inspector results", report_id=None)`。

**否决**：把 pipeline 返回类型改成携带原因的结构体——那会破坏 proposal「不动 `run_diagnosis_pipeline` 行为/签名」承诺并需重做 `--intent`/`demo` 回归；既有 sink seam 已足够，零行为变更。

### D-3：`RunStatus` 新建（8 值），`Run` 模型在构造时强制不变量
`scheduler/store.py`（或 `scheduler/models.py`）定义 `RunStatus(StrEnum)` = {ok, partial, budget_exhausted, missed, skipped_due_to_running, failed_api_unavailable, failed, daemon_stopped}（对齐 ARCHITECTURE §7）。`Run` 是 Pydantic v2 模型，用 `model_validator(mode="after")` 强制下列硬不变量（违反即 `ValidationError`，fail-loud 杜绝脏数据）：
1. **`report_id is None ⇔ status ∉ {ok, partial}`**（杜绝「ok 却没 report」或「missed 却挂了个 report」）。
2. **`started_at is None` 当 `status ∈ {missed, skipped_due_to_running, budget_exhausted}`**——**对齐 ARCHITECTURE §7 第 842 行的三值集合**（这三者 job 体在拿到执行/配额前就被裁定，从未真正 started）；其余状态（含 `failed` / `failed_api_unavailable` / `daemon_stopped`）`started_at` **可**非 None（job 体已启动或已进入 API 调用）。
3. **`report_hash is not None` 仅允许 `status ∈ {ok, partial}`**（有 Report 才有指纹），其余状态 `report_hash` 必须 None。
4. **`report_storage is not None` 当且仅当 `status ∈ {ok, partial}`**（有 Report 才有存储形态，见 D-11），其余状态必须 None。

> 先前草案漏了 `budget_exhausted`（只列两值），与 §7 真理源分歧、会让 validator 误纳 `Run(budget_exhausted, started_at=<dt>)`。已收口为三值，向 §7 看齐（不改 §7）。

留痕字段（CLAUDE.md §4.6 / ARCHITECTURE §7 第 819 行要求 who/when/target/inspectors/report_hash）：`Run` 除 `run_id/schedule_name/triggered_at/started_at/finished_at/status/report_id/error/notify_results` 外，**新增**：
- `targets: list[str]`（本次触发的 target 集合，所有状态恒有）。
- `inspectors: list[str]`（本次实际跑的 inspector 集合快照）。**来源**：有 Report → 从 `report.meta.inspectors_used`（`reporting/models.py:324`，`list[InspectorRun]`）投影各 `InspectorRun.name`；job 体未启动的状态（missed/skipped/budget_exhausted）Planner 未运行、本无 inspector 执行 → `[]`（语义正确的「本次无执行」，非漏记）。⚠️ 注意 `inspector_versions: dict[str,str]` 是 `BaselineRef`（:282）的字段、不是 `ReportMeta` 的——别用错。
- `report_hash: str | None`（**完整性锚点**，不是跨运行去重键）。**算法定死**：`hashlib.sha256(reporting.render_json.render(report).encode()).hexdigest()`，对 `ReportStore.save` 持久化的**同一份**确定性渲染字节取指纹。`reporting.render_json.render` 是公开函数（`__all__=["render"]`，render_json.py:40）；`ReportStore.save` 内部以别名 `render_report_json` 调它（store.py:53/178）——契约里用**真名 `render`**，别把局部别名当公开符号。runner 对它产出的 Report（save 前持有的对象）取指纹。**db 路径**：save 持久化同一份 `render(report)`，`report_hash` 与 reports.db 字节一致（完整性锚点成立）。**orphan 路径**：save 先 `model_copy(status=stored_as_orphan)` 再 render 写文件（store.py:234-239），故 orphan 文件字节与 `report_hash` 不逐字节相等，`report_hash` 锚定逻辑诊断内容（orphan 降级的可接受弱化）。**不**声称跨运行可复现（Report 含本次时间戳）；只保证「同一 Report 对象 → 同一 hash」（确定性、可逐字节测）。无 Report 时 None。
- `report_storage: Literal["db","orphan"] | None`（orphan 注记的确定载体，见 D-11）：正常入库 `"db"`、orphan 降级 `"orphan"`、无 Report `None`。

> ARCHITECTURE §7 的 `Run` python 块与第 819 行 prose 自身存在「prose 列了 target/inspectors/report_hash 但 python 块没有」的内部不一致；本提案以 §4.6 / 第 819 行 prose 为准把字段补全。§7 python 块的同步留作 ARCHITECTURE 自身的后续债（不在本提案范围）。

### D-3b：pipeline 有 Report 时按 `ReportStatus` 映射 RunStatus（token/turns 预算 → `partial`，不是 `budget_exhausted`）
核验 `agent/loop.py:82-90` 的 `_TerminalStatus` 闭集**不含** `budget_exhausted`；token 预算耗尽是 `degraded_token_budget`、turns 耗尽是 `degraded_max_turns`，二者按 ARCHITECTURE §7 边界表（第 855 行）**产出 partial Report（有 Report）**。⇒ runner 拿到 Report 时：
- `Report.meta.status == ok` → `Run(ok, report_id=<saved>)`。
- `Report.meta.status ∈ {partial, degraded_no_planner, degraded_rate_limited, degraded_token_budget, degraded_max_turns, empty_response, stored_as_orphan}` → `Run(partial, report_id=<saved>)`。

**`budget_exhausted`（无 Report）是 §7 定义的「API quota 排队 5 分钟未拿到、早期失败」语义，M2/M3 pipeline 当前不产生此信号**（无 quota-queue 检测）。所以 M4 runner 的 pipeline-结果映射**永不构造** `budget_exhausted`；它保留在 8 值枚举里对齐 §7、供未来 quota-queue 路径（OPERABILITY）由调度层产出。**否决**把 token/turns 预算耗尽映射成 `budget_exhausted`——那会丢弃本应保存的 partial Report 且与既有 `ReportStatus` 语义冲突。

### D-4：Run 记录用独立 `RunStore`（独立 `runs.db`），镜像 `ReportStore` 形态
Run 是「调度层执行台账」、Report 是「诊断产物」，两个生命周期 + 两套查询。⇒ 新 `RunStore`（`scheduler/store.py`）持有自己的 `~/.local/share/hostlens/runs.db`（WAL、async、无 module-global 连接、`db_path` 可注入测试用 tmp），不塞进 reports.db。`Run.report_id` 是跨库软引用（指向 reports.db 的 run_id），`schedule status` 展示 Run、需要详情时用 report_id 走 `ReportStore.get_run`。**否决**：Run 塞进 reports.db 同表（混淆两个聚合根、查询互相污染）。

### D-5：SIGTERM 优雅停机 = runner 追踪 in-flight task + 有界 grace + 显式 drain，强制中断才落 `daemon_stopped`
**不依赖 `AsyncIOScheduler.shutdown(wait=True)` 做 in-loop 等待**：APScheduler 的 `shutdown()` 是**同步方法、返回 `None`（非 awaitable）**，`asyncio.wait_for(scheduler.shutdown(...))` 会 TypeError；且 `wait=True` 在同一事件循环内等本循环上的 job task 会**自等死锁**。⇒ runner 自己维护一个 **in-flight job task 集合**。

**task 注册机制（定死，不留二义）**：`AsyncIOScheduler` 的 `AsyncIOExecutor` **在内部 `create_task` 运行 job coroutine、不把 Task 交还**，故 runner **无法**在外层「创建」该 task——唯一可行 seam 是 **job 体自己在入口注册 `asyncio.current_task()`**：job coroutine 的**第一条语句**（任何 `await` 之前）`t = asyncio.current_task(); inflight.add(t)`，并 `finally: inflight.discard(t)`。**已知窗口**：从「scheduler 派发 job」到「job coroutine 实际被事件循环调度执行到注册行」之间有一个极小窗口，此窗口内到来的 SIGTERM 会看到 `inflight` 尚不含该 job——该 job 等同「未及启动」，落入 SIGKILL/未启动同类的「不留记录」路径（接受，见风险段）；因注册行在所有 `await` 之前、同步执行，窗口仅限「coroutine 已被 create_task 但尚未获得首次执行权」的调度间隙，`max_instances=1`+`pause()` 下不会放大。停机机制：

```python
async def graceful_stop(scheduler, inflight: set[asyncio.Task], grace: float):
    scheduler.pause()                       # 停止派发新触发（不再有新 job 启动）
    if inflight:
        done, pending = await asyncio.wait(inflight, timeout=grace)   # 有界 grace 等当前 job 跑完
        for task in pending:                # 超 grace 仍未完成 → 强制 cancel
            task.cancel()
        if pending:
            # 关键 drain：在关闭 event loop 前 await 被 cancel 的 task 跑完它们
            # shield 保护下的终态写，否则 loop 一关 shielded save 会被打断、行丢失
            await asyncio.gather(*pending, return_exceptions=True)
    scheduler.shutdown(wait=False)          # 同步、不等（job 已 drain 完）
```

job 体包 `try/except asyncio.CancelledError`：grace 内自然跑完 → 落真实状态（**优雅路径无 `daemon_stopped`**）；超 `grace` 被 `task.cancel()` 硬切 → `except CancelledError` 分支落 `Run(status=daemon_stopped, report_id=None)` 后重抛。**信号 handler 必须幂等**：停机已在进行时收到的二次 SIGTERM/SIGINT 必须被忽略（不再 `task.cancel()` 第二次）——否则二次 cancel 会落在 job 体已进入的 `await asyncio.shield(save)` 处、在已执行过的 `except` 之外重抛（Python 不重入同一 `except`），徒增复杂度；shield 仍保 save 不丢，但幂等 handler 让路径干净。`GRACE_SECONDS` 固定保守默认（见「待解决问题」定值）。

**两层正确性保护（缺任一 `daemon_stopped` 行都会丢）**：
1. **shield 终态写**：`except CancelledError` 分支里的 `await run_store.save(daemon_stopped_run)` 跑在已被 cancel 的 task 内，不保护则该 `await`（RunStore 的 `asyncio.to_thread` SQLite 写）立刻再抛 `CancelledError`、行永不落库。⇒ 必须 `await asyncio.shield(run_store.save(daemon_stopped_run))` 后再重抛。
2. **主协程 drain**：drain 等的是 **job task 本身**——`gather(*pending)` 里 `pending` 是 job task；job task 在 `except` 内执行 `await asyncio.shield(save)` 时会**挂起直到内层 save future 完成**（shield 把 cancel 从内层 save 偏转掉、但 job task 自己仍 `await` 着它），save 完成后 job task 才继续重抛 `CancelledError` 结束。所以「`gather` await job task」**传递性地** await 了 save——save 是被 job task **await 的**（非 detached），drain 的作用是让主协程在关闭事件循环前等 job task（连同它 await 的 shielded save）真正跑完。**纠正一个易误解的心智模型**：不要把 save `create_task` 成游离写再「靠 drain 维持 loop」——那才是丢失模型；正确是「save 被 job task await + drain 等 job task」。**即 shield（防 cancel 偏转到内层 save）+ drain（主协程等 job task 连带 save 完成）两者缺一不可**。

**可测性（tasks 5.6 据此写可执行验收，不留口号）**：用**阻塞型 FakeBackend**（`messages_create` 内 `await asyncio.Event().wait()` 永久挂起，模拟长跑 job）+ `GRACE_SECONDS` 注入极小值（如 0.05s）+ RunStore 注入临时 `runs.db` + 触发 `graceful_stop`；**观测序列**：`asyncio.wait` 超 grace → cancel pending task → 主协程 `await asyncio.gather(*pending, return_exceptions=True)` drain（其内 shield 写完 `daemon_stopped`）→ `graceful_stop` 返回后**再查 RunStore** 断言恰一条 `daemon_stopped` 行已落库（**不靠额外 sleep / 不靠 timing 侥幸**——drain 保证返回即写完）。另一用例用正常 FakeBackend（pipeline 秒回）+ 正常 grace → 断言落真实状态（`ok`/`partial`/`failed_api_unavailable`）而非 `daemon_stopped`。两者都不靠真实定时/真实进程。

**否决**：在 job 启动时先写一行 `running` 占位再 reconcile——`running` 不在 8 值内、且引入孤儿行回收复杂度；finally/except 落状态更简单且不脏库。

### D-6：manifest 加载时校验（fail-loud），不留到触发时
`scheduler/loader.py` 扫 `schedules/*.yaml`，逐个 `ScheduleManifest.model_validate` + 语义校验：cron/interval **恰一个**、timezone 经 `zoneinfo` 合法、`name` 唯一且为合法 job_id、`targets` 非空且**每个都在 `TargetRegistry`**、`intent` 非空。任一非法 → 在 `schedule list`/`daemon`/`trigger` 启动即 raise（指出文件名 + 字段 + 原因），**不**静默跳过该文件、**不**等到 fire 时才崩。理由：CLAUDE.md §4.9 doctor 范式 + §4.6「Scheduler 不是黑盒」。

### D-7：固定调度策略 `coalesce=True` / `max_instances=1` / 有限 `misfire_grace_time`
- `max_instances=1`：上次未跑完，新触发不并发启动 → 引擎跳过，**我方拦截并记 `skipped_due_to_running`**（APScheduler 默认是静默 warning，我们挂 listener 落 Run）。
- `coalesce=True`：daemon 休眠/卡顿后积压的多次触发合并为一次跑（不补跑 N 次）。
- `misfire_grace_time`：**固定默认定值**（M4 不做 per-manifest 配置）——interval job = `max(30, interval_seconds // 2)` 秒（取 interval 时长一半、下限 30s）；cron job = 固定 `300` 秒（保守 5 分钟，覆盖机器短暂休眠/卡顿而不致把正常延迟误判 missed）。超窗 → APScheduler misfire → listener 落 `missed`。`GRACE_SECONDS`（D-5 停机 grace，与 misfire 无关）固定默认 `30` 秒。
- listener 职责边界（避免与 job 体双写同一 Run）：`EVENT_JOB_MISSED → missed`、`max_instances` 拒启 → `skipped_due_to_running`、`EVENT_JOB_ERROR → failed`。**`EVENT_JOB_EXECUTED` 不写 Run**——`ok`/`partial`/`failed_api_unavailable` 这些「job 体真正跑了」的状态由 **job 执行体内部**「先 `ReportStore.save` 再写带 report_id 的 Run」落库（D-3b/D-2b）；`EVENT_JOB_EXECUTED` 至多做日志，不重复落 Run。即：listener 只负责「job 体根本没机会跑/没正常返回」的三种调度层状态，job 体负责其余。

### D-8：`schedule` 是 cron / interval 二选一的判别结构
`ScheduleManifest.schedule` = `cron: str`（**标准 5 段 crontab**：minute hour day-of-month month day-of-week）**或** `interval: {weeks?/days?/hours?/minutes?/seconds?}`，二者互斥（`model_validator` 强制恰一个）+ 顶层 `timezone`（cron 必用，interval 也带以统一展示）。映射到 APScheduler `CronTrigger.from_crontab(cron, timezone=tz)` / `IntervalTrigger(**interval, timezone=tz)`。**限定 5 段而非 6 段**：APScheduler 的 `CronTrigger.from_crontab()` 只接受标准 5 字段；6 段（含秒）需走 `CronTrigger(second=..., ...)` 另一条构造路径——M4 不引入秒级 cron（秒级精度用 interval 表达即可），故 manifest 的 `cron` 字段校验为标准 5 段，避免「文档说 6 段但 `from_crontab` 实现不出来」的不可实现契约。秒级 cron 留后续（非目标）。

### D-9：`notify` 占位、`trigger` 复用同一 job 体
manifest 的 `report`（`format: Literal["md","json"]="md"` + `diff_with_last: bool=False`，字面量与既有 `inspect`/`reports` CLI 对齐、禁 `markdown`/`html`）：`format` 决定渲染格式（消费）；`diff_with_last` 在 **M4 仅解析为类型化字段、不消费**——`run_diagnosis_pipeline` / `Report.from_inspector_results` 不据它在组装期自动 diff、不填 `baseline_ref`、不嵌 diff section（regression diff 是 persisted reports 上的 post-hoc `compute_diff`/`reports diff`；自动 diff/嵌入报告是 M5 非目标，见上「非目标」）。`diff_with_last` 与 `notify` 同属占位字段，为 M5 预留语义。`notify` 解析为类型化但**惰性**字段（M4 不消费、不验 secret、不发送），`Run.notify_results` 恒 `[]`。`schedule trigger <name>` 手动立即跑一次 = 调与定时 job **同一执行体**（保证手动/定时同语义），便于 CI 不靠真实时钟测全链。

### D-10：doctor schedule 检查落 `checks.schedules`（cli-foundation add-only 政策内，无 MODIFIED delta）
核验 `cli-foundation/spec.md:44-46`：doctor `--json` 是**封闭顶层 schema** `{version, timestamp, checks:{<id>:{status, detail, ...}}, ready}`，每个检查项必须在 `checks` 命名空间下，`status` 取既有 5 值 `{ok, present, missing, unreadable, error}`；演进政策明示「在既有 `checks` 内 add-only 新增 check_id / 在 check 内加 optional 字段不 bump version、不 breaking；改 required 字段或加顶层字段才是 breaking」。

⇒ `cli/doctor.py` 加 `_check_schedules(settings)`，产出落为 **`checks.schedules`** 这一新 check_id（**不是**顶层 `.schedules`）：`status` = manifest 加载失败 → `error`、否则 `ok`；next_fire_time / 最近 N 次 Run 状态分布塞进该 check 的 `detail` 或 add-only optional 字段。接入 `_build_report` 与 `_render_human` + `--json`。**这完全落在 add-only 政策内 ⇒ 无需 `cli-foundation` MODIFIED delta**（proposal Demo Path 的 `jq` 路径已同步改为 `.checks.schedules`）。若实现期被迫改 required 字段或加顶层字段，则补 `cli-foundation` MODIFIED delta。

### D-11：`ReportStore.save` 的 orphan 边界——`partial` + 记录存储形态，`report_hash` 兜底对账
核验 `reporting/store.py:161`：`save` 在 SQLite INSERT 失败时重试一次后**降级写 orphan JSON 文件**，返回 `SaveResult(stored_as_orphan=True, orphan_path=...)`，且 `meta.status="stored_as_orphan"`——此时 report **不在 reports.db**，`ReportStore.get_run(report_id)` 返回 `None`。runner 必须显式处理这条边界：`stored_as_orphan` 仍是「有 Report」（§7 表把 `stored_as_orphan` 列在 partial/Report-存在），故落 `Run(status=partial, report_id=<run_id>, report_hash=<指纹>, report_storage="orphan")`。**注记载体定死为 `Run.report_storage: Literal["db","orphan"] | None` 专用字段，不复用 `error`**（`error` 语义是「失败时的简短脱敏错误」，partial+orphan 非失败，塞 `error` 会污染语义；且二选一会让 Run schema 不确定）。`report_storage` 已列入 Run 字段集 + 不变量④（`report_storage is not None ⇔ status ∈ {ok,partial}`）。正常入库 → `"db"`。使 `schedule status` 能区分「db：report_id 必可 get_run 取回」与「orphan：需走 orphan 文件」。spec scenario「report_id 可经 `ReportStore.get_run` 取回」**仅适用于 `report_storage="db"` 的正常保存**，已在 spec 显式限定。**否决**：把 orphan 当 ok 静默写——会产出一条指向 get_run 取不到的 Run，违反「Run 不指向不存在的 Report」承诺。

### D-12：daemon/run 启动翻转 `is_daemon_mode`，复用 `create_backend` 既有安全门 seam
核验 `agent/backend.py:453`：`is_daemon_mode(settings)` M2 **恒返回 False**，docstring 明示「M5 Scheduler 翻转它」；`create_backend`（:471）**仅当 `is_daemon_mode(settings)` 为 True** 才调 `backend.ensure_safe_for_daemon()`。⇒ 本提案是那个翻转点：`schedule daemon` / `schedule run` 启动时**经 settings 注入 daemon 上下文标志**使 `is_daemon_mode(settings)` 返回 True，从而 `create_backend` 内部既有 gate 自然 fire、`ClaudeSubscriptionBackend` 的 `ensure_safe_for_daemon()` raise `BackendDaemonUnsafe`、daemon 拒启（exit 非零）。**否决**：在 scheduler 里平行再调一次 `ensure_safe_for_daemon()`——会让 `create_backend` 既有 gate 成死代码、两处判定可能分叉。具体翻转机制（settings 字段名 / 是否 contextvar）tasks 阶段定，但**必须经既有 `is_daemon_mode` seam**而非绕过。

## 风险 / 权衡

- [D-2 迁移触及 M3 代码] → 以「移动 + 重导出」最小化，迁移后全量回归 + 确认 `cli/_intent` 既有测试零改动通过；若迁移牵连过广，回退为 scheduler 直接 import `cli._intent` 并在 design 记债。
- [daemon_stopped 仅覆盖「被硬切的 in-flight」，SIGKILL 不留记录] → Run 记录**只在终态写**（D-5 否决 start-row 占位：`RunStatus` 恰八值、无「进行中」态，写占位行会破坏八值对齐与 `report_id ⇔ status` 不变量）。因此进程被 `SIGKILL`（-9 不可捕获、无 finally）中断的 in-flight job **不产生任何 Run 记录**——该次触发从台账缺失。**这是单进程内存调度在无 start-row WAL 下的已知限制，M4 显式接受并写进 spec 契约**（不假装能 stale-标注/恢复：既不写「进行中」start-row 占位，自然也无「进行中残行」可供 stale 查询，所以上一轮设想的「按时间阈值标 stale 悬空行」是建立在不存在的前提上、已删除。`finished_at` 仍按 §7 保留 `datetime | None`，本提案不对各状态的 finished_at 是否为 None 作额外断言，以免与字段 Optional 声明冲突）。SIGKILL 残留的对账/恢复需 start-row WAL，超出 M4 范围（非目标）。优雅 SIGTERM 的硬切 in-flight 仍正常落 `daemon_stopped`（D-5 shield+drain），不受此限制影响。
- [APScheduler misfire/coalesce 语义边界] → 用其官方 event listener 而非自己算时间窗，避免重造时间逻辑出错；测试用 `trigger` + 受控注入而非真实等待。
- [Run / Report 跨库软引用一致性] → `report_id` 仅软引用；`ReportStore.save` 成功后才写 `Run(ok/partial, report_id=...)`，顺序保证不会出现「Run 指向不存在的 report」；orphan 保存（SQLite 写失败降级写 JSON）按 D-11 显式落 `partial` + 注记存储形态，不当 ok 静默写；反向 orphan（report 在但 run 写失败）记为可接受的台账缺失。
- [并发：同一事件循环内多 job 同时跑] → 复用 M3 per-run 依赖注入（registry/ctx/clock 闭包绑定），无 module-global 可变状态；`max_instances=1` 仅限同名 job。

## 迁移计划

1. 先建 `orchestration/pipeline.py` 迁 `run_diagnosis_pipeline` + `cli/_intent` 重导出（零行为变更，跑回归锁定）。
2. 加 `scheduler/{schema,loader,store,runner}.py`（manifest→loader→RunStore→runner 依赖序）。
3. 加 `cli/schedule.py` 子命令 + 注册到 CLI app。
4. doctor 集成（落 `checks.schedules`，D-10 已确认 add-only、无 spec delta）。
5. `pyproject` 加 `APScheduler` runtime dep。
回滚：删 scheduler 包 + cli/schedule.py + doctor 增量 + 还原 pipeline 物理位置（重导出使还原无外部影响）。无持久化迁移（runs.db 是新库）。

## 待解决问题

- ~~`misfire_grace_time` 固定默认取值~~ **已定**（D-7）：interval = `max(30, interval_seconds // 2)`s、cron = `300`s；停机 `GRACE_SECONDS = 30`s。M4 均用固定默认、不做 per-manifest 配置（非目标）。SIGKILL 残留不产生 Run 记录（已知限制，见风险段），不引入 STALE_THRESHOLD。
- ~~`cli-foundation` 是否含 doctor 输出契约~~ **已确认**（D-10）：含（spec.md:44-46 封闭 schema + add-only 政策）；schedule 健康落 `checks.schedules` 完全在 add-only 政策内 ⇒ 无需 MODIFIED delta。
- `is_daemon_mode` 翻转的具体载体（settings 布尔字段 vs contextvar）——tasks 阶段定（D-12 已锁定「必须经既有 seam，不绕过」的约束，仅载体形式待定）。
