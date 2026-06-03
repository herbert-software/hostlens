# wire-demo-to-report — Design

## Context

`add-intent-report-persistence`（已归档）给 `--intent` 建了完整的 Report 管线：
`run_intent_diagnosis(settings, target, intent, ...) -> Report | None` 内部
`create_backend(settings)` → 建 `InspectorResultCollector` → Planner loop（`register_default_tools(collector=)`）→ 从 Planner-phase snapshot seed `FindingStore` → Diagnostician loop（`register_diagnostician_tools(collector=)`）→ 诊断后全量 snapshot 经 `Report.from_inspector_results` 组装 + 投影 hypotheses/narrative + id 不变量校验。

`demo run`（`add-demo-cli` 归档）是离线版：`_run_scenario` → `build_demo_planner(scenario_key, exit_stack)` 用 `ReplayTarget`（读 `fixture.json` 的命令录制）+ `PlaybackBackend`（读 `cassette.jsonl` 的 LLM 录制）跑 **Planner-only**，返回 `PlannerResult`，`render_planner_result` 渲染、`_compute_intent_exit_code` 退出码。8 套场景资产在 `src/hostlens/demo/scenarios/<key>/{fixture.json, cassette.jsonl}`。

约束：
- demo 必须保持**完全离线**（`PlaybackBackend` + `ReplayTarget`，不发真实 API、不读用户配置），且有「装配后 backend 必为 `PlaybackBackend`」的**结构性**断言。
- demo 必须保持自包含：不读 `~/.config/hostlens/*`、不读 `ANTHROPIC_API_KEY`。
- 不能让 demo 与 `--intent` 的 Report 装配逻辑漂移（id 一致性 / status 合并 / 两时点时序是 M3.1 的核心契约，重复实现必然走形）。

## Goals / Non-Goals

**Goals**：
- `demo run` 跑全链 Planner→Diagnostician→忠实 `Report`，渲染 intent 风格（含根因假设），保持离线。
- 复用（而非复制）`--intent` 的 Report 装配核心。
- 给 8 套场景补离线 Diagnostician 回放资产，每套至少产 1 条根因假设。
- `demo run --persist` 落盘，离线复现 `reports show/diff` 闭环。

**Non-Goals**：
- hypothesis-level diff（独立后续提案）。
- 改 Planner record 内容 / fixture（确定性重录使 Planner record byte-identical，fixture 不动）。
- 新场景 / 真实 API / SSH。
- 改 `--intent` 对外行为（编排去重是行为等价的内部重构）。

## Decisions

### D-1：抽出 backend/registry 可注入的装配核心，`--intent` 与 demo 共享

把 `run_intent_diagnosis` 内「collector 装配 + 两 loop + seed + 组装」抽成一个**注入 backend、context_factory、tool_clock** 的核心函数（命名暂定 `run_diagnosis_pipeline`）：

```
async def run_diagnosis_pipeline(
    backend: LLMBackend,
    settings: Settings,
    context_factory: Callable[[], ToolContext],   # 无 ContextFactory 别名；现实类型即此
    report_target_name: str,       # 写进 Report.target_name 的标识名
    target_lookup_name: str,       # registry 查找键（diagnostician 默认 target）
    target_type: str,
    intent: str,
    *,
    tool_clock: Callable[[], datetime] | None = None,
    observer: LoopObserver | None = None,
    planner_result_sink: Callable[[PlannerResult], None] | None = None,
) -> Report | None
```

**`planner_result_sink`（录制 harness 投快照的挂载点，修 D-3.5 step3 的契约缺口）**：pipeline 在 `planner.run()` 返回后、Planner-result 还在手时调一次 `planner_result_sink(planner_result)`（若非 None）。`--intent` 薄包装传 `None`（**行为与现状逐字等价**——sink=None 时这一步是 no-op）；demo 录制 harness 传一个捕获 `PlannerResult` 的 sink，用它投 incident Planner-only 快照（D-3.5 step3 的单遍跑路线靠此挂载，不"额外跑一遍"）。这是一个 optional kwarg（`observer` 先例），不改 `--intent` 对外行为。

它内部做：建 collector → `register_default_tools(planner_registry, collector=collector, clock=tool_clock)` → `PlannerAgent(backend, ...)` → 计时/seed/诊断/snapshot/组装（与现 `run_intent_diagnosis` 主体逐字一致，含 `failed_api_unavailable` 早返、no-result 判据、status 合并、id 不变量）。诊断段 `register_diagnostician_tools(target_name=target_lookup_name)`；组装 `Report.from_inspector_results(report_target_name, ...)`。

**两个 target 名分参（修单参双语义坑）**：现 `run_intent_diagnosis` 用单个 `target` 字符串同时喂 `register_diagnostician_tools(target_name=)`（registry 查找键，`request_more_inspection` 内 `ctx.target_registry.get()` 用）与 `Report.from_inspector_results(target_name=)`（Report 标签）。`--intent` 下两者本就同名故不矛盾；但 demo 要 Report 标签 `demo:<scenario>`（去污染标记，D-5）而 registry 里 ReplayTarget 注册键是 `DEMO_TARGET_NAME="incident-host"`（assembly.py），两者**必须不同名**。故核心拆 `report_target_name` 与 `target_lookup_name` 两参，避免把「显示名」与「查找键」耦合成一个参数。**不变量**：即使 D-3 让 demo 不调 `request_more_inspection`（`target_lookup_name` 当下不被查），`target_lookup_name` 仍必须等于 `DEMO_TARGET_NAME` —— 这样未来某场景补 `request_more_inspection` cassette 时查找不会 miss。已核 `Report.target_name` 是 `Field(min_length=1)` 无 regex 约束，冒号 `demo:<scenario>` 合法（ExecutionTarget 名的 `^[a-z][a-z0-9_\-]{0,63}$` 正则只约束 registry 注册名，不约束 Report 标签）。

**tool_clock 注入（修 D-1 签名漏 clock）**：demo 的 Planner cassette 是在**冻结时钟** `FROZEN_DT`（`demo/assembly.py` 的 `_frozen_clock`）下录的——`sampling_window` inspector 的命令文本随时钟变化，冻结时钟是这些命令 byte-stable、cassette request key 不 miss 的**前提**（assembly.py 注释明示「replay miss otherwise」）。核心内部既然自建 Planner registry 调 `register_default_tools`，就必须把 `tool_clock` 透传进去：`--intent` 薄包装传 `None`（生产用 wall clock），demo 薄包装传 `_frozen_clock`。漏掉此参 demo 的 Planner 段必 CassetteMiss。

- `run_intent_diagnosis` 退化为**薄包装**：`create_backend(settings)` + 用真实 `target_registry`/`inspector_registry` 建 `context_factory` + `report_target_name=target_lookup_name=target` + `tool_clock=None` + 调核心。**行为零变化**（现有 `--intent` 测试是回归守护）。
- demo 薄包装：见下「demo 侧装配」。

**demo 侧装配（D-1 demo 分支具体化）**：扩展 `demo/assembly.py`——
  1. `build_demo_planner` 现返回 `(PlannerAgent, ReplayTarget)` 已不够；改/增装配函数暴露 **`(backend: PlaybackBackend, context_factory, replay_target, settings)`**（4 元组）：`context_factory` 复用 assembly 现有闭包（`target_registry` 含注册为 `incident-host` 的 `ReplayTarget` + 内置 inspector registry + `_DemoSettings` 屏蔽 `HOSTLENS_*`）；`backend` 为**单个** `PlaybackBackend` 实例（D-2，喂两 loop）；**`settings` 必须是装配内部建的那个 `_DemoSettings` 实例本身**，caller 原样回传给 pipeline——避免 caller 另建 `_DemoSettings` 致 `settings.agent.primary_model`（request key 的 `model` 来源）与录制期漂移。`replay_target.type` 是构造期常量（`impersonate` 字面值，不依赖 reader 生命周期），可在 ExitStack 区外安全读。
  2. demo caller（`cli/demo.py`）在**单一 ExitStack** 作用域内：拿 `(backend, context_factory, replay_target, settings)` → 调 `run_diagnosis_pipeline(backend, settings, context_factory, report_target_name=f"demo:{scenario}", target_lookup_name="incident-host", target_type=replay_target.type, intent, tool_clock=_frozen_clock, observer=observer)` → 跑完断言 `replay_target.misses == []`。注：clock 经 `tool_clock` 入参穿透（见上）。
  3. `register_default_tools` 在核心内被调时同时传 `collector=` 与 `clock=tool_clock`（现 demo assembly 只传 `clock=`，需补 `collector=`）。

**核心不吞 `CassetteMiss`**：`run_diagnosis_pipeline` 被 `--intent` 与 demo 共用，**不得**在内部 catch `CassetteMiss`（或把它误判为 `failed_api_unavailable` / no-result）——诊断段漂移的 `CassetteMiss` 必须原样冒泡到 caller 边界，由 demo caller 包装成单行 `internal: CassetteMiss: ...` → exit 2（与 spec「运行期 cassette miss」场景一致）。核心的 no-result 早返仅判 `failed_api_unavailable` 与 collector 空两种，不含 `CassetteMiss`。

**为什么不让 demo 自写镜像逻辑**：两时点时序 / id 一致性 / status 合并是易错的核心契约；复制 = 双份维护 + 漂移风险（§7 反模式）。注入式核心让两条路径共享 SOT，差异收敛到「backend + context_factory + tool_clock + 两 target 名」几个注入点。

**替代方案（否决）**：(a) demo 复制 run_intent_diagnosis 主体——漂移风险；(b) 给 run_intent_diagnosis 加 `backend=None` 参数（None 时 create_backend）——把 demo 关注点漏进 `--intent` 签名、语义含糊。抽核心 + 双薄包装最干净。

### D-2：Diagnostician 回放记录追加进现有 `cassette.jsonl`（单 backend 单 cassette）

`run_diagnosis_pipeline` 给 Planner 与 Diagnostician **同一个 backend 实例**。demo 的 `PlaybackBackend` 读单个 `cassette.jsonl`，按 **request key** 匹配 record——key 算法在 `src/hostlens/agent/cassette_key.py::request_key_for_payload`（replay 端 `agent/backends/playback.py` 调它，record 端规范化在 `tests/support/cassette_recording.py`，两端同一函数）：对 `{model, messages, tools_count}` 做 `sort_keys` 规范化指纹，**含完整 `messages` 数组**、`system` 不入 key；record 里可能另带 `tools_schema_hash` 等字段但不参与 key。Diagnostician 阶段的 `messages_create` 请求与 Planner 阶段 messages 不同（诊断 system prompt + seeded findings + 不同 tools），request key 天然不同、不与 Planner record 冲突。故：

- **每个场景的单份 `cassette.jsonl` 同时含 Planner 与 Diagnostician 阶段 record**（不新建第二份 cassette；录制经 D-3.5 全链重录，Planner record 内容不变）。单 backend 从一份 cassette 同时服务两段，匹配靠 key 不靠顺序。验收须断言：诊断段 record 的 request key 与该场景**所有** Planner record 的 key 均不相等。**撞键结构性不可能（但仍硬断言兜底）**：已核 Planner 与 Diagnostician **tools_count 均为 3**（Planner=run_inspector/list_inspectors/list_targets；Diagnostician=correlate_findings/request_more_inspection/list_inspectors），故 `tools_count` 维度**不区分**两段；区分**全靠 messages 数组内容不同**——Planner 首条 user message 是 intent，Diagnostician 首条是 findings-block（`_render_findings_block`），内容必异 → key 必异。`system` 不入 key 不影响（两段靠 messages 已分）。
- **替代方案（否决）**：每场景加 `diagnostician_cassette.jsonl` + demo 建两个 PlaybackBackend——但核心函数只接一个 backend，要么改核心签名（污染 `--intent`）要么 demo 绕过核心（违背 D-1）。单 cassette 最贴合 D-1。

### D-3：demo Diagnostician cassette 只用 `correlate_findings`，不用 `request_more_inspection`

诊断段录制让模型基于 Planner findings 调 `correlate_findings` 产 1+ 根因假设后 finalize，**不调 `request_more_inspection`**。

- **理由**：`request_more_inspection` 会触发 inspector → `target.exec` → 需要 `ReplayTarget` 的 `fixture.json` 补对应命令录制（扩大 fixture、易漂移）。只用 `correlate_findings` 则诊断段**零新增 target 命令**，fixture 完全不动，`replay_target.misses == []` 守卫仍成立。
- 仍满足卖点：根因假设来自对已采集 findings 的关联，正是 Diagnostician 的核心职责。
- **取舍**：牺牲「demo 展示补查」这一次要能力，换 fixture 零改动 + 强离线确定性。补查的离线演示留作未来（需要时再给特定场景补 fixture 命令）。

### D-3.5：Diagnostician record 由现有「authored-response 离线录制」机制扩展产出，禁手编 request record（硬约束）

诊断段 cassette record **必须**沿用 8 套 Planner cassette 现成的**离线 authored-response 录制机制**（`tests/incidents/_generate.py` + `_harness.build_authored_responses`）扩展产出，**零真实 API key、零真实 Anthropic 调用、完全离线**。**禁止手工编 request record**（request key 必须由活体 pipeline 经 `RecordingBackend` 自动捕获）。

- **已核的现实机制（纠正第 2 轮设计对录制方式的误述）**：现有录制是 `RecordingBackend(cassette_path, inner=FakeBackend(responses=build_authored_responses(scenario)))`（`_generate.py:164-166`，注释明写 "wrapping a scripted FakeBackend — **zero API key**"）。即：`RecordingBackend` 包的是 **`FakeBackend`（手写 `MessageResponse` 脚本）**，**不是** live `AnthropicAPIBackend`。`RecordingBackend` 的职责是**捕获活体 PlannerAgent/DiagnosticianAgent 组装的真实 request**（含 request key），**model 响应由 authored 脚本提供**。所以「禁手编」针对的是 **request record**（自动捕获），不是 response（response 本就是 authored 的 SOT）。**录制不需要真实 key**。

- **理由（为何升级为硬决策而非"实现阶段定"）**：request key 对**完整 `messages` 数组**取指纹（D-2），诊断段 `messages` 含 **seeded findings**（`id = compute_finding_id(name, version, message)`，文本/顺序/序列化全参与 key）。手编 request record 必须 byte-for-byte 复刻活体 `DiagnosticianAgent` 组装的 messages，任何漂移 → 运行期 `CassetteMiss`。让活体 pipeline 经 `RecordingBackend` 自动捕获 request 是唯一稳健路径。

- **录制入口改造范围（不止"扩展 `build_authored_responses`"——三处都要动）**：
  1. **扩展 `build_authored_responses(scenario)`**：现仅返回 2 个 Planner turn（`_harness.py:142-179`）。补诊断段 turn 后，happy-path 须返回**≥4 个有序 `MessageResponse`**——loop 合约要求每个 `tool_use` 响应后必有一次后续 backend 调用返回 `end_turn`：`planner_turns=[planner_tool_use(stop_reason=tool_use), planner_end_turn(stop_reason=end_turn)]` + `diagnostician_turns=[diag_correlate_tool_use(stop_reason=tool_use), diag_end_turn(stop_reason=end_turn)]`。建议暴露具名 helper 拆两段、标注各自 `stop_reason`。**诊断段 `correlate_findings.supporting_findings` 引用的是 ordinal label（`["F1","F3"]`，已核 `diagnostician_tools.py:108`，**不是** finding id）**。authored 引用哪条 F#，取决于 D-7 的稳定 seeding 顺序——**有 D-7 排序，F1/F2 跨 run 确定，authored `[F1]` 引用稳定且指向固定 finding**；无 D-7 则 label 错位。authored label 须对照该场景**排序后**的 seeded findings 列表写（录制脚本打印 seeded 列表辅助）。
     - **写错 label 的失败路径（纠正）**：handler 对未知 label 抛 `ToolError`（`diagnostician_tools.py:90`）→ adapter 转成 `is_error` tool_result 信封反馈模型让 loop **自纠错重试**，**不是 exit 2**。但录制期用 `FakeBackend`（无自纠错、只吐下一条脚本响应）：authored label 悬空 → 该 correlate 被 bounce → `harvest_hypotheses` 只迭代**成功**的 correlate 调用、跳过它 → 该 hypothesis 静默丢失 → 录成 **0 假设** cassette（Failure Mode 2 的 `_暂无根因假设_`）。故「authored label 写错」**不在录制期 loud-fail**，只由 tasks 3.1 的「≥1 hypothesis liveness 守护」事后拦截。（`_assemble_report` 的 id-consistency invariant 只覆盖**已 harvest** 的 hypothesis 的 id，dangling label 在 harvest 前就被丢、不走该 invariant。）
  2. **重写 `_generate.py::_record_cassette_and_snapshot`**：现用 `build_incident_planner(recorder, ...)` 跑 **Planner-only** `planner.run()`（`_harness.py:73`）。改造须改用 `run_diagnosis_pipeline(backend=recorder, context_factory=<incident context_factory>, tool_clock=_frozen_clock, ...)` 跑全链，使 `RecordingBackend` 捕获两段 request、scenario 末**整文件 `os.replace` 覆写** cassette（`never append`，`cassette_recording.py:228`）；`recorder.flush(persist=True)` 仍在 `run_diagnosis_pipeline` 返回**之后**调（早 flush 会丢诊断 record）。**不需要** composite/phase-switching backend（代码里不存在）——单 `FakeBackend` 供两段响应、单 `RecordingBackend` 捕两段 request 即可。
  3. **snapshot 投影：用 Planner-result sink（单遍跑），不"额外跑一遍"**：`run_diagnosis_pipeline` 返回 `Report`，但 incident 快照 `snapshots/<key>.md` 与 8 个 `tests/incidents/test_*.py`（`assert_incident_snapshot`，Planner-only）**保持不变**（不迁 Report 投影，否则破坏那 8 个测试）。`_generate.py:173` 的 `assert result.findings` 与 `:178` 的 `project_planner_result(result)` 现吃 `PlannerResult`——改造后须改为消费 `run_diagnosis_pipeline` 暴露的 **Planner-phase 结果 sink**（**不是** pipeline 返回的 `Report`，`Report` 无 `PlannerResult` 同 API），**单遍跑、单 RecordingBackend、零冲突**。
     - **「额外跑一遍 Planner-only 投快照」否决**：(a) `build_authored_responses` 是有限脚本，全链跑已消费 Planner+诊断全部响应，第二遍 Planner-only 需 Planner 那 2 条响应再来一份 → `FakeBackend` 脚本耗尽吐不出；(b) 第二遍新建 `RecordingBackend` 指向同一 `cassette_path` 撞 `_ACTIVE_CASSETTE_PATHS` 去重直接 raise（`cassette_recording.py:91-97`）。故 sink 路线是唯一可行解。
- **第二消费者零回归（共享 cassette）**：8 个 `tests/incidents/test_<key>.py` 经 `llm_cassette("incident_<key>")` 复用**同一份**被重录、变大的 `cassette.jsonl`（Planner+诊断），但走 Planner-only `assert_incident_snapshot`。已核 `PlaybackBackend` **不强制全量消费**（`playback.py:111` 明示「lines are skipped so cassettes can be hand-edited without strict [consumption]」、无 leftover 断言），故新增诊断 record 不被 Planner-only 查找 → **incident 快照测试零回归**（无需改这 8 个测试 + `snapshots/<key>.md`）。此结论须作为验收断言之一。
- **整文件覆写不破"不改 Planner record"——byte 级验收 + 前提显式 + 漂移退路**：authored Planner 响应不变 + 冻结时钟 + **录制侧 Settings 与历史一致** → 重录 Planner record 与现有 byte-identical。byte-identity 的**全部前提（须显式断言、非默认）**：(i) 录制侧 `<incident context_factory>` 的 `Settings.agent.primary_model` 必须等于历史录制的 `claude-opus-4-7`（现 `_harness` 用 `Settings(agent=AgentSettings())`；demo 用 `_DemoSettings`——两者 `primary_model` 已核均默认 `claude-opus-4-7`、env-stripped 下相等，model 进 key）；(ii) `register_default_tools(collector=...)` 的 collector 是纯旁路、**绝不进 messages**；(iii) `asyncio.gather` 保 tool_result 为 response 顺序（已核 `loop.py:405`，Planner messages 确定）。**验收钉死 byte 级 `git diff --exit-code`**（回放绿/key 相等**不**守护 byte-identity——key 是 canonical 哈希、格式字节漂移仍可绿）：重录后 Planner record 行与 `git show HEAD:` 逐字节相同。**漂移退路**：**先只重录 cpu_saturation 一套验 `git diff` 真空**；非空（即便语义等价）说明上述某前提被违反 → **abort 并定位/修违反的前提（录制侧 Settings/路径），不得直接 accept-and-rebaseline**（rebaseline 即改 Planner record 内容、破 Non-Goal）。验空后再批量重录其余 7 套。
- **替代方案（否决）**：(a) 手编 request record——byte 漂移必致 CassetteMiss；(b) 临时文件 + key-set-difference append-merge——需要不存在的 composite backend，确定性重录已让整文件覆写安全，多此一举。

### D-3.6：诊断 record 隐式依赖 Planner 段输出 —— contributor 约定 + e2e 常驻守护

诊断段 record 的 request key 含 seeded findings（来自 Planner 段产出），故**改任一场景的 Planner authored 响应 / fixture（即便"无关"小改导致 finding message 文本或顺序变）会让该场景诊断 record 的 key 漂移 → 运行期 `CassetteMiss`**。seeded findings 注入诊断 messages 的顺序由 Planner-phase collector snapshot 决定（`asyncio.gather` 保序，见 D-2），录制可复现。

- **contributor 约定（写进 contributor 文档，tasks 5.x 落实）**：改某场景 Planner authored 响应/fixture 时，**必须用扩展后的 `build_authored_responses` 整体重录该场景 cassette**（Planner+诊断段一起，确定性重录保 Planner record 内容等价）。本变更的 Non-Goal「本次不改 Planner record 内容」只约束本次，挡不住未来改动。
- **额外约定（D-7 排序键/渲染字段演进 → authored label 错位）**：authored cassette 的 `supporting_findings=["F1",...]` 是按该场景 **D-7 排序后** seeded 列表对照写死的。**改 D-7 排序键或 `_render_findings_block` 的 per-finding 渲染字段时**（如 M3 evidence DSL 改渲 evidence body 并同步进键），sort 顺序可能重排 → 同一证据 finding 从 F1 挪到 F2 → **所有 authored `[F#]` label 错位** → 录制期 FakeBackend 无自纠错 → 录成 0 假设。故此类改动**必须连各场景 authored label 对照新排序一并重写并整体重录**。常驻 e2e（≥1 hypothesis liveness + misses==[]）会在忘了重写时 loud-fail 兜底，但约定须显式写进 contributor 文档。
- **常驻守护**：tasks 的「8 套全链离线回放 `misses==[]` + 诊断段无 CassetteMiss」e2e 测试就是这条耦合的回归网——未来改 Planner 资产忘了重录诊断 record，该 e2e 立刻红。这条 e2e 必须在 CI 常驻。

### D-4：渲染与退出码复用 `--intent` 的 Report 表面

- 渲染：`render_intent_report(report, fmt)`（已存在，json=render_json 脱敏 / md=intent 风格 narrative+Findings+根因假设+遥测）。
- 退出码：`_compute_intent_report_exit_code(report)`（已存在，含 `partial`→2）。demo 的 exit 3（资产缺失 / --output 不可写 / 未知场景）仍由 demo caller 边界自管（pre-flight 不变）。
- no-result（collector 真空，demo 正常路径不会触发）：复用 `--intent` 的 None→stderr 降级+exit2+不 persist。

### D-5：`demo run --persist` 默认关，落标准 store，target_name 标记 demo 来源

- 新增 `--persist` flag（默认 False）。仅在显式 `--persist` 时调 `ReportStore.save`（复用 `--intent` 的 `_persist_report` + orphan/persist-fail 升 exit2）。
- **落盘到标准 `ReportStore`**（XDG 默认路径，与 `reports show/diff` 读的同一个），这是离线复现闭环的前提。「自包含/不读用户配置」约束的是**读** targets.yaml / API key，**写** Report 是 `--persist` 的显式动作，不违反。
- demo Report 的 `target_name` 取**显式** demo 标记 `demo:<scenario>`（不是 ReplayTarget 的 registry 注册名 `incident-host`），与真实 run 区分。区分**仅**靠 `target_name` 的 `demo:` 前缀：`target_type` 忠实取 `ReplayTarget.type`（已核为 `Literal["local","ssh"]` 的 impersonate 值，**不是** `demo`/`replay`），故 `target_type` 不参与、也不能当 demo 区分手段——否则会与真实 local/ssh run 撞。
- **与现有 `reports` 契约相容（不扩范围）**：`reports show <run_id>` 按全局 run_id 解析（无 target 参数，已核 `reports.py` show_cmd 仅收 run_id），故取回不受 `demo:` 前缀影响；`reports list <target>` 是 per-target 过滤、行不渲染 target_name（`RunIndexRow` 仅 `run_id/timestamp/status/finding_count` 四字段，`extra="forbid"`）。故「可区分」= 取回时 target_name 带前缀 + 可用 `reports list demo:<scenario>` 定向查询，**不**要求 demo 标记出现在真实 target 的 list 行里（那要改 `RunIndexRow`/`_format_row`，属 Non-Goal 外，不做）。
- **替代方案（否决）**：写临时/隔离 store——则 `reports show/diff` 找不到，闭环演示失效，违背 Demo Path 目标。

### D-7：seeding 顺序确定化（共享核心改动，demo 离线确定性的硬前提）

**问题**：诊断段首条 user message 由 `_render_findings_block(seeded_findings)` 按 **F1/F2 位置标签**渲染（`diagnostician_tools.py`：authored `correlate_findings.supporting_findings` 引用的是 **ordinal label `["F1","F3"]`，不是 finding id**）。F1/F2 由 `FindingStore.seed(stamped)` 按**列表顺序**位置式分配，而 `stamped` 来自 `_seed_findings_from_snapshot(collector.snapshot(), ...)` 的 collector 顺序。已核 `InspectorResultCollector` docstring 明写：**同一 response 内并行多 inspector 的 append 顺序 = handler 完成顺序，跨 run 不保证稳定**。8 套里 cpu_saturation / memory_oom / disk_inode 等在 turn1 并行分发 2 个 inspector，故重录/回放时两 inspector 完成顺序可能翻转 → F1/F2 标签互换 → 诊断段 messages 文本变 → **诊断段 request key 非确定** → 回放期**非确定 `CassetteMiss`**（本次录绿下次回放红），且 authored `[F1]` 引用错位到另一 finding（语义错但 id-consistency invariant 仍过，静默错误）。这直接打破 demo「离线确定性回放」卖点。

**决策**：在共享核心的 seeding 步骤（`_seed_findings_from_snapshot`，`--intent` 与 demo 共用）对 Planner-phase snapshot 施加**确定性稳定排序**后再 `store.seed`，使 F1/F2 标签分配跨 run 确定。这样诊断段 messages byte-stable、request key 确定、authored `[F1]` 引用稳定。

- **排序键 = `_render_findings_block` 渲染投影的超集（不能只用 `finding.id`）**：已核 `compute_finding_id` **severity-agnostic**（`sha256(name\0version\0message)`，排除 severity），故 `(name, version, finding.id)` 会 tie（同 inspector 同 message 不同 severity）→ 回退 collector 非确定序。正解是让排序键覆盖**诊断段渲染行实际输出的每一个 per-finding 字段**（已核 `diagnostician.py:144-145` 行格式：`severity={f.severity} inspector={name/version} tags={",".join(f.tags)} evidence={len(f.evidence)} :: {f.message}`，label 位置式排除）。键定为：
  ```
  (inspector_name, inspector_version, severity, tuple(f.tags), len(f.evidence), message)
  ```
  注意：(i) tags 用 **`tuple(f.tags)` 原序**（匹配渲染的 `",".join(f.tags)`，**不** `sorted`，否则同 tag-set 不同序会 tie 却渲染不同 `tags=a,b` vs `b,a`）；(ii) evidence 用 **`len(f.evidence)`**（渲染只输出计数、非 body，故键只需计数）。
- **为何无需 fail-loud 守护（round-6 的守护多余且会误伤，删除）**：用上面的键，**key tie ⇒ 渲染行逐字节相同 ∧ 同 id**（**单向**蕴含；反向不成立也不需要——`inspector_version` 入键但不入渲染，故键是渲染投影的**真超集**）。tie 的两条：(a) 渲染行相同 → F1/F2 谁先谁后对 messages 文本**无影响**（两行一样）；(b) `id = compute_finding_id(inspector_name, inspector_version, message)`（已核 `agent/diagnostician.py` 渲染只用 `f.inspector_name` 纯 name，键的 `inspector_name`/`inspector_version` 与 id 同源自 stamp 的 `ir.name`/`ir.version`）——tie 蕴含 name/version/message 三者同 → **同 id**，authored `[F1]`/`[F2]` 解析到**同一 finding id**。两点合起 → 诊断段 messages 与 label 解析**全确定**，tie 完全无害。故**不需要**「重复 finding fail-loud」守护（它会把 evidence-body 不同但渲染计数相同的**合法**finding 误判重复）。
- **不变量（单向 superset）**：排序键须 ⊇ `_render_findings_block` 输出的每个 per-finding 字段投影（键可严格更大，如含渲染不输出的 `inspector_version`）；**新增渲染 per-finding 字段必须纳入键，反之不要求**。tasks 1.1 验收锁这条单向 superset。注：渲染对空 tags 输出 `"-"` 哨兵（`tags=",".join(f.tags) or "-"`），键用 `tuple(f.tags)`（空→`()`）**故意不镜像该哨兵**——`tuple(f.tags)` 对 `tags==["-"]` 这种病态只会**过度**区分（无害、额外稳定），**不要**把键塌缩成渲染字符串（那会让 `tags=[]` 与 `tags=["-"]` 撞键而 id 可能不同）。

- **对 `--intent` 的影响（行为等价、非回归）**：`--intent` 的诊断段本就存在同一非确定性（只是 live 路径不回放 cassette 故从不表现为失败，仅 prompt 内 finding 顺序逐 run 抖动）。加确定排序是**行为改善**（同一组 findings、稳定顺序），不改诊断结论。D-1「核心主体与 `run_intent_diagnosis` 逐字一致」据此**加一处例外**：seeding 步骤插入稳定排序。现有 `--intent` 回归测试若 pin 了某 F1/F2 具体分配，须按新稳定顺序更新（之前是非确定，pin 本就脆）。
- **collector 本身不排序**：collector 的 append 顺序语义不动（identity 是内容派生、非位置），排序只发生在 seeding 入口，最小改面。
- **替代方案（否决）**：依赖「ReplayTarget 下 gather 完成顺序事实确定」——asyncio 不契约保证、collector docstring 明确否定，把确定性押在调度细节上脆弱且未证。显式排序是唯一稳健解。

## Risks / Trade-offs

- [Diagnostician cassette 与真实 diag loop 请求键漂移致 CassetteMiss] → 录制时用与 `--intent` 同套 DiagnosticianAgent 装配生成请求；测试断言 `replay_target.misses == []` + 全链跑通；CassetteMiss 落 exit 2（与 Planner 段同处理）。
- [编排去重重构动到 `--intent` 主体引入回归] → 核心函数主体与现 `run_intent_diagnosis` 逐字一致，现有全部 `--intent` 测试（test_inspect_intent*.py）作为回归守护必须全绿；薄包装只负责 backend/context_factory 注入。
- [demo --persist 污染用户报告库] → target_name 打 `demo:` 前缀；默认不 persist（需显式 flag）；文档说明 demo Report 可经 `reports` 正常管理/删除。
- [demo md/json 输出 BREAKING 破坏既有 demo snapshot 测试] → demo 是演示命令无下游契约消费者；更新 demo snapshot 到 Report 风格（注意 [[project_precommit_eof_fixer_aborts_first_commit]] 尾换行 + `.rstrip("\n")`）。
- [8 套场景逐一补诊断 cassette 工作量] → 8 套机械重复，但每套只新增**恰好 2 条**诊断 record（D-3 单 `correlate_findings`：1 条 tool_use + 1 条 finalize）；用 D-3.5 扩展的 authored-response 录制（单 RecordingBackend(FakeBackend) 全链重录，零真实 key）批量生成，**不手编 request record**。
- [`≥1 hypothesis` 守护被误当质量门] → 该守护是 **liveness 守护**（抓"诊断段录成空关联 / finalize 时没调 `correlate_findings`"→ 0 假设 → 验收 loud-fail，对应 Failure Mode 2 的 `_暂无根因假设_` 路径），**不评估假设正确性/质量**（假设内容由 cassette 录死，录什么有什么）。spec / Goal 措辞须如实表述为 liveness，不可暗示它证明假设质量。

## Migration Plan

- **确定性重录资产**：扩展 authored 响应后整文件重录 cassette.jsonl；authored Planner 响应不变 + 冻结时钟 → Planner record byte-identical，fixture 不动 → Planner 段零回归（验收断言 Planner record 集合逐条相等）。
- **内部重构**：抽 `run_diagnosis_pipeline` 后 `run_intent_diagnosis` 退薄包装，`--intent` 行为不变（测试守护）。
- **回滚**：本变更纯加法（demo 行为升级 + 资产追加 + 一个内部重构），回滚 = revert 该 PR；不涉及持久化 schema / 数据迁移。
- 部署：随 wheel 发新 cassette package-data；用户 `pip install` 升级即得。

## Open Questions

（无剩余 Open Question。）

**已收口的原 Open Questions**：
- **诊断 cassette 录制方式** → 升级为硬决策 D-3.5（扩展 authored-response 离线录制、单 RecordingBackend(FakeBackend) 全链重录、零真实 key、禁手编 request record）。
- **target_name 格式** → 定为 `demo:<scenario>`（D-5 + spec + tasks 一致采用）。已核 `Report.target_name` 是 `Field(min_length=1)` 无 regex、冒号合法。不再是待定项。
