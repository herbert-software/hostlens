## 0. 范围说明（适用性裁剪）

- [x] 0.1 确认本变更**无 LLM 调用 / 无 Anthropic API / 无 Notifier / 无 Scheduler / 无 ExecutionTarget 新增**——故 tasks 规则里「prompt cache hit rate」「429 honor / API 宕机 degraded」「webhook 签名」「SIGTERM 优雅停机」「非 root 跑通」等验收**不适用**（pure data + 本地 SQLite 层）。在 PR 描述显式声明此裁剪。验收：PR 描述含该说明。

## 1. report-data-model schema 扩展（models.py）

- [x] 1.1 新增子模型 `ReportStatus`(str,Enum 八值；本提案只产出 ok/partial/stored_as_orphan，degraded_*/empty_response 仅定义不产出不测，spec 已声明)、`TokenUsage`(四字段默认 0)、`InspectorRun`、`BaselineRef`、`RootCauseHypothesis`、`ReportMeta`（按 specs/report-data-model 字段集；`target_type: str` 非 Literal）。验收：`pytest tests/reporting/test_models_m3.py -q` 覆盖每个模型字段集 + `ReportStatus("failed_api_unavailable")` raise。
- [x] 1.2 `Finding` add-only 加 `id: str | None=None` / `inspector_name: str | None=None` / `inspector_version: str | None=None`（保持 frozen + extra=forbid）。验收：legacy dict `{"severity":"info","message":"x"}` 经 `model_validate` 三字段为 None；`Finding(..., not_a_field=...)` 仍 raise。
- [x] 1.3 `Report` 加 `meta: ReportMeta | None=None` / `hypotheses: list[RootCauseHypothesis]=[]`，`schema_version` 放宽 `Literal["1.0","1.1"]`。验收：`schema_version="1.1"+meta` 与 `"1.0"+无meta` 均构造成功，`"2.0"` raise；legacy 1.0 JSON（无 meta 键）`model_validate` 成功且 `meta is None`。
- [x] 1.4 实现 `Finding.id` 指纹函数 `sha256(f"{inspector_name}\x00{inspector_version}\x00{message}")[:16]`（severity-agnostic 模块级 helper）。验收：同 (name,version,message) 异 severity → 同 id；异 message → 异 id；异 version → 异 id（`tests/reporting/test_finding_fingerprint.py`）。
- [x] 1.5 `mypy --strict src/hostlens/reporting/models.py` 0 错误（新模型全类型完整，禁止 `Any` 裸用）。

## 2. 工厂升级（from_inspector_results）

- [x] 2.1 工厂签名加 `target_id=None / target_type="local" / token_usage=None / status=None / schedule_name=None`；flatten 时用 `model_copy(update=...)` 给每个 finding 填 `inspector_name`/`inspector_version`/`id`（取自所属 InspectorResult）。验收：flatten 后 findings 身份字段正确（spec §场景:自动 flatten findings 并填充身份字段）。
- [x] 2.2 工厂组装 `meta`（run_id=str(report_id)、target_id 缺省=target_name、timestamp=started_at、duration=差值、inspectors_used 投影、**status 派生对齐 §9**（部分 timeout 不降级；全 timeout / target_unreachable / exception / requires_unmet → partial）、token_usage 缺省全零），写 `schema_version="1.1"`。验收：spec §场景:meta.run_id 等于 report_id / inspectors_used 机械投影 / 全 ok 派生 ok / 部分 timeout 仍 ok / 全 timeout partial / target_unreachable 等 partial / status 可覆盖 / token_usage 缺省全零 全绿。
- [x] 2.3 空 inspector_results 仍 raise `ValueError`（保持 M1 行为）。验收：spec §场景:空 inspector_results 列表 raise ValueError。

## 3. 脱敏与渲染同步（_redact.py / render_markdown.py）

- [x] 3.1 `_redact_finding` 重构调用加 `id`/`inspector_name`/`inspector_version`（原样透传不脱敏）；`redact_report_for_render` 透传并脱敏 `meta`（target_name/intent/target_id 等字符串过 redact_text）与 `hypotheses`（description/suggested_actions）。验收：spec §场景:脱敏拷贝保留 Finding 身份字段 / 保留并脱敏 meta / legacy 无 meta 不崩。
- [x] 3.2 加守门测试：渲染/脱敏后 `meta` 与 finding 身份字段**非丢失**（防「漏改 _redact 静默丢字段」回归）。验收：`tests/reporting/test_redact_m3_fields.py`。
- [x] 3.3 `render_markdown.render` 加 `## 根因假设` 章节（空→`_暂无根因假设_`，位于 `## Findings` 之后）。验收：spec §场景:无假设时显示占位 / 根因章节位于 Findings 之后。
- [x] 3.4 密钥脱敏测试：store 落盘 JSON 不含明文密钥（见 4.2，密钥不进持久化 sink）。

## 4. report-persistence（store.py）

- [x] 4.1 `ReportStore`（注入 db 路径）：建表 `runs`（含 `finding_count INTEGER` 列）+ 索引、WAL、`sqlite3` 包 `asyncio.to_thread`；`save(report) -> SaveResult`：blob 存 `render_json.render(report)`（已脱敏），**索引列从内存 `report` 投影**（target_id/target_name/status/timestamp/schema_version 取自 meta，finding_count=`len(report.findings)`；非反解 JSON），要求 `meta is not None`（否则 raise）。验收：spec §场景:save 落盘并可回读 / save 拒绝缺 meta 报告 / finding_count 索引正确 / run_id 主键唯一。
- [x] 4.2 落盘脱敏验证：含 `sk-...` 的报告 save 后库中 `report_json` 不含明文。验收：spec §场景:落盘内容已脱敏（密钥不进存储 sink）。
- [x] 4.3 orphan 降级 + `SaveResult`：INSERT 失败 1 次重试后，先把 `meta.status` 改写 `stored_as_orphan`（frozen 嵌套 `model_copy`）再 render_json 写 `orphan_reports/<run_id>.json`（§9 一致；`stored_as_orphan` 唯一产出点），run_id 非合法 UUID 拒写（防穿越），返回 `SaveResult(stored_as_orphan=True, orphan_path=...)`；正常入库 `stored_as_orphan=False`。验收：spec §场景:主库不可写时落 orphan 并标记（orphan JSON meta.status==stored_as_orphan）/ 正常入库 stored_as_orphan 为 False / 非法 run_id 不写文件。
- [x] 4.4 定义 `RunIndexRow`（extra=forbid，字段 run_id/timestamp/status/finding_count）；查询 API：`list_runs(target_id,limit)`（按总序 `(timestamp DESC, rowid DESC)`）/ `get_run(run_id)`（库内行必带 meta，不重建）/ `latest_ok_baseline(target_id, schedule_name=None, before_run_id=None)`（**总序用 `rowid` tie-break**，before_run_id 在总序上严格早于它选基线、防自基线；`inspector_versions` 从基线 blob 的 `meta.inspectors_used` 投影**不留空**）。验收：spec §场景:list_runs 倒序+limit / get_run 不存在返回 None / latest_ok_baseline 跳过非 ok / 排除当前 run / **时间戳并列按 rowid 选基线** / 无 ok 返回 None。
- [x] 4.5 存储边界验收（裁剪版）：明确**无自动删除 / 无 retention**（proposal 非目标 7），orphan 不丢；落盘只增不删。验收：测试断言 save 不触发任何删除；retention「保留策略 / 压缩降存储」标注为 non-goal（本提案不实现，记 docs）。
- [x] 4.6 `mypy --strict` 通过；store 模块无 module-level 全局连接（依赖注入，便于测试）。

## 5. report-regression-diff（diff.py）

- [x] 5.1 `RegressionDiff`（字段 `baseline_meta`（**非** `baseline_ref`）/ added / resolved / changed_severity / inspector_upgraded / dst_boundary_crossed / `diff_skipped_reason` 闭集 Literal）/ `FindingFingerprint` / `SeverityChange` 模型（extra=forbid）。验收:spec §场景:RegressionDiff 拒绝未声明字段 / diff_skipped_reason 是闭集。
- [x] 5.2 `compute_diff(baseline,current,*,force=False)` 规则顺序：**meta=None 前置(任一侧 `meta is None` → missing_finding_ids，在任何 `.meta.` 解引用前)** → per-target 隔离(raise) → finding 身份完整性(id=None → missing_finding_ids) → 基线状态门槛(baseline_not_ok) → schema 对齐(schema_changed) → inspector 版本对齐(排除+inspector_upgraded) → 指纹集合差。`baseline_meta` 当且仅当 `baseline.meta is not None` 时非 None（与 current.meta 无关；仅 `baseline.meta is None` 时为 None）。验收:spec report-regression-diff `compute_diff` 全部场景全绿（含 meta=None 不 deref / None id 跳过 / force 覆盖 / 版本升级排除；`tests/reporting/test_diff.py`）。
- [x] 5.3 边界用例：同报告 diff 自身全空；severity 变化进 changed_severity 不进 added/resolved；版本升级排除。验收：对应 spec 场景。
- [x] 5.4 `mypy --strict` 通过。

## 6. CLI（reports 子命令组 + inspect --persist）

- [x] 6.1 `cli/reports.py` Typer sub-app：`list <target> [--json]` / `show <run_id> [--format md|json]` / `diff <a> <b>` | `diff --target <t> [--baseline last_success] [--force]`；在 `cli/__init__.py` `add_typer(name="reports")`。验收:spec §场景:show 未知 run 退出码 3 / list 空历史退出码 0 / diff 未知 run 退出码 3 / 无基线退出码 0。
- [x] 6.2 `reports list --json` 输出 schema 稳定性 snapshot（run 索引行字段集固定）。验收:`tests/cli/test_reports_cli.py` + snapshot；`--json` 输出 `json.loads` 成功且字段集稳定。
- [x] 6.3 退出码契约：not-found→3、单行 stderr 无 traceback；read-only 命令无 `--yes` 概念（reports 全只读；`inspect --persist` 写**本地** store 非远端状态，不需 --yes/审批，PR 说明此裁剪）。验收:CLI 测试覆盖退出码 0/3。
- [x] 6.4 `hostlens inspect <target> --inspector <name> --persist`：产出 `Report`（已有 from_inspector_results 路径）后 `store.save(report)`；落 orphan 时退出码非 0。**仅 `--inspector` 路径**——`--intent`/`demo run`（PlannerResult，无 Report）不暴露 `--persist`。注：`meta.status`（报告 banner，§9 派生）与 inspect CLI **退出码**（任一非 ok inspector → exit 2，inspect-cli-command spec）是**独立信号**、不必相等（部分 timeout 时 status 可为 ok 而 exit 仍 2）——勿在测试里把二者绑定。验收:spec §场景:--persist 后报告可被 reports list 看到 / --intent 与 demo run 不接受 --persist。

## 7. fixture / snapshot 迁移

- [x] 7.0 **稳定 agent-facing `run_inspector` 输出**（兑现 proposal §对外契约「Agent tool 投影 schema 仅取必要字段，不扩大 Agent 可见面」；**实现期发现**：Finding 加字段后 `RunInspectorOutput.findings` 的 `model_dump()` 多出 `id`/`inspector_name`/`inspector_version`(null)，使 Planner→LLM 的 tool_result 变化 → 既有 incident/demo/planner cassette 全 miss）：给 `src/hostlens/tools/schemas/run_inspector.py` 的 `RunInspectorOutput` 加 `@field_serializer("findings")` 排除 M3 身份字段，使 agent-facing tool_result 与 M2 **逐字一致** → cassette **无需重录**仍命中。`FindingSummary = Finding` 类型别名**不变**（保留 locked spec §需求:Finding type alias 路径）。验收：`tests/incidents/*` + `tests/demo/test_demo_*` + `tests/agent/test_planner_replay.py` 全绿（**不改任何 cassette**）。
- [x] 7.1 **确认 incidents/demo snapshot 不变**（7.0 修复 cassette 后成立）：它们由 `tests/incidents/_harness.py:project_planner_result` 投影（只取 severity+message+tags，**不**走 render_markdown/render_json），新字段/根因章节不波及。验收:不改这些 snapshot 文件，`pytest tests/incidents tests/demo -q` 仍全绿（若意外变动说明投影漏隔离新字段，需排查）。
- [x] 7.2 更新**经 render 的 sink**：`inspect` 路径 `.ambr`（`from_inspector_results` 现写 `schema_version="1.1"` + meta + 根因章节 + render_json 新字段）+ 直接断言 `Report`/`Finding`/`schema_version`(1.0→1.1) 的 reporting/cli 单测——**已知至少含** `tests/cli/test_inspect_streams.py`、`tests/cli/test_inspect_demo_path_4_cases.py`（名字含 demo 但在 `tests/cli/` 走 render，需改，**勿**与「demo snapshot 不变」混淆）、`tests/reporting/test_report_factory.py`；核对 `doctor --json` 不含报告 schema 字段（若含则一并更新）。验收:`pytest tests/reporting tests/cli -q` 全绿 + `hostlens doctor --json` schema 稳定。
- [x] 7.3 新增 `tests/incidents/test_diff_replay.py`：**直调 `InspectorRunner.run`（+ `ReplayTarget`）+ `from_inspector_results`**（**不**复用 `_harness.build_incident_planner` 的 Agent 路径），组装 baseline(无 critical) 与 current(`linux.memory.pressure`+`linux.kernel.oom_killer` 产 critical) 两份 Report，断言 `compute_diff(...).added` 含 critical；另断言 hello.echo 两次 inspect --persist 的空 diff。验收:spec report-regression-diff §场景:同输入两次机械巡检 diff 为空 / 不同严重度场景 diff 出 added critical。

## 8. 收尾验证

- [x] 8.1 全量 `mypy --strict src/` 0 错误。（71 files, 0 errors）
- [x] 8.2 `pytest -q -m 'not live'` 全绿；core/reporting 模块覆盖率 ≥80%（store/diff 新模块 ≥85%）。验收:`pytest --cov=hostlens.reporting --cov-report=term-missing`。（1492 passed / 0 failed；reporting TOTAL 97%：store 97% / diff 100% / models 100%）
- [x] 8.3 `ruff check . && ruff format --check .` 全清；`pip-audit --skip-editable` 无新增漏洞（无新依赖，预期通过）。（245 files formatted；No known vulnerabilities）
- [x] 8.4 端到端 Demo Path（proposal §Demo Path）：`hostlens inspect local-host --inspector hello.echo --persist` ×2 → `reports list local-host` → `reports diff` 出空 diff。**验收以自动化测试为准**（7.3 `test_diff_replay.py` + reports CLI 测试断言 added critical 与空 diff）；手动跑通仅作 PR 附加证据，不作唯一验收。（实测离线无 API：2 runs 落盘，diff --target exit 0）
- [x] 8.5 OpenSpec 归档就绪检查：`openspec-cn validate add-report-persistence-and-diff` 通过；spec delta 标题全中文（`### 需求:`/`#### 场景:`/`**当**`/`**那么**`）。
