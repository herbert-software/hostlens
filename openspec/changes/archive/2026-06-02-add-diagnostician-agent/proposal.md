## 为什么

Planner Agent 现在能把自然语言意图拆解成一组只读巡检、并把原始 `Finding` 综述给用户，但**到此为止**：`hostlens inspect --intent` 输出的 `PlannerResult` 只是「一堆扁平 finding + 一段叙事」，没有跨信号关联，也没有「为什么会这样」的根因判断。这正是 Hostlens 区别于 Zabbix/Prometheus「规则匹配 + 告警」的核心卖点（CLAUDE.md §1「理解意图 + 推理诊断」）所缺的最后一环。

`Report.hypotheses: list[RootCauseHypothesis]` 的形状早在 `add-report-persistence-and-diff` 就已定义，但永远是 `[]` —— 没有任何代码产出内容。同样，`ReportStatus` 的 5 个 `degraded_*` / `empty_response` 值已定义但无产出方。M3 退出条件要求「报告中能看到 📌 根因假设章节（内容由 3.1 Diagnostician 填充）」。本提案就是 3.1：引入 **Diagnostician Agent**，消费 Planner 产出的 findings，做跨信号关联，产出带证据链接的根因假设。

## 变更内容

- **新增 `DiagnosticianAgent`**（`agent/diagnostician.py` + `agent/prompts/diagnostician.md`）：第二个手写 tool-use Agent（复用既有 `AgentLoop`），输入 = 带稳定 `id` 的 findings 列表 + intent，输出 = 根因假设 + 诊断叙事。
- **新增 `correlate_findings` ToolSpec —— 纯结构化输出通道**：input_schema 即 `RootCauseHypothesis` 的形状，`supporting_findings` 用首条 user message 里呈现的**序号标签**（`F1`/`F2`…）引用 finding，而非逐字符抄 16-hex `id`（降低抄写不收敛风险）；handler 只用 finding-store resolve 标签**做命中校验**（不做任何关联推理，§4.2；真 id 无处写回 `ToolInvocation`，见 design D-2）。诊断师每产一条假设调用一次；**编排层在 harvest 时**从 `tool_invocations[*].input` 读字段、用同一 finding-store 把标签 resolve 成真 id，组装 `list[RootCauseHypothesis]`（其 `supporting_findings` 为 harvest 时 resolve 的真 id），与 Planner 从 `run_inspector` harvest findings 完全对称（绕开 loop「只还 `final_text`」、且不违反 §4.7「禁止 json.loads 解析模型输出」）。
- **新增 `request_more_inspection` ToolSpec —— 复用 `InspectorRunner` 采集执行引擎**：当诊断揭示证据缺口时，诊断师可补查一个 inspector。它**不是**新造的采集能力，handler 复用 `run_inspector_handler` 内部同款的 `InspectorRunner`；因是新工具（不受 `run_inspector` cassette 约束），其 `output_schema` 暴露 inspector 的 `status`（使模型能区分「跑了没发现」与「失败被吞」）+ 带 id + 序号标签的 findings。target 由编排层从 CLI 的 `<target>` 参数固定注入（诊断师不重新暴露 `list_targets` 发现能力，§7 最小能力）。
- **新增装配层 finding-id 盖章**：编排层（intent 路径）从 `loop_result.tool_invocations` 按 `(inspector_name → findings)` 重新分组，用 `InspectorRegistry` 按 name 反查 `manifest.version`，调 `compute_finding_id` 给每个 finding 盖上稳定 id —— 这是 hypotheses 能用 `supporting_findings` 锚定 finding 的前提。**不改 wire、不改 `ToolContext`**。
- **新增 `terminal_status → ReportStatus` 映射**：编排层把 Planner loop 与 Diagnostician loop 的 `terminal_status` reconcile 成单一 `ReportStatus`（消费此前无产出方的 `degraded_*` 值），写进新的 `DiagnosticianResult`。
- **新增 `DiagnosticianResult` 输出模型 + `register_diagnostician_tools` 装配函数**。
- **修改 `hostlens inspect --intent`**：在 Planner 之后串接 Diagnostician，md 渲染新增「## 根因假设」章节，输出 `DiagnosticianResult`（PlannerResult 的超集）而非裸 `PlannerResult`；退出码改由 `DiagnosticianResult` 映射。

## 功能 (Capabilities)

### 新增功能
- `diagnostician-agent`: Diagnostician Agent 的行为契约 —— `DiagnosticianAgent` 类（复用 `AgentLoop` + `diagnostician.md`）、`correlate_findings` / `request_more_inspection` 两个 ToolSpec 及其策略元数据、`register_diagnostician_tools` 装配、装配层 finding-id 盖章规则、两个 loop 的 `terminal_status → ReportStatus` reconcile 规则、`DiagnosticianResult` 输出模型与 prompt caching 策略。

### 修改功能
- `inspect-cli-command`: `--intent` 路径从「Planner 产出 PlannerResult 直出」改为「Planner → 装配层 id 盖章 → Diagnostician → 产出 DiagnosticianResult」。涉及三处既有需求的修改：(1)「必须输出 narrative + findings 摘要 + 遥测」扩展为额外渲染「根因假设」章节、输出 DiagnosticianResult；(2) md/json 渲染契约更新；(3) 退出码由 DiagnosticianResult（含 reconcile 后的 status）映射。**`--inspector` 机械路径完全不变。**

## 影响

- **新增代码**：`src/hostlens/agent/diagnostician.py`、`src/hostlens/agent/prompts/diagnostician.md`、`src/hostlens/tools/schemas/correlate_findings.py`、`src/hostlens/tools/schemas/request_more_inspection.py`、`src/hostlens/tools/diagnostician_tools.py`（含 `register_diagnostician_tools`）。
- **修改代码**：`src/hostlens/cli/_intent.py`（编排 Planner→Diagnostician + id 盖章 + **新增** `render_diagnostician_result`）、`src/hostlens/cli/inspect.py`（`--intent` 退出码改用**新增** `_compute_diag_exit_code(DiagnosticianResult)`）。**关键**：既有 `render_planner_result` / `_compute_intent_exit_code(PlannerResult)` 被 `cli/demo.py` 共享，**不改其签名**（否则破坏 `demo run`）—— `--intent` 路径用新增的 DiagnosticianResult 版函数，PlannerResult 版保留给 demo。
- **零对外契约破坏**：不改 `RunInspectorOutput`（保 cassette hash 不变）、不改 `ToolContext` 6 字段（ADR-008）、不改 `Report` 模型（`RootCauseHypothesis` 复用既有形状）。`correlate_findings` / `request_more_inspection` 仅 `surfaces={"agent"}`，不上 MCP（§4.10 rule 1）。
- **依赖**：无新增第三方依赖。
- **复用既有契约**：`RootCauseHypothesis`（report-data-model）、`ReportStatus`（report-data-model）、`compute_finding_id`（report-data-model）、`AgentLoop` / `LoopResult`（agent-loop）、`ToolsAdapter`（agent-tool-adapter）、`run_inspector_handler`（tool-registry-capability-layer）、`InspectorRegistry`（inspector-plugin-system）。

## 非目标 (Non-Goals)

- **不**让 `--intent` 路径产出**忠实的一等 `Report`**，因此**不**解锁 `--intent --persist` / diff。理由：忠实 `ReportMeta` 需要 `InspectorResult` 的 `status` / `duration_seconds` / `version`，而这些已在 `run_inspector_handler` 投影成 `RunInspectorOutput` 时丢失（非 ok 状态被静默吞成 `findings=[]`）；强行组装会迫使在 meta 里造假（status 报 ok 但可能是被吞掉的 timeout、duration=0）。这条边界已在 `report-persistence` spec 明确标注。「忠实 ReportMeta + out-of-band `InspectorResult` propagation + `--intent --persist` 解锁」拆为**后续提案（尚未 propose，无排期背书）**，本提案零 wire / 零 ToolContext 改动。
- **明确缺口（诚实披露）**：因上一条，本提案**不**满足 M3 退出条件中「**持久化报告**中能看到根因假设章节」的部分 —— hypotheses 只活在 `--intent` 的 stdout `DiagnosticianResult`，**不入库**；`Report.hypotheses` 仍为 `[]`，`hostlens reports diff` 不覆盖 hypotheses。本提案只满足「`--intent` stdout 渲染根因假设章节（含证据链接）」。持久化 + diff 覆盖 hypotheses 由上述后续提案交付。`--inspector` 路径的 Report 仍按既有占位渲染（空假设）。
- **不**实现 Remediation / 任何写操作 —— 诊断师只读，`correlate_findings` 是 `side_effects="none"` 的输出通道，`request_more_inspection` 是 `side_effects="read"`。
- **不**改 `correlate_findings` 为「按 id 取 finding 完整证据」的 read 工具（M2 推迟的 `read_finding_detail` 仍是独立的未来工具，不在此揉入）。
- **不**引入 extended-thinking / 推理模型支持（独立提案 `support-extended-thinking`，见 TODO 3.6）。
- **不**改 Planner Agent 行为（`PlannerResult` 形状、planner.md、harvest 逻辑均不变）。
- **不**上 MCP surface（M7）。
- **`hostlens demo run` CLI 暂不接 Diagnostician**（仍产 `PlannerResult`、行为不变）：理由是 `demo run` 经 `PlaybackBackend` 回放 8 份按 Planner-only 录制的 cassette，接 Diagnostician 需重录全部 8 份 cassette + 改 `demo-cli-command` 契约 + 改 `demo/assembly.py`/`cli/demo.py`，与「架构清晰度 > 功能广度」不成比例。本提案**不改** `demo-cli-command` 契约、不重录 demo cassette。诊断师的 offline 验证走**测试级**：monkeypatch `cli._intent.create_backend` 注入 `PlaybackBackend`（沿用 `tests/cli/test_inspect_intent.py` 既有 pattern）over 新录的单场景诊断 cassette。把 `demo run` 升级到 Diagnostician 留待后续提案（可与「忠实 Report」提案合并）。

## 对外契约影响

| 契约面 | 影响 |
|---|---|
| Agent tool schema | **新增** `correlate_findings`（input=带序号标签的假设形状）/ `request_more_inspection`（output 含 `status` + 带 id/标签 findings）（均仅 agent surface）；**不改** 既有 `run_inspector` / `list_inspectors` / `list_targets` 的 schema 与 wire 投影 |
| `RunInspectorOutput` wire | **不变**（field_serializer 仍剥 id/inspector_name/inspector_version，所有 incident/demo/planner cassette 不重录）|
| `ToolContext` | **不变**（仍 6 字段，ADR-008）|
| `Report` / `RootCauseHypothesis` / `ReportStatus` 模型 | **不变**（复用既有形状，本提案只是首个产出 hypotheses 内容 + degraded_* 值的消费方）|
| CLI 命令 | `hostlens inspect --intent` 行为变更（见修改功能）；`--inspector` 路径不变；无新增子命令 |
| MCP tool schema | 不变（不上 MCP）|

## Agent 行为变更：Prompt Caching 策略与 Token 影响

- 诊断师是**第二个** `AgentLoop`，有自己 byte-stable 的系统提示 `diagnostician.md`（含工具总览，渲染方式与 planner.md 一致），命中断点 A（静态前缀 `tools + system`）。系统提示禁止内插任何 per-run 动态内容。
- **动态输入（findings 列表）必须进 messages 首条 user message，绝不进 system** —— 否则系统提示随 run 漂移，断点 A 永不命中（§4.8）。findings JSON 落在 messages 尾部，由 loop 既有的滚动断点 B 覆盖。
- 诊断师工具集小（`correlate_findings` + `request_more_inspection` + 复用的 `list_inspectors`，见 design D-6），其 tool schema 与系统提示同属断点 A 静态前缀。
- token 影响：诊断阶段额外 ~1 个 loop（多数意图 1–3 turn），输入主要是 findings JSON 的重述。详见 Cost / Quota Impact。

## Failure Modes

1. **诊断师引用了不存在的 finding 标签**：`correlate_findings` handler 把 `supporting_findings` 的序号标签经 `FindingStore` resolve 为真 id 并校验标签命中，不命中（含同 turn 前向引用尚未返回的标签）→ 返回结构化 error envelope（`is_error`），loop 喂回让模型在下一轮自纠（与既有 hallucinated-tool 路径一致），不 crash、不静默接受悬空引用。
2. **诊断师 loop 被 rate limit / token budget / max turns 降级**：Planner 已成功收集到 findings，但 hypotheses 缺失/不全。降级行为：仍输出已收集的 findings + 空假设占位 +（**可能为空的**）narrative（多数降级路径 loop 不传 `final_text` → narrative 为 `""`，但 `max_tokens` 停止路径可能携带 —— md 渲染必须**容忍空且不假设非空**），`DiagnosticianResult.status` 反映降级（reconcile 规则见 design），hypotheses 为已 harvest 的部分。CLI 退出码 = 2，不重试（收口在 loop）。**子情形（前向引用不收敛）**：若模型反复在发出 `request_more_inspection` 的同一 turn 引用其尚未返回的新标签，每次 `correlate_findings` 悬空 → error envelope → 烧 turn，最终 `degraded_max_turns` + hypotheses 全丢。`diagnostician.md` 显式指令「补查后下一轮再 correlate」缓解；此降级路径显式承认（结局 exit 2），不藏在「可恢复」措辞下。
3. **诊断师不调用任何 `correlate_findings`（end_turn 带文本）**：合法 —— 意图本身无需根因假设（如「列一下有哪些 target」）。`hypotheses=[]`，`status=ok`，md 渲染「_暂无根因假设_」占位（与 `--inspector` 路径 Report 的空假设渲染一致），退出码按 finding severity 正常映射。（区别于诊断师空响应 → `empty_response` → 退出码 2。）
4. **Planner 阶段就降级/失败**：Planner `degraded_*` / `empty_response` 时诊断阶段**跳过**（理由是采集不完整、v1 不在残缺证据上做根因推理；**Planner 已 harvest 的 findings 仍输出**，可能非空），`DiagnosticianResult.status` 取 Planner 的降级值；`failed_api_unavailable` 时不产任何 `DiagnosticianResult`（无 findings，走 CLI no-result 路径 exit 2，归 M4 RunStatus 边界）。
5. **`request_more_inspection` 补查的 inspector / target 解析失败或采集非 ok**：handler（新写、复刻 `run_inspector_handler` 编排）unknown inspector / unknown target → 结构化 `ToolError` 回传；inspector 采集非 ok（timeout/target_unreachable/...）→ `output.status` 如实暴露该状态 + `findings=[]`，诊断师据 `status` 区分「失败」与「无发现」继续推理，不 crash。

## Operational Limits

- **并发预算**：诊断师 loop 串行于 Planner loop 之后（非并行），不增加峰值并发；`request_more_inspection` 触发的 inspector 采集复用 `InspectorRunner` 既有并发预算（docs/OPERABILITY.md §1），不放大。
- **超时**：`correlate_findings` 是纯内存校验，`timeout` 设小值（如 5s）；`request_more_inspection` 复用 `run_inspector` 的 30s ToolSpec timeout。诊断师 loop 的 `messages_create` 复用既有 60s per-call timeout 与 `max_turns` / token budget 守卫。
- **内存预算**：findings JSON 在 Planner→Diagnostician 间重述一次（已脱敏的 `Finding` 对象），无额外大对象驻留。

## Security & Secrets

- **不引入任何新密钥**（复用 `ANTHROPIC_API_KEY` 经既有 backend）。
- **不扩大攻击面**：`correlate_findings` 不接触 target、不执行命令、不读文件，纯内存校验 + 记录；`request_more_inspection` 是 `run_inspector` 的受限再暴露（target 固定、`allow_privileged=False` 沿用 agent surface 铁律、不暴露 `list_targets` 发现）。诊断师工具均 `surfaces={"agent"}`，不上 MCP。
- **脱敏**：诊断师消费的 findings 已是经 `run_inspector` 输出的脱敏对象；hypotheses 文本由模型基于这些脱敏 finding 生成，不引入新的敏感数据路径。`correlate_findings` 的 `sensitive_output=False`（只回 ack），`request_more_inspection` 的 `sensitive_output=True`（同 `run_inspector`）。

## Cost / Quota Impact

- **每次 `--intent` 运行新增 ~1 个 LLM loop**（诊断阶段）。典型意图：诊断 loop 1–3 turn。输入 token 主要是 findings JSON 重述（受 Planner 收集规模影响，通常数 KB）+ byte-stable 系统提示（命中 cache_read，近乎免费）。
- 估算：在 8 场景 Incident Pack 规模下，诊断阶段增量 ≈ Planner 阶段输出 token 的同量级，输入侧因 prompt caching 命中而显著低于全价。
- 对 Anthropic 配额：每次 `--intent` 的 `messages_create` 调用数从「Planner N 次」变为「Planner N + Diagnostician M 次」。CI 全程走 cassette replay，零真实配额消耗。

## Demo Path

5 分钟、无 SSH / 无付费 API reproduce。注意 `hostlens inspect --intent` 用 `create_backend(settings)` 走**真** backend（会打付费 API），且 `hostlens demo run` 本提案**不接** Diagnostician（仍 Planner-only，见非目标）。故 Diagnostician 的 offline repro 走**测试级 cassette replay**（与既有 `tests/cli/test_inspect_intent.py` 同 pattern）：

```bash
# 测试级：CliRunner 跑 `inspect --intent`，monkeypatch create_backend 注入回放
# authored cassette 的 backend（沿用 tests/incidents/_generate.py 的 RecordingBackend
# 包脚本化 FakeBackend 录制机制 —— 零 key、确定性、可重现），真 local-host target，全程离线
pytest tests/cli/test_inspect_intent_diagnostician.py -q
```

该测试断言 stdout（md）含：诊断叙事 + `## Findings` 摘要 + **新增的 `## 根因假设` 章节**（每条含 description / confidence / 关联的 finding 证据链接 / suggested_actions）+ 一行遥测；stderr 含 Planner 与 Diagnostician 两段逐轮进度；退出码按 finding severity + reconcile status 映射（healthy=0 / critical=1 / degraded=2 / 用法错误=3）。cassette 用 authored FakeBackend 录制（**无真 key**，与既有 8 份 incident cassette 同纪律），故无付费 API。

**Live repro**（有真 key）：`hostlens inspect <target> --intent "为什么响应变慢" --format md` 直接产出带根因假设的报告。
