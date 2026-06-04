## 1. 编排函数上提（design D-2，先做，零行为变更）

- [x] 1.1 新建 `src/hostlens/orchestration/__init__.py` + `src/hostlens/orchestration/pipeline.py`，按 design D-2 迁移清单迁入 `run_diagnosis_pipeline` + 其私有纯编排辅助（`_seed_findings_from_snapshot` / `_seeding_sort_key` / `_assemble_report` / `_sum_loop_usage`）；Rich UI / rendering / `run_intent_diagnosis` 留在 `cli/_intent.py`（不改逻辑）
- [x] 1.2 `cli/_intent.py` 改为 `from hostlens.orchestration.pipeline import ...` 重导出**所有被外部直接 import 的迁移符号**（`run_diagnosis_pipeline` 及被 `cli/demo.py` / incidents generator / 既有测试引用的私有 helper），保留既有公开名、import 路径不破
- [x] 1.3 验收：全量 `pytest tests/ -m 'not live'` 绿（尤其既有 `--intent` / `cli/_intent` / `demo` / incidents generator 测试**零改动**通过），确认上提是纯物理迁移
- [x] 1.4 `mypy --strict src/hostlens/orchestration/ src/hostlens/cli/` 通过

## 2. Schedule manifest schema + loader（4.1，spec: schedule-manifest）

- [x] 2.1 `src/hostlens/scheduler/schema.py`：`ScheduleManifest`（`extra="forbid"`）+ `ScheduleSpec`（cron/interval `model_validator` 恰一个 + `timezone` zoneinfo 校验）+ `IntervalSpec` + `ReportConfig`（`format: Literal["md","json"]="md"` 消费 / `diff_with_last: bool=False` M4 仅解析不消费 / `extra="forbid"`）+ `NotifyConfig`（占位，类型化但不消费）
- [x] 2.2 `src/hostlens/scheduler/loader.py`：扫 `schedules/*.yaml` → 逐个 `model_validate`；加载时语义校验（targets ∈ 注入的 `TargetRegistry`、**targets 恰 1 个**（M4 单 target，多 target fail-loud 拒）、name 跨文件唯一、intent 非空白），非法 fail-loud（错误含文件名+字段+原因）
- [x] 2.3 单测：合法 manifest 解析 / 未知字段拒绝 / cron+interval 互斥两向 / cron 非 5 段拒 / interval 全零或全省略拒 / 非法 timezone / report.format 非 md/json 拒 / diff_with_last 仅解析不消费（解析为 True 但不触发组装期 diff）/ targets 未注册 fail-loud / **多 target（≥2）fail-loud** / 单 target 正常 / name 重复 fail-loud / notify 占位不触发发送；验收 `pytest tests/scheduler/test_schema.py tests/scheduler/test_loader.py -q`

## 3. RunStatus + Run 模型 + RunStore（4.3，spec: scheduler-engine）

- [x] 3.1 `src/hostlens/scheduler/store.py`（或 `scheduler/models.py`）：`RunStatus(StrEnum)` 八值（对齐 ARCHITECTURE §7）；与 `reporting.models.ReportStatus` 独立
- [x] 3.2 `Run` Pydantic 模型，字段含留痕 `targets: list[str]` / `inspectors: list[str]`（有 Report 从 `report.meta.inspectors_used` 的 `.name` 投影）/ `report_hash: str | None`（`sha256(reporting.render_json.render(report).encode()).hexdigest()`）/ `report_storage: Literal["db","orphan"] | None = None` 与 `notify_results: list[object] = []`（占位恒空；用 `list[object]` 而非 `list[Any]` 避免触 CLAUDE.md §6「不允许 Any」红线；`NotifyResult` 是 M5 类型当前不存在，**禁止** M4 引用，M5 收紧为 `list[NotifyResult]`；不在 M4 新建 NotifyResult 避免侵入 M5 契约）；`model_validator(mode="after")` 强制四不变量：① `report_id is None ⇔ status ∉ {ok,partial}`；② `started_at is None` 当 `status ∈ {missed, skipped_due_to_running, budget_exhausted}`（对齐 §7 三值）；③ `report_hash` 仅 `status ∈ {ok,partial}` 可非 None；④ `report_storage is not None ⇔ status ∈ {ok,partial}`
- [x] 3.3 `RunStore`：独立 `runs.db`（默认 `~/.local/share/hostlens/runs.db`，`db_path` 可注入；WAL、async、无 module-global 连接；镜像 `ReportStore` 形态）+ 按 schedule_name/时间倒序列最近 N 条查询
- [x] 3.4 单测：八值集合 / 不变量边界（ok 无 report_id 拒、missed 带 report_id 拒、partial 带 report_id 过、skipped started_at 非 None 拒、missed started_at 非 None 拒、budget_exhausted started_at 非 None 拒、failed started_at 非 None 过、failed_api_unavailable started_at 非 None 过、无 Report 状态带 report_hash 拒、无 Report 状态带 report_storage 拒、partial 缺 report_storage 拒）/ report_hash 确定性（同一 Report 两次相等且 == `sha256(render(report).encode()).hexdigest()`）/ RunStore 写入后倒序取回 / runs.db 与 reports.db 文件分离；验收 `pytest tests/scheduler/test_run_model.py tests/scheduler/test_run_store.py -q`

## 4. APScheduler 封装 + job 执行（4.2，spec: scheduler-engine）

- [x] 4.1 `src/hostlens/scheduler/runner.py`：`AsyncIOScheduler` 封装，每 manifest 注册一 job（`job_id=name`，cron→`CronTrigger.from_crontab`/interval→`IntervalTrigger`，`max_instances=1`/`coalesce=True`/`misfire_grace_time` **定值**：interval=`max(30, interval_seconds//2)`s、cron=`300`s，见 design D-7）
- [x] 4.2 job 执行体：`await run_diagnosis_pipeline(..., planner_result_sink=<捕获 terminal_status>)`（从 `orchestration` 导入；用既有 sink 形参做无-Report 判别）→ 有 Report 按 `Report.meta.status` 映射（ok→`ok`、降级类含 `degraded_token_budget`/`degraded_max_turns`→`partial`），`ReportStore.save` 后落带 `report_id`+`report_hash` 的 Run；返回 `None` 且 `terminal_status==failed_api_unavailable`→`failed_api_unavailable`，否则（空采集）→`failed`(error 注记)；**budget_exhausted 不由本映射产生**；**orphan**（`SaveResult.stored_as_orphan`）→落 `partial`+`report_storage="orphan"`（正常入库 `report_storage="db"`），不当 ok 静默写；**先 save 成功再写带 report_id 的 Run**
- [x] 4.3 APScheduler 事件 listener → RunStatus：`EVENT_JOB_MISSED→missed`、`max_instances` 拒启→`skipped_due_to_running`、`EVENT_JOB_ERROR→failed`（单 job 异常不崩 daemon）；**`EVENT_JOB_EXECUTED` 不写 Run**（ok/partial/failed_api_unavailable 由 job 体内部落，避免双写，见 D-7）
- [x] 4.4 单测（FakeBackend + 手动 trigger / 受控时钟，不靠真实定时）：正常产 Report 留痕 ok / token 预算耗尽产 partial Report（degraded_token_budget）非 budget_exhausted / 后端不可用(terminal_status=failed_api_unavailable)无 Report→failed_api_unavailable / 空采集(terminal_status≠failed_api_unavailable)→failed 不误记 / orphan save→partial+注记 / max_instances 拒并发记 skipped_due_to_running / job 异常记 failed 且调度器存活；验收 `pytest tests/scheduler/test_runner.py -q`

## 5. schedule CLI + daemon 优雅停机（4.4，spec: schedule-cli-command）

- [x] 5.1 `src/hostlens/cli/schedule.py`：`list`（含 next_fire_time）/ `run`（前台）/ `daemon` / `trigger <name>` / `status`（最近 N Run 状态分布）；注册到 `hostlens` CLI app；所有子命令先加载+校验 manifest，非法 fail-loud 退出非零
- [x] 5.2 `trigger` 复用与定时 job **同一执行体**；未知 name fail-loud
- [x] 5.3 SIGTERM/SIGINT handler（按 D-5）：job 体入口（任何 await 前）`inflight.add(asyncio.current_task())` + `finally: discard`（AsyncIOExecutor 内部建 task 不交还，只能 job 体内 current_task 注册）；停机 = `scheduler.pause()` → `asyncio.wait(inflight, timeout=GRACE_SECONDS=30)` → 超 grace 的 pending `task.cancel()` → **主协程 `await asyncio.gather(*pending, return_exceptions=True)` drain** → `scheduler.shutdown(wait=False)`；**信号 handler 幂等**（停机中再收信号忽略、不二次 cancel）。**不**用 `asyncio.wait_for(scheduler.shutdown(wait=True))`（APScheduler `shutdown()` 同步返回 None、非 awaitable、同 loop wait 会死锁）。job 体 `try/except asyncio.CancelledError` → 仅被强制中断落 `daemon_stopped`（优雅完成不误记）；**终态写必须 `await asyncio.shield(run_store.save(...))`**（防 cancel）**且主协程 drain**（防 loop 早关），两者缺一行丢失
- [x] 5.4 daemon/run 启动**经 settings 注入 daemon 上下文标志使 `is_daemon_mode(settings)` 返回 True**，从而 `create_backend` 既有 gate 触发 `ensure_safe_for_daemon()`；订阅 backend 拒绝启动（exit 非零，非仅 warn）；**不在 scheduler 平行另调 ensure_safe_for_daemon**（见 D-12）
- [x] 5.5 daemon 日志写文件（默认 `~/.local/share/hostlens/logs/scheduler-daemon.log`，可配置覆盖，启动时 stderr 打印实际路径）+ structlog json 脱敏（不泄露凭据值）
- [x] 5.6 单测：list 显示 next_fire_time / 非法 manifest fail-loud / trigger 产 Run+Report 且 reports show 可取回 / trigger 未知 name 报错 / **SIGTERM 等 in-flight（正常 FakeBackend 秒回 + 正常 grace）完成落真实状态非 daemon_stopped** / **强制中断（阻塞型 FakeBackend：`messages_create` 内 `await asyncio.Event().wait()` 永久挂起 + 注入极小 GRACE_SECONDS 如 0.05s + 触发停机）落 daemon_stopped** / 订阅 backend daemon+run 均拒启 / job 执行中不写「进行中」占位行（Run 只在终态出现）/ SIGKILL 残留不留 Run 记录（模拟：不调用终态写即视为被杀，RunStore 无该行）/ status 默认列最近 N 条+状态分布 / status 空历史 exit 0（人类提示/`--json` 空结构）/ status `--name` 未知报错 / daemon 日志写入指定文件路径且不含密钥；验收 `pytest tests/cli/test_schedule.py -q`

## 6. doctor 集成（4.5，design D-10：add-only 可选，无需 spec delta）

- [x] 6.1 （design D-10 已确认）schedule 健康落 `checks.schedules` 新 check_id（status 用既有 5 值枚举、附加信息走 optional 字段），完全在 cli-foundation add-only 政策内 ⇒ 无需 MODIFIED delta；本任务仅复核实现未越界（未改 required 字段、未加顶层字段）
- [x] 6.2 `cli/doctor.py`：`_check_schedules(settings)` —— manifest 加载错误数 / 各 job next_fire_time 合理性 / 最近 N 次 Run 状态分布；接入 `_build_report` + `_render_human` + `--json`，落为 `checks.schedules`（status=加载失败→`error`/否则 `ok`）
- [x] 6.3 单测：doctor 有/无 schedules 目录、含非法 manifest、含最近 Run 的输出经 `.checks.schedules` 可见；`doctor --json` 顶层 schema 仍恰为 `{version,timestamp,checks,ready}`、满足 cli-foundation 既有 required 字段契约；验收 `pytest tests/cli/test_doctor.py -k schedule -q`

## 7. 依赖与收尾

- [x] 7.1 `pyproject.toml` 加 `APScheduler` runtime 依赖（pin 合理下界）；确认 lock / CI 装得上
- [x] 7.2 `mypy --strict src/hostlens/scheduler/ src/hostlens/cli/schedule.py src/hostlens/orchestration/` 通过（无 `Any` 泄漏）
- [x] 7.3 全量 `pytest tests/ -m 'not live'` 绿（1780 passed / 12 skipped）
- [x] 7.4 `openspec-cn validate add-scheduler` 通过
- [x] 7.5 Demo Path 跑通（离线可验证部分真跑：list 见 next_fire_time / status 空历史 exit 0 + `--json` 空结构 / doctor `--json` 见 `checks.schedules` / trigger 未知 name fail-loud / 非法 targets 配置干净 fail-loud；trigger→Report 的真实 LLM 调用与 daemon SIGTERM 优雅停机由 FakeBackend / graceful_stop 单测覆盖，未起真实 API / 真实进程）
- [x] 7.6 把 TODO.md M4 的 4.1–4.5 勾选为 `[x]`；M3.6 Path 1（tolerate-inbound-thinking，已 merged+archived #53）各项标 `[x]`
- [x] 7.7 PR 前对抗性 review（CLAUDE.md §5.3）—— 已跑 `/review-loop` 3 轮对抗性 review（Codex + Code Reviewer + Reality Checker），收敛到三方 APPROVE；修了 6 项（listener 游离 task 丢行/吞异常、Run.error 脱敏、name 路径分隔符校验、RunStore 混 tz 排序、去防御兜底、Run 时间戳 tz-aware 校验）+ 补测试
