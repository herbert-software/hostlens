## 为什么

Hostlens 的简历核心价值是「面试官打开 `agent/loop.py` 能直接看到 Agent 怎么工作」（CLAUDE.md §4.1）。M2 前置件已全部就位 —— `LLMBackend` Protocol + 三个 backend（`add-llm-backend-protocol` 已归档）、双层 Tool Registry + agent-surface `ToolsAdapter`（`add-tool-registry-capability-layer` 已归档）—— 但把它们串起来跑通「自然语言意图 → 多轮 tool-use → 结构化结果」的那个**手写循环本身还不存在**（`src/hostlens/agent/loop.py` 缺失）。

本变更交付 M2.2：一个**自己手写、不依赖 LangChain 的** Anthropic tool-use 循环骨架。它是 M2.4 Planner Agent、M2.7 CLI `--intent`、M3 Diagnostician 的共同地基。

## 变更内容

新增 `AgentLoop` 类（`src/hostlens/agent/loop.py`），消费已落地的 `LLMBackend` 与 `ToolsAdapter`，实现以下机制：

- **多轮 tool-use 循环**：`while` 循环按 `response.stop_reason` 推进（`end_turn` 终止 / `tool_use` 续跑），同一 turn 内的多个 `tool_use` block 通过 `asyncio.gather` **并行 dispatch**。
- **capability-gated prompt caching**：调 backend 前根据 `backend.capabilities.prompt_caching` 决定是否在 system block 注入 `cache_control: ephemeral`（loop 端检查 capability，**不是** backend 端静默丢，CLAUDE.md §4.8 / §4.11 规则 2）。
- **安全网（兜底，非业务逻辑）**：单次 run 的 input/output token 预算上限（从 `settings.agent.token_budget_*` 读）+ 最大 turn 数兜底（`settings.agent.max_turns`，默认 20）。耗尽时**强制收尾**而非抛裸异常。
- **Failure Semantics 处理（按真实异常分类层，非 HTTP 状态层）**：backend 已把 HTTP 状态收敛为更小的异常集（`anthropic_api.py` 实证：429+529 → `BackendRateLimited`，仅靠 `retry_after_seconds` 是否为 `None` 区分；5xx+连接超时 → `BackendUnavailable`；401/403 → `BackendError(kind="auth_invalid")`）。循环按**异常类**而非 HTTP 状态决策：`BackendRateLimited`（honor `retry_after_seconds`，超限强制收尾）/ `BackendUnavailable`（退避后按有无结果降级或失败）/ `BackendError(kind=不可重试)` 与 `BackendCapabilityViolation`（不重试，向上抛）。
- **工具结果处理（按 `ToolsAdapter.dispatch` 真实契约）**：`dispatch(name, args_json, ctx) -> dict[str, Any]` —— 成功返回 `result.model_dump()` dict；handler 内部异常**已被 dispatch 捕获并 scrub**成 `{"is_error": True, "error_kind", "tool_name", "message", "cause"}` envelope dict 返回（循环**不再二次 scrub**）。循环按下列规则分流（详见 design D-5）：
  - **幻觉工具名用前置成员检查拦截，不靠 `KeyError`**：`dispatch` 的 `KeyError` 既可能是 registry 查无此名、也可能是 handler 内部 re-raise，同类型不可区分；故循环先查 `block.name ∈ list_for_agent() 名字集`，不在 → 不调 dispatch 直接回灌「无此工具」让模型改用真实工具。
  - **error envelope 用多键签名判别**：`is_error is True` 且含 `error_kind`、`message` 键 → 映射 Anthropic `tool_result(is_error=True)` + 记 `ToolInvocation(error=...)`（**禁止**仅凭裸 `is_error` 真值判别，业务 output schema 理论上可含同名字段）；否则正常 `tool_result`。
  - **会从 `dispatch` 抛出**的：`TypeError`（malformed tool args，循环捕获 + scrub 回灌让模型自纠）；`KeyError`（此时 name 已确认注册 → handler 内部 bug，**向上抛** fail-loud 不掩盖）；`ToolPolicyViolation`（loop 广告了一个自己拒绝 dispatch 的工具 = 配置/loop bug —— 向上抛暴露）；`ToolError`（output-schema 校验失败 = handler 返回类型错误的代码 bug —— 向上抛 fail-loud）。
  - **并行中任一工具 fail-loud 时取消其余未完成的 sibling**：同 turn 多个 tool_use 并行执行，若其中之一抛出 fail-loud 异常（`KeyError`/`ToolPolicyViolation`/`ToolError`/`CancelledError`），循环必须取消并 drain 其余仍在跑的 sibling task 后再向上抛原异常，避免 orphaned 长跑 handler 泄漏资源（`asyncio.gather` 默认不取消 sibling）。
- **统一退出状态**：`run()` 返回一个新的 `LoopResult` 数据模型（含 final assistant 文本、累积的 tool 调用与输出、token usage 汇总、turn 数、`terminal_status`）。

`terminal_status` 取一个 M2 范围的字符串闭集（`ok` / `degraded_rate_limited` / `degraded_token_budget` / `degraded_max_turns` / `degraded_no_planner` / `empty_response` / `failed_api_unavailable`）。这是 M3 `ReportStatus` enum 的前身；M2 先用字符串承载，M3 `add-report-persistence-and-diff` 再升级为 typed enum。

## 功能 (Capabilities)

### 新增功能
- `agent-loop`: 手写 Anthropic tool-use 循环 —— `AgentLoop` 类（构造契约、backend 私有持有、依赖注入边界）、turn 循环与并行 tool dispatch 语义、capability-gated `cache_control` 注入规则、token 预算 / max-turns 兜底、§9 Failure Semantics 到 `terminal_status` 的映射、`LoopResult` 输出 schema。

### 修改功能
- `agent-tool-adapter`: `ToolsAdapter.dispatch` 的 **output-schema 校验失败**（handler 返回非 `output_schema` 类型 = handler/adapter 代码 bug）从 raise `TypeError` 改为 raise `ToolError`。理由：input-schema 失败（malformed model args，可回灌让模型自纠）已 spec-locked 为 `TypeError`，而 output-contract 失败语义完全不同（代码 bug，应 fail-loud）；二者共用 `TypeError` 会让 Agent loop 无法区分，导致把代码 bug 误当可恢复的模型错误回灌。output-contract 失败异常此前未被 spec 约定，本变更补充约定为 `ToolError`。

## 影响

- **新增源文件**：`src/hostlens/agent/loop.py`（`AgentLoop` + `LoopResult` + 循环内部 helper）。
- **新增异常**：`core/exceptions.py` 增 `UnexpectedStopReason`（`HostlensError` 子类，循环遇到非预期 `stop_reason` 时 raise；ARCHITECTURE §9 引用了它）。
- **构造期约束**：`Settings.agent` 类型为 `AgentSettings | None`（`core/config.py` 实证默认 `None`）。`AgentLoop` 依赖 `settings.agent.{primary_model, max_turns, token_budget_input, token_budget_output}`，故构造时若 `settings.agent is None` 必须 raise `ConfigError`（不静默默认，让缺配置立即可见）。
- **消费既有契约（不修改）**：`LLMBackend.messages_create` / `MessageResponse` / `BackendCapabilities`（`agent/backend.py`）、`BackendRateLimited` / `BackendUnavailable` / `BackendError` / `BackendCapabilityViolation`（`core/exceptions.py`）、`ToolsAdapter.list_for_agent()` + `ToolsAdapter.dispatch()`（`agent/tools_adapter.py`）、`AgentSettings`（`core/config.py`）。
- **测试**：`tests/` 新增 `AgentLoop` 单测，无需付费 API。**happy-path / 并行 / cache-gate / 兜底**用既有 `FakeBackend(responses=[...])`（实证接口：仅吃 `MessageResponse` 序列，耗尽 raise `IndexError`，无异常注入、无 call recording）。**故障注入（限流/不可达/auth 异常序列）与「调用次数」断言**用测试模块内本地定义的 scripted backend double（实现 `LLMBackend` Protocol：按脚本依次 raise 指定异常或返回响应，并记录 `messages_create` 调用次数与入参）—— 这是测试夹具，不扩展已发布的 `FakeBackend` 契约。
- **不接 CLI / 不写 Planner prompt / 不接 MCP**（见非目标）。

## 非目标 (Non-Goals)

- ❌ **不写 Planner Agent 的系统 prompt 与能力概览** —— 归 M2.4 `add-planner-agent`。本骨架的 `system` 入参由调用方传入；测试用占位 system。
- ❌ **不接 CLI `--intent`** —— 归 M2.7。本变更只交付可被单测驱动的 `AgentLoop` 库类。
- ❌ **不实现 `LoopResult → Report` 的组装** —— 循环是**通用** tool dispatcher，不特判 `run_inspector`，也无法从 `RunInspectorOutput`（只含 findings，缺 status/duration）还原 `InspectorResult` 去喂 `Report.from_inspector_results`。Report 组装归 M2.4/M2.7（设计理由见 design.md）。
- ❌ **不实现 typed `ReportStatus` / `RunStatus` enum** —— 归 M3 `add-report-persistence-and-diff`；M2 用 `terminal_status` 字符串闭集承载。
- ❌ **不实现 `read_finding_detail` 工具** —— 已归档 `add-tool-registry-capability-layer` design 明确否决（M3 再加）。
- ❌ **不新增 backend 实现**，不碰 `BedrockBackend` / `ClaudeSubscriptionBackend`（M10.5）。
- ❌ **不实现 fallback_model 自动降级链路** —— `settings.agent.fallback_model` 字段已存在但本骨架只用 `primary_model`；配额降级切模型归后续。
- ❌ **不实现 extended thinking / streaming** —— M2 backend capability 即声明 False，Protocol 签名不含相应参数。
- ❌ **不实现 ARCHITECTURE §9 中 backend 异常层无法区分的 HTTP-状态级细分策略** —— 529 专项「固定 30s × 2 次」退避（backend 已把 529 收敛进 `BackendRateLimited`，循环用统一退避）、连接超时与 5xx 的差异化处理（均为 `BackendUnavailable`）、订阅模式软限制检测（M10.5 才有 `ClaudeSubscriptionBackend`）均不在本骨架。
- ❌ **`degraded_no_planner` 不触发「按 manifest 机械跑 Inspector」的兜底巡检** —— §9 该行的机械降级巡检需要 Inspector 列表 / Planner 上下文（M2.4+）。本骨架的 `degraded_no_planner` 仅表示「backend 不可达但已有部分 tool 结果，收尾输出已收集内容」。

## 对外契约影响

- **Agent tool schema**：无变更 —— 循环通过 `ToolsAdapter.list_for_agent()` 投影既有 3 个首批 ToolSpec（`run_inspector` / `list_inspectors` / `list_targets`），不新增不修改 ToolSpec。
- **新增内部 Python 契约**（非对外协议，但属本仓 SOT）：`AgentLoop` 构造签名 `AgentLoop(backend, tool_adapter, settings)`、`LoopResult` 数据模型字段集、`terminal_status` 字符串闭集、`UnexpectedStopReason` 异常。这些进 `agent-loop` spec。
- **CLI 命令**：无变更。
- **MCP tool schema / Notifier Protocol / Schedule manifest / Inspector schema**：均无变更。

## Agent 行为变更：Prompt Caching 策略与 Token 影响

- **缓存点**：本骨架只负责「**是否注入** `cache_control`」的 capability gate —— 当 `backend.capabilities.prompt_caching == True` 时，在传入的 `system` block 上标记 `cache_control: ephemeral`；为 False 时透传不标记。**具体缓存哪些内容（system prompt 全文 / tool registry 概览 / few-shot）的策略文档归 M2.5**，本变更只保证机制正确。
- **断言**：单测用 `FakeBackend` 构造两次响应，断言「`prompt_caching=False` 的 backend 上 `system` 不含 `cache_control` key」与「`prompt_caching=True` 上含」。`cache_read_input_tokens > 0` 的真实缓存命中断言归 M2.5（需真实/cassette backend）。
- **Token 影响**：循环每轮调用 `_track_token_usage(response.usage)` 累加 input+output（含 `cache_creation` / `cache_read` 字段），与 `token_budget_input` / `token_budget_output` 比较触发兜底。骨架本身不改变单次调用的 token 成本，只新增预算追踪。

## Failure Modes

1. **Backend 持续 429（限流）**：`messages_create` raise `BackendRateLimited`。循环 honor `retry_after_seconds`（无则固定退避），最多 3 次；超限后强制收尾，`terminal_status=degraded_rate_limited`，返回已累积的 tool 结果而非裸抛。
2. **Backend 5xx / 连接超时（不可达）**：raise `BackendUnavailable`。指数退避（1s/4s/16s，≤3 次）；超限后——已有 tool 结果则降级收尾（`degraded_no_planner`），无结果则 `failed_api_unavailable`（无可用产物）。
3. **Token 预算 / max turns 耗尽**：循环检测到超限 → 强制收尾（`degraded_token_budget` / `degraded_max_turns`），输出当前已收集结果，**不**再发起新一轮调用。
4. **Malformed tool_use args / 幻觉工具名 / handler 异常**：路径行为不同（按 `dispatch` 真实契约）。malformed args → `dispatch` raise `TypeError`，循环捕获 + `scrub_exception_message` 后作为 `is_error` tool_result 回灌；幻觉工具名 → 由前置成员检查（`block.name ∉ list_for_agent() 名字集`）在调用 dispatch 前拦截，回灌「无此工具」error tool_result；handler 内部异常 → 已被 `dispatch` 捕获 scrub 成 `is_error` envelope dict 返回（循环按多键签名识别，**不二次 scrub**，映射协议层 `is_error=True`）。上述均 continue，**一个工具失败不拖垮整轮**（并行 dispatch 下其余工具结果照常）。**向上抛不掩盖**的三类：`ToolPolicyViolation`（loop 广告了一个自己拒绝的工具 = 配置/loop bug）、已注册工具的 handler 内部 `KeyError`（name 已确认注册 → 代码 bug，不当幻觉名处理）、`ToolError`（output-schema 校验失败 = handler 返回类型错误的代码 bug）。fail-loud 上抛前，循环会取消并 drain 同 turn 其余未完成的并行 sibling task（防 orphaned handler 泄漏）。
5. **模型返回空内容 / 非预期 stop_reason**：`end_turn` 但无可用内容、或 `stop_reason=refusal`（§9「拒绝回答」行）→ `terminal_status=empty_response`，原始响应留存便于调试；`max_tokens`（单次输出被截断）→ `degraded_token_budget` 收尾；`stop_sequence`/`pause_turn`（Hostlens 不使用 stop sequences / server-tool pause）→ raise `UnexpectedStopReason`（明确暴露，不静默）。

## Operational Limits

- **并发预算**：同一 turn 内 `tool_use` block 并行 dispatch（`asyncio.gather`），并行度受该 turn 模型实际产出的 tool_use 数量约束（通常 ≤5）；底层 Inspector 采集的并发预算由 `run_inspector` handler / runner 控制，本骨架不再加全局信号量（避免重复限流，OPERABILITY §1）。
- **超时**：单次 `messages_create` 传 `timeout=60.0`（与 ARCHITECTURE §9 示例一致）；可由 settings 后续参数化，本变更先硬编码常量并注释 WHY。
- **内存预算**：循环持有完整 `messages` 历史（多轮累积）。骨架不做历史截断 / 压缩（M3+ 视 token 预算再评估）；token 预算兜底是事实上的内存上界代理。
- **Turn 上限**：`settings.agent.max_turns`（默认 20，schema 约束 1–100）。

## Security & Secrets

- **不引入新密钥** —— backend 已持有 API key，循环只调 `messages_create`，不接触凭据。
- **脱敏**：tool handler 异常回灌给模型前，错误文本走既有 `scrub_exception_message`，避免把路径 / 内网地址 / env 值泄露进对话历史（继而进报告）。`LoopResult` 不得携带未脱敏的 backend 异常原文。
- **攻击面**：不扩大 —— 循环只能调用 `surfaces ∋ "agent"` 的 ToolSpec（policy gate 在 adapter dispatch 层既有强制），无法执行任意命令（CLAUDE.md §7 反模式：不暴露 `exec_arbitrary_command`）。

## Cost / Quota Impact

- **单测全程零 API 成本** —— happy-path 走 `FakeBackend`、故障路径走本地 scripted backend double，均不触网；CI 默认 `-m 'not live'`，不消耗 Anthropic 额度。
- **运行时**：循环把多轮调用收口到单一 backend 入口，token 预算兜底（默认 100K input + 30K output）是单次 run 的硬上限，防止失控的 turn 循环烧穿配额。prompt caching 命中（M2.5 策略落地后）进一步降低重复 system/tool schema 的 input token 成本。

## Demo Path

本变更交付库类，无 CLI 入口（CLI 归 M2.7），5 分钟 reproduce 走单测：

```bash
pip install -e ".[dev]"
# happy-path/并行/cache/usage 兜底用 FakeBackend；failure 分支用本地 scripted backend double
pytest tests/agent/test_loop.py -v        # 全绿，零 API 调用
mypy --strict src/hostlens/agent/loop.py  # 类型完整
```

一个最小内联示例（写进 spec/测试，演示「意图 → 两轮 tool-use → 收尾」）：用 `FakeBackend(responses=[<tool_use 响应>, <end_turn 响应>])` + 注册了 `list_inspectors` 的 `ToolsAdapter` + 含非 `None` `AgentSettings` 的 `Settings` 构造 `AgentLoop`，`await loop.run("列出可用 inspector")` 返回 `LoopResult(terminal_status="ok", turns=2, ...)`。
