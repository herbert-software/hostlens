# diagnostician-agent 规范

## 目的

定义 Diagnostician Agent 契约——复用 `AgentLoop` + 外部系统提示串接于 Planner 之后、findings 进 messages 禁进 system 以命中 prompt cache、`correlate_findings` 为纯结构化输出通道按序号标签引用 finding、`request_more_inspection` 复用 `InspectorRunner` 且不暴露 target 发现、编排层给 Planner findings 盖稳定 id、`DiagnosticianResult` 聚合带 id 的 findings / hypotheses / reconcile 后 status、两个 loop 的 terminal_status reconcile 成单一 `ReportStatus`。
## 需求
### 需求:`DiagnosticianAgent` 必须复用 `AgentLoop` + 外部系统提示，串接于 Planner 之后

`DiagnosticianAgent` 必须与 `PlannerAgent` 同构：包一个 `AgentLoop` 实例 + 一份外部系统提示 `agent/prompts/diagnostician.md`（缺失/不可读时构造期 raise `ConfigError`，禁止静默降级为空提示）。系统提示必须渲染诊断师工具总览（渲染方式与 `planner.md` 一致，按 `spec.name` 升序保证 byte-stable）。`run(intent, findings, *, observer=None)` 必须接受带稳定 `id` 的 findings 列表 + intent，驱动诊断 loop，并把 `observer` 原样透传给 `AgentLoop.run`（诊断师不解释/过滤 `LoopEvent`）。backend 必须只注入 `AgentLoop`，禁止进入 context_factory 产出的 `ToolContext`（ADR-008）。诊断师 loop 必须复用 `settings.agent` 的 budget 配置值，其 token/turn 计数独立于 Planner（各自 `run`、各自 `LoopUsage` 从 0 起）。

#### 场景:系统提示模板缺失构造期失败

- **当** 构造 `DiagnosticianAgent` 但 `diagnostician.md` 不可读
- **那么** 必须 raise `ConfigError`（kind 指明提示模板缺失），禁止以空系统提示继续

#### 场景:findings 作为诊断输入

- **当** 以一组带 `id` 的 findings + intent 调用 `DiagnosticianAgent.run`
- **那么** findings 必须出现在诊断 loop 的 messages（首条 user message，含每个 finding 的序号标签），禁止注入 system 系统提示

### 需求:findings 列表必须进 messages、禁止进 system，以保系统提示 byte-stable 命中 prompt cache

诊断师的动态输入（findings 列表及其序号标签）必须作为诊断 loop 的首条 user message 内容，**禁止**内插进 `diagnostician.md` 系统提示。系统提示 + 诊断师工具 schema 必须 byte-stable 跨 run，使 prompt-cache 断点 A（`tools + system` 静态前缀）可命中（CLAUDE.md §4.8）。findings JSON 落在 messages 尾部，由 `AgentLoop` 既有滚动断点 B 覆盖。当 `backend.capabilities.prompt_caching=False` 时，由 `AgentLoop` 既有逻辑决定不注入 `cache_control`，诊断师不做特殊处理。真实 cache 命中计数依赖真打 API，禁止用 `FakeBackend`/`PlaybackBackend` 的 usage 断言真实 hit rate（见 tasks）。

#### 场景:系统提示跨 run 字节稳定

- **当** 对两个不同 intent / 不同 findings 各跑一次诊断
- **那么** 两次诊断 loop 的 system 系统提示内容必须字节一致（findings 差异只体现在 messages，不污染 system）

#### 场景:prompt_caching 关闭时不注入 cache_control

- **当** backend `capabilities.prompt_caching=False`
- **那么** 诊断 loop 的请求必须不含任何 `cache_control` block（由 `AgentLoop._inject_cache_control` 既有逻辑保证）

### 需求:`correlate_findings` 必须是纯结构化输出通道，用序号标签引用 finding

必须新增 `correlate_findings` ToolSpec 作为诊断师产出根因假设的结构化输出通道：`input_schema` 必须是单条根因假设的字段形状（`description` / `confidence ∈ {low, medium, high}` / `supporting_findings: list[str]` / `suggested_actions: list[str]`）。`supporting_findings` 必须用首条 user message 里呈现的**序号标签**（如 `["F1", "F3"]`）引用 finding，**禁止**要求模型逐字符抄写 16-hex `Finding.id`（降低抄写错误率与不收敛风险）。handler **禁止**做任何关联/推理计算，且**只做命中校验**（用 finding-store resolve 标签仅为判断悬空，不在 handler 里记录真 id —— 真 id 由编排层在 harvest 时 resolve，见下）（关联推理是 Agent 的职责，§4.2）。**finding-store 必须以序号标签为唯一键**（不以 `Finding.id` 为键）：`compute_finding_id` 故意排除 severity 且 Planner harvest 不去重，两个 `(inspector_name, version, message)` 相同而 severity 不同的 finding 会得到**同一真 id**；以 id 为键会令后者覆盖前者、令标签悬空，故 store 是 `label → Finding`（label 唯一），`resolve(label) → real_id` 允许多个 label 映射到同一真 id。

**handler 与编排层的 resolve 分工（关键，避免机械不可能）**：`ToolInvocation` 只有 `input`（模型原始 args = 标签）与 `output`（ack，不含真 id）两个可读字段，handler 解析出的真 id 无处写回 `ToolInvocation`。故 handler **只做命中校验**（用 finding-store resolve 标签仅为判断是否悬空；任一标签悬空——含同 turn 前向引用尚未由 tool_result 返回的标签——必须返回结构化 error envelope，使 loop 喂回模型下一轮自纠，禁止静默接受）；**编排层在 harvest 时**从每个成功 `correlate_findings` 调用的 `inv.input` 读 `description`/`confidence`/`suggested_actions`/`supporting_findings`(标签)，用它持有的同一 finding-store `resolve(label)→real_id` 把标签解析成真 id，组装 `RootCauseHypothesis`。最终 `RootCauseHypothesis.supporting_findings` 必须是**真 `Finding.id`**（持久化/渲染语义不变）。`CorrelateFindingsOutput` **只回 `accepted: bool`（可选 echo 接受的标签），禁止回传真 `Finding.id`**（模型用标签即可，避免无谓地把 id 回吐给模型并简化脱敏面）。策略元数据必须为 `surfaces={"agent"}`、`side_effects="none"`、`sensitive_output=False`（输出仅为 ack；敏感内容载于模型生成的 description，经已脱敏 findings 派生，由下游脱敏测试覆盖）。

#### 场景:每条假设一次调用并被 harvest

- **当** 诊断师对某 intent 产出两条根因假设
- **那么** 诊断师必须调用 `correlate_findings` 两次，编排层必须 harvest 出含两条 `RootCauseHypothesis` 的列表，每条的 `supporting_findings` 为解析后的真 `Finding.id`

#### 场景:悬空序号标签被拒并回传自纠

- **当** `correlate_findings` 的 `supporting_findings` 含一个不在本 run finding 集合中的序号标签
- **那么** handler 必须返回结构化 error envelope，loop 把它作为可自纠错误喂回模型，禁止 crash、禁止接受该假设

#### 场景:correlate_findings 不上 MCP

- **当** 检视 `correlate_findings` 的 `surfaces`
- **那么** 必须仅含 `"agent"`，禁止含 `"mcp"`

#### 场景:同 id 碰撞 finding 经唯一标签区分

- **当** 同一 inspector（同 version）产出两个 message 相同但 severity 不同的 finding（`compute_finding_id` 因排除 severity 给出同一真 id）
- **那么** finding-store 必须给它们分配两个不同序号标签（如 `F1`/`F2`），两标签 resolve 到同一真 id 而不互相覆盖、不令标签悬空

### 需求:`request_more_inspection` 必须复用 `InspectorRunner` 执行、暴露 status、target 固定、不暴露 target 发现

必须新增 `request_more_inspection` ToolSpec，让诊断师在证据不足时补查一个 inspector（通常是 Planner 未跑的；不强制跟踪「已跑过」集合）。其 handler 是**新写的**（不能直接调 `run_inspector_handler`，因后者返回剥了 id 的 `RunInspectorOutput`），但必须**复刻 `run_inspector_handler` 的完整编排、复用同款 `InspectorRunner` 采集执行引擎（禁止新造采集逻辑）**，依次：

1. `ctx.inspector_registry.get(inspector_name)` 拿 manifest，`inspector_not_found` 必须转结构化 `ToolError`；
2. `ctx.target_registry.get(target_name)`（target_name 由编排层闭包固定为 CLI 的 `<target>`）把字符串解析成 `ExecutionTarget` 对象（`InspectorRunner.run` 收对象不是 name），unknown target（`KeyError`）必须转结构化 `ToolError`；
3. **clock 透传（可选）**：`register_diagnostician_tools` 必须接受可选 `clock`（镜像 `register_default_tools(clock=...)`）并把它透传给 `InspectorRunner`。`--intent` 路径（`build_planner` 现状无 clock）传 `None` → 真 UTC，与该路径 `run_inspector` 行为一致；frozen-clock 仅在未来 replay 装配（如把 demo 接入 Diagnostician）传入时才生效以保 `sampling_window` inspector 命令 byte-stable。本提案 `--intent` 路径不依赖 frozen clock；
4. `InspectorRunner(...).run(manifest, target, parameters=dict(args.parameters) if args.parameters else None, allow_privileged=False, cancel=ctx.cancel)` 拿 `InspectorResult`（`parameters` 透传必须与 `run_inspector_handler` 一致、勿漏；`allow_privileged` 必须为 `False`，沿用 agent surface 铁律）；
5. **version 必须直接用 `InspectorResult.version`**（runner 已填，进程内未丢；**禁止**反查 `ctx.inspector_registry` —— 反查只是 D-3 Planner 路径因 wire 剥离才用的退路），调 `compute_finding_id(result.name, result.version, message)` 盖 id；
6. 在 per-run finding-store 给每个新 finding **分配新唯一序号标签** → append → 返回。

其 `output_schema`（本工具是新工具，不受 `run_inspector` cassette 稳定性约束）必须携带：(1) inspector 的 `status`（`ok`/`timeout`/`target_unreachable`/`requires_unmet`/`exception`，使诊断师能区分「跑了没发现」与「失败被吞」）；(2) 每个 finding 的稳定 `id`；(3) 每个 finding 的序号标签。新 findings（带 id + 标签）append 进 finding-store 后，使**后续 turn** 的 `correlate_findings` 可引用这些新标签；**同 turn 前向引用**（模型臆造尚未返回的标签）按 finding-store 并发规则处理（resolve 悬空 → error envelope → 下轮自纠）。策略元数据必须为 `surfaces={"agent"}`、`side_effects="read"`、`sensitive_output=True`（与 `run_inspector` 一致；`id` 是 message 内容指纹、不引入新敏感字段）。诊断师工具注册表**禁止**包含 `list_targets`（§7 最小能力）。

#### 场景:补查复用执行引擎、暴露 status 并盖 id

- **当** 诊断师调用 `request_more_inspection` 补查一个 inspector
- **那么** 必须经 `InspectorRunner` 执行该 inspector，`output` 必须含 `status` 与带稳定 `id` + 序号标签的 findings，且这些 findings 必须进入 finding-store 供后续 turn 的 `correlate_findings` 引用

#### 场景:补查失败时 status 可见

- **当** `request_more_inspection` 补查的 inspector 因 target 不可达返回非 ok 状态
- **那么** `output.status` 必须为对应非 ok 值（如 `target_unreachable`）、findings 为空，使诊断师能区分失败与「无发现」，禁止把失败静默吞成与「无发现」无法区分

#### 场景:unknown inspector / unknown target 结构化回传

- **当** `request_more_inspection` 指定一个未注册的 inspector 名，或闭包固定的 target_name 不在 `ctx.target_registry`
- **那么** 必须分别在 inspector lookup / target lookup 步骤转成结构化 `ToolError` 回传（不 crash 整个 tool 轮），诊断师据错误自纠

#### 场景:诊断师注册表不含 list_targets

- **当** 检视 `register_diagnostician_tools` 装配出的注册表
- **那么** 必须含 `correlate_findings` / `request_more_inspection` / `list_inspectors`，**禁止**含 `list_targets`

### 需求:编排层必须给 Planner findings 盖稳定 id，零 wire / 零 ToolContext 改动

`--intent` 编排层必须给 findings 盖稳定 `id`，且 id 必须统一到一个来源以保 `hypotheses[*].supporting_findings` 与 `Report.findings` 一致（见 `agent-report-assembly` 能力）：

- **新机制（本提案）**：id 来自 per-run `InspectorResultCollector` 供给的**真 `InspectorResult.version`**，经 `compute_finding_id(name, version, message)` 盖（**不再**用 `InspectorRegistry.get(name)` 反查 version）。两个时点用同源 version、同函数、同输入，故 FindingStore seed 的 id 与最终 `Report.findings` 的 id 天然相等（**id 一致由内容确定性保证，非组装顺序**；详见 `agent-report-assembly` D-2/D-3）。`FindingStore` 在诊断 loop **前** seed（Planner-phase findings 经 `compute_finding_id` 盖 id），权威 `Report` 在诊断 loop **后**由 `from_inspector_results` 组装；二者 id 相等。`supporting_findings` 引用的真 id ∈ `Report.findings`。
- **`stamp_planner_findings` 删除**：其唯一调用点是 `--intent` 路径，改用 collector 真 version 后变死代码，本提案**删除该函数 + 其测试**（见提案影响）。其原 fail-loud（inspector 跑后被卸载致 registry 反查 `inspector_not_found`）的保护对象随之消失——version 在 run 时随 `InspectorResult` 落定，不再有事后反查、该场景不再可能触发。
- **不变项**：仍**零 wire 改动**（`RunInspectorOutput` 不变、cassette 全命中——collector 是 out-of-band）、**零 `ToolContext` 改动**（6 字段，ADR-008；collector 经 handler 闭包注入）。组装后必须做 id 一致性不变量校验（每个 `supporting_findings` id ∈ `Report.findings`），不满足 fail-loud（`internal: ... → exit 2`）。

#### 场景:id 同源于 from_inspector_results

- **当** Planner 跑了两个 inspector，编排层用 collector 的 `InspectorResult` 经 `from_inspector_results` 组装 Report
- **那么** finding id 必须由 `compute_finding_id`（用真 `InspectorResult.version`）盖出，`hypotheses[*].supporting_findings` 引用的 id 必须 ∈ `Report.findings`，无 registry 反查

#### 场景:盖章不改 run_inspector wire

- **当** 启用 collector 后回放既有 incident/demo/planner cassette
- **那么** `run_inspector` 的 tool_result 必须字节不变、cassette 全部命中（collector 是 out-of-band 内存收集，不上 wire）

### 需求:`DiagnosticianResult` 必须聚合 findings(带 id) / hypotheses / reconcile 后的 status

诊断 loop 必须产出 frozen 的 `DiagnosticianResult` 作为**编排层内部聚合**（不再是 `--intent` 的 CLI / 持久化表面契约——CLI 表面是 `Report`，见 `inspect-cli-command` 与 `agent-report-assembly`）。字段必须含：`narrative`（诊断 loop 的 `final_text`，降级路径下可能为空字符串）、`findings: list[Finding]`（带稳定 id 的 canonical 集合，id 同源于 `from_inspector_results`）、`hypotheses: list[RootCauseHypothesis]`（harvest 自 `correlate_findings`，`supporting_findings` 为 Report 的真 finding id）、`status: ReportStatus`（按 reconcile 规则得出）、`planner_result: PlannerResult`、`diagnostician_loop: LoopResult | None`（`None` 当且仅当诊断阶段被跳过）。编排层必须把 `DiagnosticianResult.hypotheses` 投影进持久化 `Report.hypotheses`，把 `narrative` 投影进 Report 渲染 / `metadata`（见 `agent-report-assembly`）。**不再禁止**组装 `reporting.models.Report` —— 本提案正是让 `--intent` 路径经 `from_inspector_results` 产出忠实 Report（取代 `add-diagnostician-agent` 的 Scope-Core「不产 Report」约束）。

#### 场景:无根因假设时 hypotheses 为空

- **当** 诊断师未调用任何 `correlate_findings` 且以 `end_turn` 带文本结束（`terminal_status=ok`）
- **那么** `DiagnosticianResult.hypotheses` 必须为空列表，投影出的 `Report.hypotheses` 也为空，其余字段正常

#### 场景:DiagnosticianResult 不再是 CLI 表面契约

- **当** `--intent --format json` 输出
- **那么** stdout 必须是 `Report` 的序列化（非 `DiagnosticianResult`）；`DiagnosticianResult` 仅作编排层内部聚合存在，不对外暴露为 json 顶层结构（见 `inspect-cli-command` 的 BREAKING 映射）

### 需求:两个 loop 的 `terminal_status` 必须 reconcile 成单一 `ReportStatus`

`DiagnosticianResult.status` 必须按下列规则由 Planner loop 与 Diagnostician loop 的 `terminal_status` reconcile（`_TerminalStatus` 与 `ReportStatus` 是 6 值重叠子集，非 1:1）：

- Planner `terminal_status=ok`：取 Diagnostician 映射值 —— `ok`→`ok`；`degraded_rate_limited`/`degraded_token_budget`/`degraded_max_turns`/`degraded_no_planner`→同名值；`empty_response`→`empty_response`；**`failed_api_unavailable`→`degraded_no_planner`**（Planner findings 已在手，禁止因诊断师网络抖动丢弃）。注：诊断师 loop 仅在「调任何工具前即不可达」（`tool_invocations` 为空）时返回 `failed_api_unavailable`；若它已调过工具再不可达，loop 自身已改写为 `degraded_no_planner` → 落入上一行同名映射。两条来源最终都收敛到 `degraded_no_planner`。**命名注**：此处 `degraded_no_planner` 是**语义复用而非字面** —— 本场景 Planner 成功（findings 完好），只是诊断师降级；因 `ReportStatus` 无「诊断师降级、Planner 完好」的专属值才复用之。下游消费方勿据该值字面推断「Planner 失败」；更精确的 status 值留待「忠实 Report」后续提案。
- Planner `terminal_status` ∈ {`degraded_rate_limited`,`degraded_token_budget`,`degraded_max_turns`,`degraded_no_planner`,`empty_response`}：诊断阶段跳过、`status` 取 Planner 的值、保留 Planner 已 harvest 的 findings（可能非空）。这些值都有对应 `ReportStatus`，故仍产出 `DiagnosticianResult`（即便 `empty_response` 下 findings/hypotheses 可能为空 —— 它仍承载 Planner loop 遥测 turns/status，值得输出，区别于下一条无任何遥测可言的 `failed_api_unavailable`）。
- Planner `terminal_status=failed_api_unavailable`：**禁止产出 `DiagnosticianResult`**（无对应 `ReportStatus`，此值意味着 Planner 一次工具都没调成、无 findings 也无可信遥测；归 M4 RunStatus 边界，由 CLI 走 no-result 降级路径）。

本路径无 `InspectorResult`，故 `status` **禁止**产出 `partial`（`partial` 仅由 `from_inspector_results` 派生）。

#### 场景:Planner 成功诊断降级取诊断值

- **当** Planner `terminal_status=ok` 而 Diagnostician `terminal_status=degraded_max_turns`
- **那么** `DiagnosticianResult.status` 必须为 `degraded_max_turns`，且仍输出 Planner 已收集的 findings

#### 场景:诊断师调工具前 API 不可达映射 degraded_no_planner

- **当** Planner `terminal_status=ok` 而 Diagnostician `terminal_status=failed_api_unavailable`（诊断师未调任何工具即不可达）
- **那么** `DiagnosticianResult.status` 必须为 `degraded_no_planner`，Planner findings 必须保留、禁止丢弃

#### 场景:诊断师空响应映射 empty_response

- **当** Planner `terminal_status=ok` 而 Diagnostician 以空响应 `end_turn`（`terminal_status=empty_response`）
- **那么** `DiagnosticianResult.status` 必须为 `empty_response`（与「end_turn 带文本无假设→ok」区分）

#### 场景:Planner 降级跳过诊断取 Planner 值

- **当** Planner `terminal_status=degraded_rate_limited`
- **那么** 诊断阶段必须跳过，`DiagnosticianResult.status` 必须为 `degraded_rate_limited`

#### 场景:Planner API 不可达不产结果

- **当** Planner `terminal_status=failed_api_unavailable`
- **那么** 禁止产出 `DiagnosticianResult`（无对应 `ReportStatus`，归 M4 边界）

### 需求:诊断师装配必须支持 narrate-only 变体（仅 correlate_findings、禁再巡检 / 选 target）

除既有「全装配」（`register_diagnostician_tools` 装出 `correlate_findings` + `request_more_inspection` + `list_inspectors` 三件,见 §需求:`request_more_inspection` 必须复用 `InspectorRunner` 执行、暴露 status、target 固定、不暴露 target 发现 的「诊断师注册表不含 list_targets」场景）外,**必须**额外提供一条 **narrate-only 装配路径**（新函数,或现有装配函数的新参数),供确定性巡检模式（见 `deterministic-inspection-mode` 能力）的「LLM 只对已采集结果写根因叙述」场景使用。该路径**必须**:

- **只注册 `correlate_findings`**（复用既有 `_build_correlate_findings_spec` 工厂,不另造结构化输出通道）。
- **禁止注册 `request_more_inspection`**——结构上让 narrate-only 的 LLM 拿不到再跑 inspector 的能力。
- **禁止注册 `list_inspectors`**——narrate-only 不需要发现可补查的巡检项。
- **禁止注册 `list_targets`**（与全装配同铁律,§7 最小能力)。

理由:确定性巡检的覆盖在采集阶段已固定（逐 target 跑固定集）,诊断阶段**仅**做根因叙述;若装出 `request_more_inspection`,LLM 可在 narrate 阶段追加巡检 / 漫游,破坏「覆盖确定 + token 有界」的确定性契约。既有全装配（三件）需求**不变**——agent 模式诊断师仍需 `request_more_inspection` 在证据不足时补查。

#### 场景:narrate-only 装配的注册表只含 correlate_findings

- **当** 检视 narrate-only 装配路径装出的工具注册表
- **那么** **必须**仅含 `correlate_findings`,**禁止**含 `request_more_inspection`、`list_inspectors`、`list_targets`

#### 场景:全装配路径不受影响

- **当** 检视既有全装配 `register_diagnostician_tools` 装出的注册表
- **那么** **必须**仍含 `correlate_findings` / `request_more_inspection` / `list_inspectors`（既有行为不变,**禁止**含 `list_targets`）
