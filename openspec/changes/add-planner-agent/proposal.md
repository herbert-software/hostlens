## 为什么

M2.2 已交付一个通用、不特判任何工具的手写 tool-use loop（`AgentLoop`，archived `add-agent-loop-skeleton`），M2.3 已交付双层 Capability Registry 与 Agent surface adapter（`ToolsAdapter` + `register_default_tools`，含 `run_inspector` / `list_inspectors` / `list_targets`）。但二者之间缺一个**装配者**：

- `AgentLoop` 是 intent-agnostic 的 —— 它接收一个 `system` 提示词和一个 `ToolsAdapter`，但**自己不构造系统提示词、不知道「巡检」语义、不组装报告**（见 `loop.py` 顶部注释：assembling a Report is the M2.4 Planner job）。
- `ToolsAdapter` 只负责 ToolSpec → Anthropic schema 投影与 dispatch，**不负责把工具清单写进系统提示词**。

目前没有任何代码把「自然语言意图」变成「一次配置好系统提示词 + 工具集 + backend 的 Agent run，并把跑完的 `LoopResult` 收敛成可消费产物」。这正是 Hostlens 简历价值的入口（CLAUDE.md §4.1 / §4.2「Agent 是调度者」）。M2.7 的 `hostlens inspect --intent` CLI、M3 的 Diagnostician 都要消费这个装配者。现在做，是因为 loop 与 registry 这两块下游依赖刚刚到位、契约稳定，正好补上中间这一层。

## 变更内容

新增 **Planner Agent**（`agent/planner.py` + `agent/prompts/planner.md`），它是 `AgentLoop` 的唯一「巡检语义」装配者：

1. **系统提示词装配**：从 `agent/prompts/planner.md` 模板加载 Planner 的角色与调度纪律（先 `list_targets` / `list_inspectors` 发现能力，再 `run_inspector`；不臆造工具；只读巡检；最后用自然语言综述），渲染成一段**跨 run 稳定**的系统提示词（CLAUDE.md §4.8 prompt caching 的前提）。提示词文件外置，不内联（CLAUDE.md §7 反模式）。
2. **依赖装配**：接收已注入默认工具的 `ToolRegistry`（或 `ToolsAdapter`）、`LLMBackend`、`Settings`，构造 `AgentLoop(backend, adapter, settings, system=<装配好的提示词>)`。系统提示词必须以**单元素 text block 列表** `[{"type": "text", "text": <rendered>}]` 形态传入（裸 `str` 会让 loop 的 `_inject_cache_control` 跳过 cache_control 注入、prompt caching 静默失效，见 design D-2）。Backend 仍是 `AgentLoop` 私有依赖，**不进 `ToolContext`**（ADR-008）。
3. **运行与收敛**：调用 `AgentLoop.run(intent)` 拿到 `LoopResult`，把它收敛成一个新的 `PlannerResult`：
   - `narrative`：LLM 最终综述文本（`LoopResult.final_text`）—— M2 的「markdown 报告」正文。
   - `findings`：从 `LoopResult.tool_invocations` 里所有成功的 `run_inspector` 输出收集到的结构化 `Finding` 列表（`run_inspector` 的 `findings` 字段即 `reporting.models.Finding`，无损）。
   - `loop_result`：原始 `LoopResult`（保留 `terminal_status` / `usage_totals` / `turns` / `stop_reason` 供 CLI 与可观测消费）。

**不**修改 `AgentLoop` 行为，**不**新增工具，**不**修改 `ToolRegistry` / `ToolsAdapter` / `Finding` 模型 —— Planner 是纯装配 + 收敛层，建立在已稳定的契约上。

## 功能 (Capabilities)

### 新增功能
- `planner-agent`: Planner Agent —— 把自然语言意图装配成一次配置好系统提示词、工具集、backend 的 `AgentLoop` run，并把 `LoopResult` 收敛成 `PlannerResult`（narrative + 结构化 findings + loop 遥测）。涵盖：系统提示词模板加载与稳定渲染、依赖注入装配、运行编排、结果收敛、`run_inspector` 输出到 `Finding` 的收集语义、terminal_status 透传。

### 修改功能
<!-- 无。Planner 消费 agent-loop / tool-registry-capability-layer / agent-tool-adapter / report-data-model（Finding）的现有契约，不改变任何已发布的 spec 级行为。 -->
（无）

## 影响

- **新增代码**：`src/hostlens/agent/planner.py`、`src/hostlens/agent/prompts/planner.md`、`src/hostlens/agent/prompts/__init__.py`（如需 importlib.resources 包定位）。
- **新增测试**：`tests/agent/test_planner.py`（用 `FakeBackend` 单测装配与收敛逻辑；用 `PlaybackBackend` + cassette 做一次端到端回放，验证 intent → 选 inspector → 收集 finding → narrative 的稳定性）。
- **对外契约影响**：
  - **Agent tool schema**：无变更（复用 M2.3 三个工具）。
  - **CLI 命令**：无变更（`--intent` CLI 是 M2.7，本提案只交付被它消费的 `PlannerAgent`）。
  - **MCP tool schema / Notifier Protocol / Schedule manifest / Inspector schema**：均无变更。
  - 新增 Python 公共接口 `PlannerAgent` / `PlannerResult`（M2.7 与 M3 消费）。
- **依赖**：不引入新第三方依赖（提示词加载走标准库 `importlib.resources`；渲染用 f-string / `str.replace`，不引入 Jinja2 到 Agent 层）。
- **配置**：复用现有 `agent:` namespace（`primary_model` / `max_turns` / `token_budget_*` 等），不新增配置字段。

## 非目标 (Non-Goals)

- **不做结构化 `Report` 对象装配**：M2.4 只产出 `narrative`（LLM 文本）+ 收集到的 `Finding` 列表。把 `findings` + InspectorResult 级元数据（status / 计时 / result 级 evidence）组装成完整 `reporting.models.Report` 对象、并做去重/排序/根因关联，是 **M3 Diagnostician + 报告体系**的职责（CLAUDE.md §4.2：Planner 是调度者，Diagnostician 才做关联）。理由：`run_inspector` 工具输出刻意只回传 `FindingSummary`（= `Finding`，无 InspectorResult 级字段）以压缩 LLM 上下文，强行从有损工具输出反推完整 `Report` 会污染架构。
- **不做 Diagnostician / 跨信号关联 / 根因假设**（M3）。
- **不做 CLI `--intent` 与 Rich 流式输出**（M2.7）。
- **不做 prompt cache 命中率断言测试**（M2.5 专项；本提案只保证系统提示词跨 run 稳定、可被 loop 的 `_inject_cache_control` 缓存）。
- **不做 MCP 投影**（M7）。
- **不新增工具 / 不暴露 `exec_arbitrary_command`**（能力面由 M2.3 的 `surfaces` 与 Inspector 限制，Planner 不绕过 registry）。
- **不修改 `AgentLoop` 的 6 个 stop_reason 推进、重试、token 预算逻辑**。

## Failure Modes

1. **Backend 持续不可用 / 限流耗尽**：`AgentLoop` 自身按 ARCHITECTURE §9 重试 3 次后返回 `failed_api_unavailable` 或 `degraded_rate_limited` 的 `LoopResult`。Planner **不**额外重试（ADR-005：loop 是唯一重试收口），直接把该 terminal_status 透传进 `PlannerResult`，`narrative` 为空、`findings` 为已收集到的部分结果（可能为空）。调用方（M2.7 CLI）据 terminal_status 决定退出码。
2. **Agent 跑满 max_turns / token 预算仍未 end_turn**：loop 返回 `degraded_max_turns` / `degraded_token_budget`。Planner 透传 `findings`（已收集部分）与 `narrative = loop_result.final_text`（逐字，不截断不补救）。`narrative` 取值（对照 `loop.py` 实证，见 design D-3）：`degraded_max_turns` 与 token 预算**预检守卫**路径 `final_text==""` → narrative 为空；但 `stop_reason==max_tokens`（也归类 `degraded_token_budget`）会带模型**部分输出** → narrative 可非空。故 narrative 是否为空**不能**由 terminal_status 唯一推定。降级而非报错。
3. **Agent 一个 inspector 都没调就 end_turn**（如意图与任何 inspector 无关，或模型误判）：`findings` 为空列表，`narrative` 为 LLM 文本。Planner 不视为错误（这是合法的「无需巡检」结论），terminal_status=`ok`。
4. **`run_inspector` 工具调用返回 error envelope**（inspector skip / 超时 / 校验失败）：该次 invocation 的 `error` 字段非空、`output` 为空，Planner 收集 findings 时**跳过**它（不计入 `findings`），但保留在 `loop_result.tool_invocations` 里供调试。不中断整个 run。
5. **提示词模板文件缺失 / 加载失败**：构造 `PlannerAgent` 时 fail-loud（raise `ConfigError`，kind=`planner_prompt_missing`），不静默用空提示词（空提示词会让 Agent 行为不可控）。这是构造期错误，不是 run 期降级。

## Operational Limits

- **并发预算**：Planner 不引入新并发；Agent 的并行 `tool_use` 仍由 `AgentLoop` + Inspector Runner 的两级 `asyncio.Semaphore` 约束（OPERABILITY §1，Agent 不能绕过并发门）。
- **Token 预算**：复用 `agent.token_budget_input`（默认 100K）/ `token_budget_output`（默认 30K）/ `max_turns`（默认 20），由 `AgentLoop` 逐轮收缩 `max_tokens` 强制执行。Planner 不放宽这些上限。
- **内存预算**：`PlannerResult` 持有全部 `tool_invocations`（含每个 `run_inspector` 的完整 findings + evidence）。单次巡检 finding 量级在数十~数百条；不做流式落盘（持久化是 M4）。
- **超时**：单次 `messages_create` 60s（loop 内 `_MESSAGES_CREATE_TIMEOUT`），单个 `run_inspector` 工具 30s（ToolSpec.timeout），均已由下游强制，Planner 不新增超时层。

## Security & Secrets

- **不引入新密钥**：复用 `ANTHROPIC_API_KEY`（backend 持有）。Planner 不读取任何凭据。
- **脱敏**：工具 error envelope 在 `ToolsAdapter.dispatch` 已过 `scrub_exception_message`；Planner 收集 findings 时不二次拼接异常文本，不引入新泄露面。`run_inspector` 标记 `sensitive_output=True`（输出可能含进程/端口/连接元数据）—— Planner 把这些 findings 原样放进 `PlannerResult.findings`，**不**写日志正文，渲染/脱敏边界由下游 reporting 层（`redact_report_for_render`）负责。
- **攻击面**：不扩大。Planner 不暴露新工具、不新增网络入口、不接受除 intent 字符串外的外部输入；intent 仅作为 user message 传给模型，不进入任何 shell/命令渲染路径。
- **能力面**：Planner 严格通过 `ToolsAdapter`（`surfaces ∋ "agent"`）拿工具，不直接 import Inspector registry、不绕过 registry 直调 handler（CLAUDE.md §4.10 硬规则 3）。

## Cost / Quota Impact

- **每次 run 的调用频次**：典型一次健康巡检 = 1 次发现轮（list_targets/list_inspectors）+ N 次 inspector 调用轮 + 1 次综述轮 ≈ 3–6 次 `messages_create`（取决于模型一轮并行调几个工具），上限由 `max_turns=20` 兜底。
- **Token 消耗**：受 `token_budget_input=100K` / `token_budget_output=30K` 硬约束，单次 run 烧钱上限确定。
- **Prompt caching**：系统提示词以 list[text block] 形态传入（裸 str 会被 `_inject_cache_control` 跳过）+ 工具 schema 跨 run 稳定 → loop 在 `backend.capabilities.prompt_caching=True` 时给末块注入 `cache_control`，多轮内 `cache_read_input_tokens` 复用系统块，显著降低重复 input token 成本（命中率断言留给 M2.5）。
- **CI / 测试成本**：单测走 `FakeBackend`（零 API），端到端走 `PlaybackBackend` + cassette（零 API），CI 默认 replay 不消耗 Anthropic 配额。

## Demo Path

无需 SSH、无需付费 API（cassette replay）：

```bash
pip install -e ".[dev]"
# 单测：FakeBackend 驱动 Planner，验证装配 + 收敛
pytest tests/agent/test_planner.py -m "not live" -q
# 端到端回放：PlaybackBackend + 预录 cassette，验证 intent → 选 inspector → 收集 finding → narrative 稳定
pytest tests/agent/test_planner.py -k playback -q
```

期望：`PlannerResult.findings` 非空（来自回放里 Agent 调用的 `run_inspector`），`PlannerResult.narrative` 含 LLM 综述文本，`PlannerResult.loop_result.terminal_status == "ok"`，重复跑结果稳定（cassette 决定性回放）。M2.7 落地后将升级为 `hostlens inspect <target> --intent "检查这台机器的健康状况"` 的 CLI demo。
