## 上下文

M1 落地的 `report-data-model`（`Severity` / `Evidence` / `Finding` / `Report`）是经 incident-pack 8 场景 snapshot 验证过的**精确实现**：`Evidence` 是 kind-discriminated 的十字段模型、`Finding` 严格四字段且 frozen、`Report` 工厂 `from_inspector_results` 机械 flatten。而 `docs/ARCHITECTURE.md §10` 的 schema 是 **早期草图**——它的 `Evidence{inspector,snippet,parsed}`、无 `tags` 的 `Finding`、完全 collapse 进 `meta` 的 `Report`，都与已落地实现**实质冲突**。本提案不能照搬 §10，必须在「尊重 M1 已锁契约」与「补齐 §10 想要的 meta/hypotheses/diff 能力」之间取 add-only 路线。

同时 `_redact.py` 的 `redact_report_for_render` 是**逐字段重构** `Report`/`Finding`/`Evidence`/`InspectorResult`——任何给 `Finding`/`Report` 加的新字段，**必须同步在 `_redact.py` 的重构调用里透传**，否则脱敏拷贝会静默丢掉新字段，渲染/落盘就看不到它们。这是本提案最容易踩的迁移坑。

`report_data_model` spec 已**预埋** add-only 扩展位（`Finding` 需求注明「M3 add-only 扩展 id/inspector_run_id/fingerprint」、`schema_version` 注明「M3 扩展为 Literal["1.0","1.1"]」、`Evidence` 注明「M3 加新 kind 时 MODIFIED 同步映射表」），所以 MODIFY 是放宽而非推翻。

## 目标 / 非目标

**目标：**
- 给 `Finding`/`Report` 加 add-only 身份与元信息字段，使其能承载根因假设、支撑跨 run diff，且 M1/M2 构造方与已存 JSON 零改动可加载。
- SQLite 持久化报告（存**已脱敏** JSON），提供 `reports list/show` 与基线查询 API，含 `stored_as_orphan` 不丢报告降级。
- regression diff 引擎：`added`/`resolved`/`changed_severity` + `inspector_upgraded`，防基线污染（版本/schema/per-target 对齐）。

**非目标：**（与 proposal 一致）不产生 hypotheses 内容、不实现 `RunStatus`/Scheduler、不实现 HTML 渲染、不实现 extended-thinking、不接 Notifier、不做 retention/`--no-redact`。

## 决策

### 决策 1：路线 A — 扁平字段保留 + 叠加 `meta`/`hypotheses`（而非 §10 collapse）

`Report` **保留** M1 全部字段（`report_id`/`schema_version`/`intent`/`target_name`/`inspector_results`/`findings`/`started_at`/`finished_at`/`metadata`）不动，**新增** `meta: ReportMeta | None = None` 与 `hypotheses: list[RootCauseHypothesis] = []`。

- **为什么不选路线 B（按 §10 collapse 进 meta）**：B 要改 `from_inspector_results` 全部调用点、`_redact.py`、`render_markdown`/`render_json`、8+1 份 snapshot 的结构，且 `Evidence`/`Finding` 的 §10 形状与 M1 实现根本不兼容——是推翻重写而非扩展，违反 `models.py:8/159` 的 add-only 承诺与 spec 预埋的扩展契约。
- **接受的代价**：`Report.target_name` 与 `meta.target_name`、`report_id` 与 `meta.run_id`、`started_at/finished_at` 与 `meta.timestamp/duration_seconds` 存在**字段冗余**。约定 **`meta` 为前进方向的权威源**，扁平字段保留仅为 M1/M2 既有 consumer 兼容；新 consumer（store/diff/未来 Diagnostician）一律读 `meta`。
- **`meta` 为何可空**：旧 `schema_version="1.0"` 的已存 JSON 没有 `meta`；`meta: ReportMeta | None = None` 让旧 JSON 仍可 `model_validate`。**所有新报告由 `from_inspector_results` 必定填充 `meta`**（并写 `schema_version="1.1"`）；`None` 仅出现在加载 legacy JSON 时（本 store 的 `save` 拒绝 meta=None，故库内行必带 meta；meta=None 只可能来自 orphan/外部导入，这类报告 finding `id=None` → diff 自动跳过，见决策 7）。
- **`schema_version`**：`Literal["1.0", "1.1"]`；填了 `meta` 即 `"1.1"`。`ReportMeta.report_schema_version` 是**报告内部 schema 版本**（§10 默认 `"1.0.0"`），与 `Report.schema_version` 是两个独立版本号——前者描述「报告整体结构版本」，后者是 M1 已有的顶层字段，保留语义不变。本提案 `meta.report_schema_version` 取 `"1.1"` 与 `Report.schema_version` 对齐，避免双版本号漂移困惑（§10 的 `"1.0.0"` 三段式不采用，统一两段式）。

### 决策 2：Finding 身份模型 — `id` 为 severity-agnostic 内容指纹，兼作 diff key

给 `Finding` add-only 增三字段（全部 `| None = None`，默认 None 保 legacy 兼容）：

- `inspector_name: str | None = None`、`inspector_version: str | None = None`：由 `from_inspector_results` 从**所属** `InspectorResult.name/.version` 填充（flatten 时每个 finding 记住来源 inspector）。§10 fingerprint 规则 2「inspector.name + version」要求 finding 自描述其 inspector，flatten 后的扁平列表才不丢这个关联。
- `id: str | None = None`：**确定性内容指纹** = `sha256(f"{inspector_name}\x00{inspector_version}\x00{message}").hexdigest()[:16]`，由工厂自动计算。**故意 severity-agnostic**（不含 severity），这样同一 finding 在两个 run 间 severity 变化时 `id` 不变 → diff 能识别 `changed_severity`（同 id、异 severity）。`id` 同时是 `RootCauseHypothesis.supporting_findings` 引用的 intra-report 锚点。

- **为什么确定性 hash 而非 uuid**：uuid 每次不同 → 跨 run 无法匹配、snapshot 不稳定。确定性 hash 让「相同 finding」跨 run 同 id，是 diff 匹配的基础，也让 snapshot 可复现。
- **为什么 severity-agnostic**：若 severity 进 hash，severity 变化会让同一问题在 diff 里表现为「一个 resolved + 一个 added」假象，无法表达「变严重了」。剔除 severity 后用 (同 id, 异 severity) 精确捕获 `changed_severity`。
- **已接受风险**：同一 inspector 在**同一 run** 内对**字面完全相同**的 message 产出两个不同 severity 的 finding → 同 id 出现两次（intra-report id 非唯一）。此为病态场景；hypotheses 引用该 id 时语义二义但无害（两 finding 同根因）。记为 Open Question。
- **message 易变性风险**：fingerprint 含整条 `message`，若 message 含易变值（如 `load 4.2`）会让每次 run 都 added/resolved。§10 理想是「关键字段哈希」（inspector 声明指纹字段），但那需要 per-inspector 元数据，**超出本提案范围**。本提案用「整条 message」近似，记为已知限制；demo path 因回放同一 cassette → message 逐字相同 → diff 干净，验收不受影响。

### 决策 3：`ReportMeta` 字段映射与暂定取值

`ReportMeta` 按 §10 字段集，但对 M3 现实做三处务实取值（记为 Decision，非偷懒）：

| 字段 | M3 取值 | 理由 |
|---|---|---|
| `run_id: str` | `str(report.report_id)` | M3 无 Scheduler，一次 inspect = 一个 run；M4 `Run.report_id` 将关联此值 |
| `target_id: str` | 暂 `= target_name` | 无独立稳定 id；引入稳定 `target_id` 留 M4。**diff per-target 隔离按此键**，故 M3 不具抗改名能力——已在 proposal Failure Mode 6 + spec 显式列为已知限制（**不**隐藏在 Open Question）；改名 → 基线重置，不误判 |
| `target_type: str` | **放宽为 `str`**（非 §10 的 `Literal[local/ssh/docker/k8s]`） | M2 demo 用 `ReplayTarget`，不在 §10 Literal 内；放宽为 str（docstring 列 canonical 值）避免 demo 持久化被卡，也为 M8 docker/k8s 与未来 target 类型留口 |
| `schedule_name: str | None` | M3 恒 `None`（CLI/MCP 触发无 schedule） | M4 Scheduler 填充 |
| `baseline_ref` / `diff_skipped_reason` | 恒 `None`（报告生成时不内嵌 diff） | diff 是独立 `reports diff` 动作，不在报告生成期跑；§10 注「定时巡检场景填写」属 M5 |

`inspectors_used: list[InspectorRun]`、`status: ReportStatus`、`token_usage`、`duration_seconds`、`timestamp` 见决策 4/5。

### 决策 4：`TokenUsage` 来源 — 字段对齐 `LoopResult.usage_totals`，本提案恒零

新增 `TokenUsage{input_tokens:int=0, output_tokens:int=0, cache_creation_input_tokens:int=0, cache_read_input_tokens:int=0}`（对齐 Anthropic usage 形状，全字段默认 0）。

- **本提案下 `token_usage` 恒为全零**：`--persist` 只接**机械 `--inspector` 路径**（决策 9），该路径无 LLM 调用 → `TokenUsage()`。
- **字段对齐 `LoopResult.usage_totals`（非 `usage`）**：经核实，`LoopResult` 暴露的是 `usage_totals: LoopUsage`（`loop.py`），**不是** `usage`；`MessageResponse.usage` 是另一层。`TokenUsage` 字段名/类型与 `LoopUsage` 对齐，使未来 Agent 路径装配 Report 时可 `TokenUsage(**loop_result.usage_totals.model_dump())` 投影——**但该装配属 `add-diagnostician-agent`，本提案不实现**（非目标 8）。
- **为什么默认全零而非 None**：让下游无需 None 分支；本提案只**定义** TokenUsage 形状并在工厂留 `token_usage` 覆盖入口，不搬运真实 token（机械路径无 token，见 proposal Cost 节）。

### 决策 5：`ReportStatus` 派生（本提案只产出三值）

`ReportStatus` enum 完整建模 §9/§10 八值（`ok`/`partial`/`degraded_no_planner`/`degraded_rate_limited`/`degraded_token_budget`/`degraded_max_turns`/`empty_response`/`stored_as_orphan`）。

- **本提案只真实产出并测试三值**：`ok`/`partial`（工厂派生**对齐 §9 Failure Semantics**：全 `ok` → `ok`；非 ok 仅为 `timeout` 且 ≥1 `ok` → `ok`（§9「Inspector 超时 → ok 除非全部超时」，单/部分超时不降级）；任一 `target_unreachable`/`exception`/`requires_unmet` 或全 `timeout` → `partial`）、`stored_as_orphan`（store orphan 降级回写，决策 6）。
- 其余五值（`degraded_*`/`empty_response`）在本提案下**仅定义为 enum 成员、不产出、不被测**——它们由 `add-diagnostician-agent`（消费 `LoopResult.terminal_status`）产出并测试。工厂留 `status` 覆盖入口供未来填，但本提案不实现 degraded 判定逻辑。spec 已把「不产出」显式写进 ReportStatus 需求，避免被当作可测需求漏测。

### 决策 6：持久化 — SQLite 存脱敏 JSON blob + 索引从内存 meta 投影 + orphan 降级

`reporting/store.py`，`ReportStore` 类（构造注入 db 路径，便于测试用临时库）：

- **库位置**：`$XDG_DATA_HOME/hostlens/reports.db`（缺省 `~/.local/share/hostlens/reports.db`）。WAL 模式。同步 `sqlite3` 包在 `asyncio.to_thread`。
- **表 `runs`**：`run_id TEXT PRIMARY KEY`、`target_id TEXT`、`target_name TEXT`、`schedule_name TEXT NULL`、`status TEXT`、`report_schema_version TEXT`、`timestamp TEXT`、`finding_count INTEGER`、`report_json TEXT`、`created_at TEXT`。索引 `(target_id, status, timestamp DESC)`。**`finding_count` 列**让 `reports list` 显示 finding 数无需加载 report_json。
- **`save(report) -> SaveResult`**：要求 `report.meta is not None`（否则 raise，索引无法投影）。`report_json` blob = `render_json.render(report)`（**已脱敏**，对齐 OPERABILITY §7.2）。**索引列从内存 `report` 投影**（`target_id`/`target_name`/`status`/`timestamp`/`report_schema_version` 取自 `meta`，`finding_count = len(report.findings)`），**不从脱敏 JSON 反解**——即便脱敏漏穿 meta，索引仍可靠（且决策 8 的 _redact 不变量保证脱敏拷贝保留 meta）。返回 `SaveResult{run_id, stored_as_orphan, orphan_path}`（**不用裸 str**）。
- **orphan 降级**：INSERT 失败 1 次重试后仍失败 → 先把 `meta.status` 改写 `stored_as_orphan`（frozen，嵌套 `model_copy`）再 `render_json`（与 §9 一致，这是 `stored_as_orphan` 的唯一产出点），写 `~/.local/share/hostlens/orphan_reports/<run_id>.json`（写前校验 run_id 合法 UUID 防穿越），返回 `SaveResult(stored_as_orphan=True, orphan_path=...)`，CLI 退出码非 0、报告不丢。
- **查询 API**：`list_runs(target_id, *, limit=20)`（返回 `RunIndexRow{run_id,timestamp,status,finding_count}`，extra=forbid）、`get_run(run_id) -> Report | None`、`latest_ok_baseline(target_id, *, schedule_name=None, before_run_id=None) -> BaselineRef | None`。**排序总序 = `(timestamp DESC, rowid DESC)`**——`meta.timestamp`(=started_at) 可并列（背靠背 inspect 同值）/ 非单调（NTP 回拨），用单调 `rowid` tie-break，**禁止**仅按 timestamp。`before_run_id` 给定时只在总序上严格早于它的 run 选基线（防自基线）。`BaselineRef.inspector_versions` 从基线 blob 的 `meta.inspectors_used` 投影 `name->version`（**不留空**，否则破 diff rule 5）。
- **legacy**：本 store `save` 拒绝 meta=None → 库内行必带 meta，`get_run` 不重建 meta；meta=None 只可能来自 orphan/外部导入（不在本提案范围），其 finding `id=None` → diff 自动跳过（决策 7）。

### 决策 7：Diff 算法 — 指纹集合差 + 版本对齐排除 + 防自基线

`reporting/diff.py`，`compute_diff(baseline: Report, current: Report, *, force=False) -> RegressionDiff`，规则顺序：

0. **meta 完整性前置**：`baseline.meta is None` 或 `current.meta is None`（legacy/orphan/外部导入）→ 在任何 `.meta.` 解引用**前**返回 `diff_skipped_reason="missing_finding_ids"`、空 diff（防 None deref 先于 id 检查爆 `AttributeError`；这类报告 finding 也必 id=None，归同一类别）。
1. **per-target 隔离**：`baseline.meta.target_id != current.meta.target_id` → raise `ValueError`（§10 规则 5）。
2. **finding 身份完整性**：任一侧含 `Finding.id is None`（直构未走工厂）→ `diff_skipped_reason="missing_finding_ids"`、空 diff（**禁止**用 None id 做集合差）。
3. **基线状态门槛**：`baseline.meta.status != "ok"` 且非 `force` → `diff_skipped_reason="baseline_not_ok"`、空 diff（不抛错，§Failure Mode 2）。
4. **schema 对齐**：`report_schema_version` 不一致 → `diff_skipped_reason="schema_changed"`、空 diff（§10 规则 3；M3 内新报告恒 `"1.1"`，故此规则只对 legacy-导入 meta 可触发）。
5. **inspector 版本对齐**（§10 规则 2）：某 inspector 两侧 `version` 不同 → 其 finding 全部排除出 added/resolved/changed_severity，name 进 `inspector_upgraded`。
6. **指纹集合差**：对版本对齐、id 非 None 的 finding，用 `Finding.id` 建两集合：`added`=仅 current；`resolved`=仅 baseline；`changed_severity`=两边同 id 异 severity（输出 `{id, from_severity, to_severity, message}`）。
7. **输出** `RegressionDiff{baseline_meta, added, resolved, changed_severity, inspector_upgraded, dst_boundary_crossed=False, diff_skipped_reason}`。**字段名用 §10 的 `baseline_meta`**（不叫 `baseline_ref`，避免与 `ReportMeta.baseline_ref` 撞名）。`baseline_meta` **当且仅当 `baseline.meta is not None` 时非 None**（与 `current.meta` 无关，含各跳过子情形）；唯 `baseline.meta is None` 时为 None（`current.meta is None` 但 `baseline.meta` 在时仍照常投影 baseline_meta）。`baseline_unavailable` **不入** `diff_skipped_reason`——无基线由 CLI 输出文本、不构造 RegressionDiff。`dst_boundary_crossed` M3 恒 False。

`reports diff <a> <b>`：a=baseline、b=current。`reports diff --target <t>`（自动基线）：current=该 target 最新 run，baseline=`latest_ok_baseline(t, before_run_id=current.run_id)`（**排除 current 自身**，否则单条 ok run 会被选成自己的基线 → 假「无回归」）；`latest_ok_baseline` 返回 `BaselineRef`（无 findings），须再 `get_run(baseline_ref.run_id)` 还原 `Report` 才喂 `compute_diff`。

### 决策 8：迁移关键点 —— `_redact.py` 与工厂字段穿线

- `_redact.py` 的 `_redact_finding` 重构调用必须加 `id`/`inspector_name`/`inspector_version`（**原样透传，不脱敏**——id 是 hash、inspector name/version 非敏感）。`redact_report_for_render` 重构 `Report` 时必须透传 `meta`（递归脱敏 meta 内字符串：`target_name`/`intent` 等）与 `hypotheses`（脱敏 `description`/`suggested_actions`）。**漏改即新字段在渲染/落盘静默消失**。
- `from_inspector_results` 升级：flatten 时为每个 finding 计算 `id` 并填 `inspector_name`/`inspector_version`（用 `model_copy(update=...)`，因 Finding frozen）；组装 `meta`（`inspectors_used` 投影、`status` 派生、`token_usage`/`duration_seconds`/`timestamp`/`run_id`/`target_*` 填充）；写 `schema_version="1.1"`。新增可选参数 `target_id` / `target_type` / `token_usage` / `status` / `intent`（已有）/ `schedule_name`，缺省走决策 3/4/5。

### 决策 9：CLI 接线 + 持久化入口为机械 `inspect` 路径

新增 `cli/reports.py`（Typer sub-app），`cli/__init__.py` `add_typer(name="reports")`。`reports list/show/diff` 遵循既有退出码契约（0 成功 / 3 not-found / 非交互错误单行 stderr 无 traceback）。

**`--persist` 接 `cli/inspect.py` 的 `--inspector` 机械路径**（而非 `demo run`）——因为只有该路径产出 `Report`（经 `from_inspector_results`）。`hostlens inspect --intent` 与 `demo run` 走 Agent 路径、产物是 `PlannerResult`（`planner.py`：无 `inspector_results`，docstring 明示「装配 Report 会 fabrication，是 M3 Diagnostician 的事」），**无法 `from_inspector_results`**，故本提案不给它们加 `--persist`（非目标 8）。Demo Path 因此改走 `inspect local-host --inspector hello.echo --persist`（确定性、离线、无 LLM）；丰富 diff 由 `tests/incidents/test_diff_replay.py` 用 incident inspector 机械 runner 经 `from_inspector_results` 组装两份 `Report` 验证。写本地 store 非远端写操作，不需 `--yes`。

## 风险 / 权衡

- [字段冗余：扁平 vs meta 双份 target_name/timestamp] → 约定 meta 权威、扁平兼容；docstring + spec 明确，避免 consumer 读错源。
- [message 易变 → diff 噪音] → 本提案 fingerprint=整条 message（已知限制）；未来由 inspector 声明指纹关键字段精化。demo（hello.echo 确定性）与集成 diff（ReplayTarget 确定性）不受影响。
- [demo run 路径产物是 PlannerResult、无 Report → 不能直接持久化] → 持久化改接产出 Report 的机械 `inspect --inspector` 路径；Agent 路径持久化（PlannerResult→Report 装配）显式推到 add-diagnostician-agent（非目标 8）。
- [`_redact.py` 漏透传新字段] → 在 tasks 里列为显式步骤 + 加「meta/hypotheses 经渲染后非空」断言测试守门。
- [`target_type` 放宽为 str 偏离 §10 Literal] → 选 forward-compat 优先；canonical 值写 docstring，未来要收紧可再 MODIFY。
- [render_json `exclude_none=False` → 新字段全量进 JSON] → 所有 JSON 断言/snapshot 统一重录（tasks 列清单）；非破坏但输出变化。
- [SQLite 并发] → M3 单进程 CLI，无并发写；WAL + 单库够用。多进程并发留 M4 Scheduler 评估（记 Open Question）。

## 迁移计划

1. 扩 `models.py`（+6 模型 +Finding 3 字段 +Report 2 字段 +schema_version Literal +工厂升级）。
2. 同步 `_redact.py`（透传新字段 + 脱敏 meta/hypotheses 字符串）。
3. `render_markdown` 加根因章节占位（空 → `_暂无根因假设_`）。
4. 新增 `store.py` / `diff.py` / `cli/reports.py` + `cli/__init__.py` 注册 + `cli/inspect.py --persist`（仅 `--inspector` 路径）。
5. **更新经 render 的 sink**：`inspect` 路径 `.ambr`（schema_version 1.0→1.1 + meta + 根因章节 + 新字段）+ 直接断言 `Report`/`Finding`/`schema_version` 的 reporting/cli 单测。**incidents/demo snapshot 不动**（`project_planner_result` 只投影 severity+message+tags，不走 render）。新增 `tests/incidents/test_diff_replay.py`。snapshot 文件 `.rstrip("\n")` 容忍 EOF-fixer 尾换行（首次 commit 注意 re-add 重跑）。
6. **回滚**：纯 add-only + 新增模块；回滚 = revert PR，旧 JSON 仍可被 revert 后代码加载（schema 1.0 路径未动）。

## Open Questions

1. 稳定 `target_id`（独立于 `target_name`）是否在 M4 引入？M3 暂 `target_id = target_name`。
2. fingerprint 的「关键字段」精化（inspector 声明指纹字段以抗 message 易变）——是否值得在 M6 Inspector 扩充时一并设计。
3. SQLite 多进程并发写策略（M4 daemon 多 schedule 并发）——M3 不解决。
4. 同 run 内同 (inspector,version,message) 异 severity 的 id 碰撞——是否需要在 id 里加序号消歧；当前记为接受风险。
