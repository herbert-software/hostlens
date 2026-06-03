# wire-demo-to-report

## Why

`hostlens demo run` 当前只跑 **Planner Agent 单段**（`_run_scenario` → `PlannerResult` → `render_planner_result` → `_compute_intent_exit_code`），产出的是「一堆 finding + narrative」，**没有根因假设、不是忠实 Report、不能 `--persist`**。但 M3 的差异化卖点正是「带根因假设的报告」（TODO §M3 行 193：「5 分钟内 reproduce 出一份带根因假设的报告，无需 SSH / 无需付费 API」），而 demo 是面向人类（简历/面试官）的**唯一离线 5 分钟入口**。

M3.1 已交付的 `--intent` 路径（`add-intent-report-persistence`）现产忠实 `Report`（Planner→Diagnostician→`InspectorResultCollector` 快照组装 + hypotheses/narrative 投影）并解锁 `--intent --persist` + `reports show/diff`。但它依赖真实 `ANTHROPIC_API_KEY`，demo 没有同等的离线体验。本变更把 demo 接到同一条 Report 管线，让离线 demo 也能展示「根因假设 + 持久化 + regression diff」全闭环——这才是项目卖点的可复现证据。

## What Changes

- **`demo run` 升级为全链管线**：`hostlens demo run <scenario>` 从 Planner-only 改为跑 **Planner → Diagnostician → 组装忠实 `Report`**（复用 `add-intent-report-persistence` 的 `InspectorResultCollector` 两时点装配 + id 一致性 + status 合并），保持完全离线（`ReplayTarget` + `PlaybackBackend`）。
- **给 8 套打包场景补 Diagnostician 回放资产**：每个 `src/hostlens/demo/scenarios/<key>/` 现有 Planner-only `cassette.jsonl`。本变更为每个场景补充 **Diagnostician 阶段的 playback record**（`correlate_findings` 产根因假设；不调 `request_more_inspection`，见 D-3），使诊断段也能离线确定性回放。录制沿用现有 authored-response 机制全链重录（`RecordingBackend` 整文件覆写）：authored Planner 响应不变 + 冻结时钟 → **重录的 Planner record 与现有逐条 byte-identical**，故 **Planner 段行为零回归**（记录内容不变，见 D-3.5）。
- **`demo run` 渲染改为 intent 风格 Report**：从 `render_planner_result` 改为复用 `render_intent_report`（narrative + `## Findings` + `## 根因假设` + 遥测），md/json 均输出 `Report`。**BREAKING**：`demo run --format json` 现输出 `Report`（非 `PlannerResult`）、md 版面改为 Report 风格（含根因假设章节）。
- **`demo run` 退出码改由 Report 映射**：从 `_compute_intent_exit_code(PlannerResult)` 改为 `_compute_intent_report_exit_code(Report)`（含 `partial`→2），保持 0/1/2/3 四值契约。
- **新增 `demo run --persist`**：把组装出的 `Report` 落盘到标准 `ReportStore`，使 `reports show / diff` 能离线消费 demo 产出（项目卖点闭环的离线复现）。
- **编排去重（设计决策，design.md 定）**：`--intent` 的 `run_intent_diagnosis` 内部调 `create_backend(settings)`；demo 必须用 cassette 的 `PlaybackBackend`，不能调 `create_backend`。两条路径要么共享一个「注入 backend + context_factory + tool_clock」的内部装配函数（DRY，单一 SOT，推荐），要么 demo 自写镜像逻辑（有漂移风险）。本提案倾向前者——把 `run_intent_diagnosis` 的「collector 装配 + 两 loop + 组装」抽成 backend/context_factory/tool_clock 可注入的核心（`run_diagnosis_pipeline`，签名见 design D-1），`--intent` 与 demo 各自提供 backend + context_factory + clock。

## Capabilities

### 修改功能

- **`demo-cli-command`**：`demo run` 的「离线回放并渲染报告」需求（现：跑完整 Planner 管线渲染报告 → 改：跑 Planner→Diagnostician→Report 全链、渲染 intent 风格 Report）、「复用 4 值退出码契约」需求（现：复用 `_compute_intent_exit_code` → 改：复用 `_compute_intent_report_exit_code`，含 partial）、「不触达 API 的结构性保证」需求（扩展到诊断段也必须是 `PlaybackBackend`）；**新增** `demo run --persist` 子需求（落盘 Report、no-result 不落盘、自包含语义说明）。

（**无新建 capability**：Report 组装逻辑由已归档 `agent-report-assembly` capability 提供，本变更只是让 demo 复用它——该 capability 已 backend-agnostic，无需改 spec；hypothesis-level diff 是**独立后续提案**，见 Non-Goals。）

## Non-Goals

- **hypothesis-level diff**：`reports diff` 现仅比 finding 级（added/resolved/changed_severity），hypotheses 入库但不参与对比。给 `RootCauseHypothesis` 设计匹配键 + 扩展 `RegressionDiff` 是**独立后续提案**（用户已确认拆分），本变更不碰 `reporting/diff.py`。
- **不改 8 套场景的 Planner record 内容 / fixture**：确定性重录使 Planner record byte-identical，Planner 段行为零回归（不改 fixture）。
- **不新增 demo 场景**：沿用现有 8 套 incident。
- **不引入真实 API / SSH**：demo 永远离线（cassette replay）。
- **不改 `--intent` 的对外行为**：若做编排去重，是内部重构（行为等价），`--intent` 的 CLI 契约不变。
- **不改 `reporting/store.py` schema / `reports` CLI**：demo --persist 复用现有 `ReportStore.save`。

## Impact

**受影响代码**：
- `src/hostlens/cli/demo.py`：`run_cmd`（渲染/退出码/新增 --persist）、`_run_scenario`（改调 `run_diagnosis_pipeline`，单 ExitStack 包两 loop）
- `src/hostlens/demo/assembly.py`：`build_demo_planner` 在此（**非** cli/demo.py），需扩展——`register_default_tools` 补 `collector=`（现仅 `clock=`）、暴露单个 `PlaybackBackend` + `context_factory` 给 caller、调整返回契约（现返回 `(PlannerAgent, ReplayTarget)`）
- `src/hostlens/cli/_intent.py`：把 `run_intent_diagnosis` 核心抽成 `run_diagnosis_pipeline`（backend/context_factory/tool_clock + report/lookup 双 target 名 可注入），`run_intent_diagnosis` 退薄包装（编排去重决策，design D-1）
- `src/hostlens/demo/scenarios/<key>/`：8 套场景的 `cassette.jsonl` 各含 Diagnostician playback record（单 cassette，D-2）；由扩展后的 authored-response 录制（`build_authored_responses` 补诊断 turn）全链重录产出（D-3.5，零真实 key，Planner record byte-identical）
- `tests/incidents/_harness.py` / `_generate.py`：扩展 `build_authored_responses` 补诊断段 authored turn（≥4 有序响应）+ 重写 `_record_cassette_and_snapshot` 改用 `run_diagnosis_pipeline` 跑全链录制（D-3.5）；incident Planner-only snapshot（`snapshots/<key>.md`）与 8 个 `tests/incidents/test_*.py` 保持不变（第二消费者零回归，PlaybackBackend 不强制全量消费）
- `tests/demo/test_demo_cli.py` / `tests/demo/test_demo_replay.py` / `tests/incidents/`：demo 全链 + --persist + 退出码 + 离线结构性保证测试；既有 PlannerResult-风格断言（如 `test_demo_run_md_and_json_same_exit_code` 断言 json `loop_result.terminal_status`）迁到 Report 契约

**对外契约影响（CLI）**：
- `hostlens demo run` **输出 BREAKING**：md 改 intent 风格 Report（含根因假设）、json 改 `Report`（非 `PlannerResult`，可 `Report.model_validate_json` 往返）。demo 是演示命令，无下游程序依赖其输出契约，BREAKING 可接受但需在 spec 标注。
- `hostlens demo run` **新增 `--persist` flag**。
- 退出码语义不变（仍 0/1/2/3），但来源从 PlannerResult 改 Report（行为差异：Report 可推出 `partial`，PlannerResult 不能）。
- 不改 Inspector / Agent tool / MCP / Notifier / Schedule 任何 schema。

## Failure Modes

1. **Diagnostician cassette 漂移**：补的诊断段 record 与实际 Diagnostician loop 请求键不匹配 → 运行期 `CassetteMiss` → 单行 `internal:` + exit 2（复用现有 demo 边界，与 Planner 段 miss 同处理）。
2. **某场景 Diagnostician 产零假设**：诊断段 cassette 只 finalize 不调 `correlate_findings` → Report 的 `## 根因假设` 渲染 `_暂无根因假设_` 占位（合法、不报错）。但 demo 卖点是「带根因假设」，故 8 套场景的诊断 cassette **应**至少产 1 条假设（验收守护）。
3. **collector 真空**：理论上 demo 的 Planner cassette 必跑 inspector（场景设计如此），collector 不会空；若某场景 Planner 段被改成不调 inspector → no-result（stderr 降级 + exit 2 + 不 persist），与 `--intent` 同语义。
4. **`--persist` 写盘失败（两分支均升 exit2）**：(a) `ReportStore.save` **抛异常** → 单行 `internal:` + exit 2；(b) 主 store 不可写但 `_persist_report` **降级写 orphan 文件**（返 `True` 不抛）→ 单行 `warning:`（非 `internal:`）+ caller `(orphaned or persist_failed) and exit∈(0,1)→2` 升 exit 2，报告仍渲染。复用 `--intent` 的 orphan/persist-fail 升级逻辑。
5. **诊断段 `request_more_inspection` 补查的 inspector 在 demo 资产里没有对应 record**：补查请求键 miss → CassetteMiss → exit 2。故 demo 诊断 cassette 若含补查，必须连补查 inspector 的 record 一起录。（注：D-3 决定 demo 诊断段不调补查，本条为防御性说明。）
6. **`--persist` 误污染真实报告库 / CI 无隔离写盘**：`_persist_report` 用 `ReportStore()` 无路径注入、解析 `$XDG_DATA_HOME/hostlens/reports.db`（真实用户库）；demo 是面向人类的入口，用户照教程跑 `--persist` 会把演示数据写进真实库（靠 `demo:` 前缀可区分/可删，但仍落真实库）。**测试缓解**：所有 `--persist` 测试必须 `monkeypatch.setenv("XDG_DATA_HOME", tmp_path)` 隔离并断言不写真实库（tasks 4.2）；`--persist --help` 文案须警示"写入真实报告库"。

## Operational Limits

- **并发**：与 `--intent` 同——Diagnostician loop 经 `asyncio.gather` 并行分发 tool_use；demo 单场景、两 loop 串行，无额外并发预算。
- **内存**：单场景 collector 持 N 个 InspectorResult（N≈场景 inspector 数，个位数），可忽略。
- **超时**：cassette 回放无网络，毫秒级；沿用 demo 现有无显式超时（离线确定性）。
- **资产体积**：每场景 cassette 新增**恰好 2 条**诊断 record（D-3 只用 `correlate_findings`：correlate tool_use 一条 + 收 tool_result 后 finalize 一条；loop 合约要求每 tool_use 后必有后续调用，故非 1；无第二 tool_use 故非 3），wheel package-data 增量小。

## Security & Secrets

- **不引入新密钥**：demo 全程 `PlaybackBackend`，无 API key、无 target 凭据（结构性保证扩展到诊断段：装配后 backend 必为 `PlaybackBackend`）。
- **脱敏**：demo Report 经 `render_intent_report` → `redact_report_for_render`（含 metadata narrative）；打包场景 fixture 无真实 secret，脱敏路径与 `--intent` 同。
- **`--persist` 与自包含语义**：demo 「不读取用户配置」需求是约束**读**（targets.yaml / API key），写 Report 到标准 `ReportStore` 是 `--persist` 的显式动作、不违反读自包含。
- **显式接受的代价：`--persist` 写用户真实报告库**（`$XDG_DATA_HOME/hostlens/reports.db`）。这是 D-5「落标准 store 以复现 `reports show/diff` 闭环」的**有意取舍**（隔离 store 已否决——闭环失效）：真人用户照 Demo Path 跑 `--persist` 会把演示数据写进真实库。缓解是**三件套**（不只测试隔离）：(1) 默认不 persist（需显式 flag）；(2) `target_name` 打 `demo:<scenario>` 前缀、可 `reports list demo:<scenario>` 定位并删除；(3) `--persist --help` 文案显式写「演示数据将写入真实报告库，可用 `reports ... demo:<scenario>` 定位删除」。测试侧另须隔离 `XDG_DATA_HOME`（Failure Mode 6）。本提案**显式承认**真实库写入是 `--persist` 的预期行为，不假装无副作用。**不扩大攻击面**。

## Cost / Quota Impact

- **零 API 成本 / 零配额消耗（运行期 + 录制期均离线）**：demo `run` 全程 cassette 回放，不发真实 Anthropic 请求。补 8 套诊断 cassette 也**不需真实 key**——沿用现有 authored-response 录制机制（`RecordingBackend` 包 `FakeBackend(authored)`，捕获真实 request、响应是手写脚本，零真实 Anthropic 调用，D-3.5）。
- **token 遥测是回放快照值、非本次计费**：demo md 渲染的遥测行 `tokens_in/out` 来自 `_sum_loop_usage`（Planner+Diagnostician 两 loop 相加）回放 cassette 录的 usage——为避免被读成「本次真实花费」误导「离线零成本」叙事，须在 **demo `run --help` 文案 / demo 文档**说明"遥测 token 为回放快照值、非真实计费"。**不改**共享的 `render_intent_report`（它同时服务 `--intent`，不能为 demo 注入 `(replayed)` 标记）——澄清落在 demo 命令帮助/文档层，渲染器本身不动。

## Demo Path

```bash
# 1. 离线产出带根因假设的忠实报告（无 SSH / 无 API key）
hostlens demo run cpu_saturation
#   → stdout: narrative + ## Findings + ## 根因假设（含证据链接）+ 遥测

# 2. 落盘 + 取回（离线复现 M3.1 持久化闭环）
hostlens demo run cpu_saturation --persist
hostlens reports list demo:cpu_saturation   # 从这里取 run_id（`demo run` 的默认 md stdout 本身不回显 run_id，沿用 --intent 行为；reports list 的行含 run_id）
hostlens reports show <run_id>        # 取回含 hypotheses 的 Report

# 3. 两次 run + diff（离线复现 diff 管线；同场景确定性回放 → 空 delta）
hostlens demo run cpu_saturation --persist
hostlens reports diff <a> <b>         # diff 跑通；两次相同 → finding 级 delta 为空（非 added/resolved）
```

验收：干净 macOS / Linux 上 `pip install -e ".[dev]" && hostlens demo run cpu_saturation` 秒级出**带根因假设**的报告；`--persist` 后 `reports show/diff` 可消费。
