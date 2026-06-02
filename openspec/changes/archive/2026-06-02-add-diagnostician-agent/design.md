## 上下文

`PlannerAgent`（M2.4）已能把意图拆解成只读巡检并综述 findings，但 `--intent` 路径止步于 `PlannerResult`（扁平 findings + narrative），无跨信号关联、无根因判断。`Report.hypotheses` / `ReportStatus.degraded_*` 早已定义形状但无产出方。本提案引入第二个 Agent —— Diagnostician —— 消费 Planner 的 findings 产出根因假设。

关键的既有事实（约束本设计，已读源码确认）：

- `AgentLoop.run()`（`agent/loop.py`）只返回 `LoopResult{final_text, tool_invocations, turns, terminal_status, usage_totals, stop_reason}` —— **没有结构化最终答案通道**。`PlannerAgent` 靠从 `tool_invocations` harvest `run_inspector` 输出来拿 findings。
- **降级路径的 `final_text` 多数为空，但非全部**：`loop.py` 的 `_finalize` 仅在 `end_turn` 与 `max_tokens`（stop_reason）两条路径传 `final_text`；预检 token 守卫 / 重试耗尽 / max_turns / refusal / rate_limited / api_unavailable 路径默认 `final_text=""`。**注意 `degraded_token_budget` 两条来源**：预检守卫触发 → 空；`max_tokens` stop_reason 触发 → 携带 `final_text`。故渲染与验收**必须容忍空 narrative，且不可假设降级时 narrative 一定非空、也不可假设一定为空**。
- `loop.py` 第 247-249 行：`_call_with_retry` 返回 `failed_api_unavailable` 后，若 `tool_invocations` **非空**则改写为 `degraded_no_planner`；**空**则保留 `failed_api_unavailable`。故一个 loop 的 unavailable 家族落在哪个值，取决于它此前是否调过工具。
- `loop.py` 第 282-293 行：`end_turn` 时 `status = "ok" if response.content else "empty_response"` —— 诊断师 end_turn 带文本但无 tool_use → `ok` + `hypotheses=[]`（合法）；end_turn 空响应 → `empty_response`。
- `loop.py` 第 386-403 行：同一 turn 内多个 tool_use block 经 `asyncio.gather` 并行 dispatch（单线程协程，handler 内无 await 的 dict 读写不交错）。
- `RunInspectorOutput`（`tools/schemas/run_inspector.py`）只有 `{target_name, inspector_name, findings}`，且 `@field_serializer` 故意把每个 `Finding` 的 `id/inspector_name/inspector_version` 从 wire 上剥掉；`run_inspector_handler`（`default_tools.py`）把非 ok 状态（timeout/target_unreachable/requires_unmet/exception）静默吞成 `findings=[]` + structlog，`status`/`duration`/`version` 都不出现在 `RunInspectorOutput`。
- `compute_finding_id(inspector_name, inspector_version, message)`（`reporting/models.py`）拒绝 None 入参（None → `ValueError`），是 `RootCauseHypothesis.supporting_findings` 锚定 finding 的唯一 id 来源。
- `_TerminalStatus`（loop）取值 7 个：`ok / degraded_rate_limited / degraded_token_budget / degraded_max_turns / degraded_no_planner / empty_response / failed_api_unavailable`。`ReportStatus`（`reporting/models.py`）取值 8 个：`ok / partial / degraded_no_planner / degraded_rate_limited / degraded_token_budget / degraded_max_turns / empty_response / stored_as_orphan`。**二者是 6 值重叠、各有独有值，不是 1:1**：`_TerminalStatus` 独有 `failed_api_unavailable`（`ReportStatus` 故意不含，见 models.py 注释）；`ReportStatus` 独有 `partial`（仅 `from_inspector_results` 派生）/ `stored_as_orphan`（仅持久化层）。
- `ToolContext`（`tools/base.py`）锁死 6 字段（ADR-008，禁 `LLMBackend`），但**含 `inspector_registry`** —— 这是装配层与 handler 反查 `InspectorManifest.version` 的合法途径。
- `run_inspector` 的 clock-binding（`register_default_tools(clock=...)` → `build_run_inspector_spec`）是「per-run 依赖经 handler 闭包注入、不动 ToolContext」的既有先例，本设计的 finding-store 注入完全沿用此模式。`InspectorRunner` 是 `run_inspector_handler` 内部使用的采集执行引擎，`request_more_inspection` 直接复用它（非新造采集逻辑）。

## 目标 / 非目标

**目标：**
- `DiagnosticianAgent` 消费带稳定 `id` 的 findings + intent，产出 `list[RootCauseHypothesis]` + 诊断 narrative。
- 结构化输出（hypotheses）经工具通道离开 loop，不违反 §4.7（禁 json.loads 解析模型输出）。
- 装配层给 findings 盖稳定 id，**零 wire 改动、零 ToolContext 改动**。
- 消费此前无产出方的 `ReportStatus.degraded_*` / `empty_response` 值。
- `--intent` md 渲染出「根因假设」章节。

**非目标：**
- 不产忠实一等 `Report`、不解锁 `--intent --persist`（拆为后续提案）。**因此本提案不满足 M3 退出条件中「持久化报告含根因假设」的部分**：hypotheses 只活在 `--intent` 的 stdout `DiagnosticianResult`，`Report.hypotheses` 仍为 `[]`，`hostlens reports diff` 不覆盖 hypotheses —— 这部分由「忠实 Report」后续提案（尚未 propose）交付（见 proposal 非目标）。
- 不改 Planner / `RunInspectorOutput` wire / `ToolContext` / `Report` 模型 / `correlate_findings` 兼做 read 工具 / extended-thinking / MCP。

## 决策

### D-1：Agent 形态 —— γ（两个 Agent / 两套 prompt，诊断师复用既有采集执行引擎）

`DiagnosticianAgent` 是**第二个** `AgentLoop` 实例（与 `PlannerAgent` 同构：包一个 loop + 外部 `diagnostician.md` 系统提示），由编排层在 Planner 之后串接。诊断师有自己的小工具注册表（`register_diagnostician_tools`），其中 `request_more_inspection` 复用 `InspectorRunner`（`run_inspector_handler` 内部同款采集引擎），而非新造采集能力。

- **替代 α（诊断师配全新采集工具）**：否决 —— `request_more_inspection` 语义就是「再跑一个 inspector」，复用既有执行引擎比造新路径更诚实、更少代码。
- **替代 β（单 loop，采集+诊断揉一个 prompt + submit_hypothesis 工具）**：否决 —— 违背 TODO 的 `diagnostician.py` + 「输入=findings 列表」，且一个大 prompt 混两种关注点削弱 §4.2 的角色清晰度。Codex 独立评审同样否决 β。
- **为什么两个 loop 可接受**：诊断师 loop 串行于 Planner（非并行），不增峰值并发；两套 byte-stable 系统提示各自命中 prompt-cache 断点 A；loop.py / planner.py / diagnostician.py 三个文件把「双层 Agent」的故事讲得很清楚。

### D-2：`correlate_findings` 是纯结构化输出通道，不是关联引擎、不是 read 工具

`AgentLoop` 无结构化最终答案通道，且 §4.7 禁止「让模型返回 JSON 再 json.loads」。沿用 Planner 从 `run_inspector` invocations harvest 的对称做法：`correlate_findings` 的 `input_schema` **就是一条 `RootCauseHypothesis` 的字段形状**（`supporting_findings` 用 finding 引用标签 F1/F2，见 D-9）。

**harvest 数据流（关键，避免一个机械不可能）**：`ToolInvocation` 只有 `input`（模型原始 args = 标签）与 `output`（`CorrelateFindingsOutput` = ack，按 RC 反馈**不含真 id**）两个可读字段 —— handler 解析出的真 id **无处可写回 `ToolInvocation`**。故分工是：
- handler 只做**命中校验**（用 `FindingStore` 把标签 resolve 仅为了判断是否悬空；悬空 → error envelope）；
- **编排层在 harvest 时**从 `inv.input` 读 `description`/`confidence`/`suggested_actions`/`supporting_findings`(标签)，用它自己持有的同一 `FindingStore.resolve(label)→real_id` 把标签解析成真 id，组装 `RootCauseHypothesis`。

即「resolve 真 id」发生在 **harvest（编排层）**，不是在 handler 里「记录」（handler 没有可记录真 id 的槽位）。这与 Planner 的对称是：Planner harvest 读 `inv.output`（findings 在 output），诊断师 harvest 读 `inv.input`（假设字段在 input）+ 编排层 resolve。

```
Planner:       narrative = final_text ; findings    = harvest(run_inspector invocations)
Diagnostician: narrative = final_text ; hypotheses  = harvest(correlate_findings invocations)
```

- 模型每产一条假设调用一次 `correlate_findings`，loop 末尾 `end_turn` 的 `final_text` 即诊断 narrative。
- **替代（给 loop 加 tool_choice-forced 结构化最终输出）**：否决 —— M2 loop 无 `tool_choice` 参数，改 loop 超出本提案范围。
- **替代（`correlate_findings` 兼做 read 工具）**：否决 —— 揉两种职责糊掉边界；M2 推迟的 `read_finding_detail` 仍是独立未来工具。

### D-3：finding-id 盖章在装配层完成（重分组 + registry 反查 version + compute_finding_id），零 wire / 零 ToolContext 改动

`PlannerResult.findings` 已 flatten 丢了分组，但 `loop_result.tool_invocations` 里每个成功的 `run_inspector` invocation 的 `output` 仍自带 `inspector_name` + 它那组 findings。编排层据此：

1. 从 `loop_result.tool_invocations` 按 `(inspector_name → findings)` 重新分组（禁止用已 flatten 的 `PlannerResult.findings` 做分组源）；
2. 对每个 `inspector_name`，用 `InspectorRegistry.get(name)` 返回的 `InspectorManifest.version` 反查 version；
3. 调 `compute_finding_id(inspector_name, version, message)` 给每个 finding 盖 id（同时填 `inspector_name` / `inspector_version`）。

- **盖章 registry 必须与 Planner 跑时同一 `InspectorRegistry` 实例**（编排层持有，传给 context_factory 与盖章 helper），避免 TOCTOU。
- **此反查仅用于 Planner 路径**：Planner 路径的 `inv.output`（`RunInspectorOutput`）已被 wire 剥掉 version，故只能反查 registry「当前」version。`request_more_inspection` 路径**不反查** —— 它在进程内直接持有 `InspectorResult.version`（见 D-9）。
- **失败模式**：若 `InspectorRegistry.get(name)` 对某 name 抛 `InspectorError(kind=inspector_not_found)`（inspector 在 Planner 跑后被卸载/改名），盖章 helper 必须 **fail-loud**（让 CLI 边界包成 `internal: ... → exit 2`），**禁止**静默跳过该组 findings（跳过会让 hypotheses 引用消失的 finding）。这是罕见的竞态边界，fail-loud 比悄悄丢 finding 诚实。
- **已知近似（精度损失）**：反查到的是 registry「当前」version；若某 inspector 在 Planner 跑后被并发 re-register 成新 version，盖章用的 version 可能 ≠ 该 finding 实际产出时的 version，导致 id 偏差。本提案接受这一近似（与 D-4「不产忠实 Report」边界一致）；忠实 version propagation 由后续提案解决。
- **替代（加宽 `RunInspectorOutput` 带 version/id 上 wire）**：否决 —— 改 wire 变 request-key hash，炸所有 cassette。
- **替代（开 out-of-band 通道 / 改 PlannerResult harvest）**：否决（在本提案）—— 触碰 `ToolContext` 锁或改 Planner 契约，拆给后续提案。

### D-4：Scope-Core —— 不产忠实 Report

忠实 `ReportMeta` 需要 `InspectorResult.status / duration_seconds / version`，而 handler 投影成 `RunInspectorOutput` 时已丢。强拼 Report 必造假。故本提案产出 `DiagnosticianResult`（见 D-7），**不**产 `Report`、**不**解锁 `--persist`。M3 退出条件中「持久化报告含根因假设」的缺口已在「目标 / 非目标」与 proposal 显式承认。

- **替代（Scope-Full：本提案同时解决 propagation + 忠实 Report + persist）**：否决 —— 契约面翻倍（碰 ToolContext 或 wire），违反「架构清晰度 > 功能广度」。

### D-5：两个 loop 的 `terminal_status → ReportStatus` reconcile 规则

`DiagnosticianResult.status` 按下表 reconcile（`_TerminalStatus` 与 `ReportStatus` 是 6 值重叠子集，非 1:1，见上下文）。本路径无 `InspectorResult` 故**不产 `partial`**。

| Planner terminal_status | Diagnostician terminal_status | DiagnosticianResult |
|---|---|---|
| `ok` | `ok` | `status=ok` |
| `ok` | `degraded_rate_limited` / `degraded_token_budget` / `degraded_max_turns` / `degraded_no_planner` | `status=` 同名值 |
| `ok` | `empty_response` | `status=empty_response` |
| `ok` | `failed_api_unavailable`（诊断师调任何工具前即不可达，loop.py:247-249 情形 b）| `status=degraded_no_planner`（Planner findings 已在手，**禁止**因诊断师网络抖动丢弃）|
| `degraded_rate_limited` / `degraded_token_budget` / `degraded_max_turns` / `degraded_no_planner` / `empty_response` | 跳过诊断（`diagnostician_loop=None`）| `status=` 取 Planner 的值；保留 Planner 已 harvest 的 findings（**可能非空**，如 token_budget 在若干 inspector 成功后才触顶）；`hypotheses=[]` |
| `failed_api_unavailable` | 跳过 | **不产任何 `DiagnosticianResult`**（无对应 `ReportStatus`，且此值意味着 Planner 一次工具都没调成、无 findings；归 M4 RunStatus 边界）|

- **为什么 Planner 降级时跳过诊断**（修正此前「无 findings 可诊断」的错误理由）：Planner 降级意味着采集是**不完整**的（即便已 harvest 部分 findings）；v1 选择不在不完整采集上做根因推理（避免在残缺证据上产误导性假设），`status` 如实反映 Planner 降级，Planner 已收集的 findings 仍输出。「在部分 findings 上诊断」留作未来增强。

### D-6：诊断师工具注册表 —— 受限三件套，target 固定注入

`register_diagnostician_tools(registry, *, finding_store, target_name, clock=None)`（`clock` 镜像 `register_default_tools`，透传给 `request_more_inspection` 的 `InspectorRunner`；`--intent` 传 None）注册：

- `correlate_findings`（新，结构化输出）
- `request_more_inspection`（新，复用 `InspectorRunner` 执行）—— handler 闭包固定 `target_name`（CLI 的 `<target>` 参数），`allow_privileged=False` 沿用 agent surface 铁律
- `list_inspectors`（复用既有 spec）—— 让诊断师知道有哪些 inspector 可补查

**不含** `list_targets`：诊断师被约束在 Planner 跑过的同一 target（§7 最小能力）。

- **替代（诊断师共享 Planner 完整注册表）**：否决 —— 含 `list_targets`，诊断师可自由重新发现，违反 §7。

### D-7：`DiagnosticianResult` 输出模型

因不产 `Report`，hypotheses 落在新的 `DiagnosticianResult`：

```
DiagnosticianResult(frozen):
    narrative: str                          # 诊断 loop 的 final_text；降级时可能为 ""
    findings: list[Finding]                 # FindingStore 完整快照（Planner 盖章 findings + 所有 request_more_inspection 新增），全部带稳定 id —— canonical 集合
    hypotheses: list[RootCauseHypothesis]   # harvest(correlate_findings)；supporting_findings 已解析为真 id
    status: ReportStatus                    # D-5 reconcile 结果
    planner_result: PlannerResult           # 原样保留（findings 为未盖章原件，仅供调试/json 保真）
    diagnostician_loop: LoopResult | None    # 诊断 loop 遥测；Planner 降级跳过诊断时为 None
```

- **`findings`（顶层）必须是诊断 loop 结束后 `FindingStore` 的完整快照** —— 含 Planner 盖章 findings **加** 所有成功 `request_more_inspection` 新增的 findings（都带 id）。这是 canonical 集合：`hypotheses[*].supporting_findings` 引用的任一 id 都必须能在此找到（否则补查后产出的假设会引用渲染集合外的 id，证据链接断）。
- `planner_result.findings`（嵌套，未盖章）仅为调试保真。`--format json` 下两者并存：spec 与 tasks 5.4 必须声明下游读顶层 `findings`，`planner_result.findings` 不作权威。
- `diagnostician_loop` 为 `Optional`：Planner 降级跳过诊断时无诊断 loop（D-5）。
- md 渲染：诊断 narrative（容忍空）+ `## Findings` 摘要 + `## 根因假设`（每条 description / confidence / 关联 finding 证据 / suggested_actions；空时 `_暂无根因假设_`）+ 一行遥测。

### D-8：`correlate_findings` 的引用校验靠 per-run finding-store 闭包注入（不动 ToolContext）

handler 校验引用合法性需要知道合法 finding 集 —— 但 findings 不在 `ToolContext` 里。沿用 `run_inspector` clock-binding 先例：编排层构造一个 per-run 可变 `FindingStore`，经 `register_diagnostician_tools` 的闭包注入两个 handler。

- **`FindingStore` 的唯一键是序号标签（label），不是 id**。原因：`compute_finding_id` 故意排除 severity（models.py），且 Planner harvest 不去重，所以两个 `(inspector_name, version, message)` 相同但 severity 不同的 finding 会拿到**同一个真 id**。若以 id 为键，后者会覆盖前者 → 标签悬空。故 store 是 `label → 盖章 Finding`（label 唯一、保证一一对应单个 finding 对象），`resolve(label) → real_id`（多个 label 可解析到同一个 real id，允许且正确）。
- 编排层用 Planner findings（已盖 id、各分配唯一 label）seed `FindingStore`。
- `correlate_findings` handler 闭包持 `FindingStore`，把 `supporting_findings` 的 label resolve 成真 id 并校验 label 命中；不命中 → 结构化 error envelope，loop 喂回模型自纠（Failure Mode 1）。
- per-run 注入、非 module-global（不违反 §6）。
- **并发与同 turn 前向引用**：同一 turn 内 `asyncio.gather` 并行 dispatch。asyncio 单线程协程、handler 内 `FindingStore` 读写无 await 不交错，故**无数据竞争**，append/resolve 不需要锁。但存在**逻辑顺序前向引用**：模型可以在同一 turn 同时发出 `request_more_inspection`（append 新 label）和一个 `correlate_findings`（引用它自己臆造的、尚未由 tool_result 返回的新 label）；并行 dispatch 下 correlate 可能在 append 前 resolve → 该 label 悬空。**这是预期的可恢复行为**（不是 bug）：correlate 返回 error envelope，模型在下一轮（已从 request_more_inspection 的 tool_result 看到真 label）重新引用即可。`diagnostician.md` prompt 必须明确指导「先补查、看到返回的 label 后再于后续 turn 关联」。（修正：先前「同 turn 不会误判合法引用」的断言不成立 —— 模型可能自认为引用合法。）

### D-9：模型如何「看见」并稳健引用 finding —— 用序号标签而非 16-hex id

`Finding.id` 是 `sha256(...)[:16]`（16 位 hex）。让模型在 `supporting_findings` 里逐字符抄这串 hex 错误率高，抄错 → error envelope → 烧 turn，反复不收敛会撞 `max_turns` 降级、hypotheses 全丢。故：

1. **编排层组装诊断师首条 user message**时，给每个盖章 finding 编**短序号标签** `F1` / `F2` / …，并同时列出其内容（severity / message / inspector / 证据摘要）。`FindingStore` 记 `label → 盖章 Finding`（含真 id）。
2. `correlate_findings` 的 `supporting_findings` 接受这些**序号标签**（如 `["F1", "F3"]`），模型只需抄短标签（鲁棒）。handler 用 `FindingStore` resolve **仅为校验标签命中**（不命中 → 结构化 error envelope，Failure Mode 1）；真正把标签解析成真 `Finding.id` 在**编排层 harvest 时**完成（见 D-2 harvest 数据流）—— 最终 `RootCauseHypothesis.supporting_findings` 仍是真 id（持久化/渲染语义不变）。
3. **`request_more_inspection` 新取的 findings**：此工具是新工具，其 `output_schema` 可带 `status` + 带 id + 带新分配的序号标签（不受 `run_inspector` cassette 约束）。其 handler 是**新写的**（不能直接调 `run_inspector_handler`，因为后者返回剥了 id 的 `RunInspectorOutput`），但复刻 `run_inspector_handler` 的完整编排（不新造采集逻辑）：
   - (a) `ctx.inspector_registry.get(inspector_name)` 拿 manifest（`inspector_not_found` → 结构化 `ToolError`）；
   - (b) `ctx.target_registry.get(target_name)` 把**闭包固定的 target_name 字符串**解析成 `ExecutionTarget` 对象（`InspectorRunner.run` 收对象不是 name；unknown → 结构化 `ToolError`）；
   - (c) **clock 透传（可选）**：`register_diagnostician_tools` 接受可选 `clock`（镜像 `register_default_tools(clock=...)`）透传给 `InspectorRunner`。`--intent` 路径传 `None`（真 UTC，与该路径 `run_inspector` 一致；`build_planner` 现状无 clock）；frozen-clock 只在未来把 demo replay 装配接入 Diagnostician 时传入（保 `sampling_window` 补查在 ReplayTarget 下 byte-stable）。本提案 `--intent` 路径不依赖它 —— **前提**：本提案 `--intent` 路径补查的 inspector 必须 clock-free（无 `sampling_window`）；带 `sampling_window` 的补查只能在未来 frozen-clock replay 装配下保可重现（见 tasks 6.1）；
   - (d) `InspectorRunner(...).run(manifest, target, parameters=dict(args.parameters) if args.parameters else None, allow_privileged=False, cancel=ctx.cancel)` 拿 `InspectorResult`（`parameters` 透传与 `run_inspector_handler` 一致，勿漏）；
   - (e) **version 直接用 `InspectorResult.version`**（runner 已填 `version=manifest.version`，进程内未丢，**无需反查 registry** —— 反查只是 D-3 Planner 路径因 wire 剥离才用的退路）；
   - (f) `compute_finding_id(result.name, result.version, f.message)` 盖章 → 在 `FindingStore` 分配新唯一标签 → **append** → 返回带 `status`/id/标签 的 findings。
   新标签**对后续 turn** 的 `correlate_findings` 可引用；同 turn 前向引用按 D-8 处理（悬空→下轮自纠）。

- **替代（直接用 16-hex id 引用 + 把不收敛列为接受的降级失败模式）**：否决 —— 序号标签是廉价且显著更鲁棒的工程选择，符合「让 Agent 可靠」的展示诉求。

### D-10：Prompt caching

- `diagnostician.md` 系统提示 byte-stable（工具总览渲染方式与 planner.md 一致），命中断点 A（`tools + system` 静态前缀）。
- **findings 列表（动态，含序号标签）进 messages 首条 user，绝不进 system** —— 否则系统提示随 run 漂移、断点 A 永不命中（§4.8）。findings 落 messages 尾部，由 loop 既有滚动断点 B 覆盖。
- backend `capabilities.prompt_caching=False` 时 loop 既有逻辑不注入 `cache_control`（§4.8 由 loop 决定，沿用）。
- **可测边界**：CI 只能验证「system block 跨 run 字节一致」+「`_inject_cache_control` 注入位置正确」+「prompt_caching=False 时不注入」；真实 cache hit rate（`cache_read_input_tokens` 计数）依赖真打 Anthropic API，只能在 live 烟测验证，`FakeBackend`/`PlaybackBackend` 不产真 cache 计费（见 tasks 4.5）。

## 风险 / 权衡

- **[模型产悬空 finding 引用]** → `correlate_findings` handler 闭包校验序号标签 ⊆ `FindingStore`，不命中回结构化 error 让模型自纠（D-8/D-9 / Failure Mode 1）；序号标签设计降低抄写错误率。
- **[诊断师 loop 降级致 hypotheses 缺失/不全 + narrative 为空]** → 仍输出 Planner findings + 空假设占位 + （可能为空的）narrative，`status` 反映降级（D-5），退出码 2；不重试（重试收口在 loop）。
- **[诊断师调任何工具前 API 不可达]** → `failed_api_unavailable` 经 reconcile 映射 `degraded_no_planner`（D-5），Planner findings 不丢。
- **[两个 loop 翻倍 token / 配额]** → 诊断师工具集小、系统提示命中 cache_read；CI 全程 cassette replay 零真实配额。
- **[诊断师拿到采集能力后自由乱跑]** → 受限注册表（无 `list_targets`）+ target 固定注入（D-6）。
- **[补查 inspector 失败被 swallow]** → `request_more_inspection` 复用 `InspectorRunner` 直接拿 `InspectorResult.status` 并经 `output_schema` 暴露 `status`，模型可区分「跑了没发现」vs「失败」（D-9）。
- **[盖章时 inspector 已卸载]** → fail-loud → exit 2，不静默丢 finding（D-3）。
- **[`FindingStore` per-run 可变状态 / 同 turn 前向引用]** → 闭包 per-run 注入、非 module-global、asyncio 单线程无数据竞争；label 为唯一键避免 id 碰撞覆盖；同 turn 引用未返回 label → 悬空 → 下轮自纠（D-8/D-9）。
- **[同 turn 前向引用不收敛 → 烧 turn 撞 max_turns → hypotheses 全丢]** → 「下轮自纠」依赖模型真的改用已返回标签；若模型每轮都同 turn 臆造引用，会反复悬空直到 `degraded_max_turns`。缓解：(1) `diagnostician.md` **显式指令**「绝不在发出 `request_more_inspection` 的同一 turn 引用其结果标签，必须等下一轮看到 tool_result 的标签再 correlate」；(2) 这条**不收敛路径仍显式列为降级失败模式**（proposal Failure Mode 2 的子情形），承认其结局是 `degraded_max_turns` + exit 2，不藏在「可恢复」措辞下。
- **[新工具 cassette]** → 诊断 loop 需新录 cassette；`run_inspector` wire 不变故既有 cassette 全部保留（D-3）。

## Migration Plan

纯新增 + 一处 CLI 行为扩展，无数据迁移、无 schema 破坏：

- 新增模块（diagnostician.py / diagnostician.md / 三个 schema / diagnostician_tools.py / FindingStore）、新增诊断 loop cassette。
- 修改 `cli/_intent.py`（串接 Planner→id 盖章→Diagnostician + 渲染）与 `cli/inspect.py`（退出码改用 `DiagnosticianResult`）。
- 回滚：`--intent` 行为变更可整体回退到「直出 PlannerResult」；`--inspector` 路径自始不受影响。
- feature branch `feat/add-diagnostician-agent`，PR squash-merge（CLAUDE.md §5.1）。

## Open Questions

无未决项（此前两个 OQ 已定，记为决策）：

- **D-11（预算守卫）**：诊断师 loop 复用 `settings.agent` 的 budget 配置值，但 token/turn 计数独立于 Planner（各自 `run`、各自 `LoopUsage` 从 0 起）。
- **D-12（首条 user message 的 finding 展示密度）**：每个 finding 呈现 `label + severity + message + inspector + tags + evidence 条数`，evidence **正文不内联**（控 messages 尾部大小 / token 成本，使 Cost/Quota 估算成立）。证据正文截断阈值在实现阶段定（不影响契约）。
  - **已知 v1 限制（诚实承认，非疏漏）**：因 evidence 正文不内联，且 `correlate_findings` 不是 read 工具、`read_finding_detail` 被推迟，诊断师想深挖 Planner 已采集的**某条 finding** 的证据正文时，**唯一手段是 `request_more_inspection` 重跑整个 inspector**（贵且多一轮），无法「按 finding id 取已有证据详情」。这是 D-12 + 「correlate_findings 不兼做 read」两决策叠加的可用性折中，v1 接受；按 id 取证据详情留待 `read_finding_detail` 后续工具。
