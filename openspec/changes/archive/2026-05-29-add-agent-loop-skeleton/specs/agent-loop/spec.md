## 新增需求

### 需求:`AgentLoop` 构造契约与 backend 私有持有

`hostlens.agent.loop.AgentLoop` 必须以 `AgentLoop(backend: LLMBackend, tool_adapter: ToolsAdapter, settings: Settings, *, system: list[dict] | str | None = None)` 构造。`system` 是调用方注入的系统提示词（design D-2「调用方传入 system」），构造期存为实例属性供 `run()` 使用；`None` 视为空（`[]`）。`backend` 必须作为 `AgentLoop` 的私有属性持有，**禁止**经由 `ToolContext` 或任何全局单例暴露给 tool handler（ADR-008 / CLAUDE.md §7 反模式）。`AgentLoop` 必须不直接 `import anthropic` —— 所有模型调用经 `backend.messages_create`。

> M2.2 不构造 system prompt 内容（那是 M2.4 Planner 的职责）；本骨架只负责把调用方注入的 `system` 透传给 backend，并按 capability 决定是否在其上注入 `cache_control`。

#### 场景:构造后 backend 不出现在 tool dispatch 路径

- **当** 构造 `AgentLoop(backend, adapter, settings)` 并在一轮中 dispatch 任一工具
- **那么** 传给 tool handler 的 `ToolContext` 不含 `backend` / `llm_backend` 字段（`ToolContext` 的既有字段集不被本变更扩展）

#### 场景:loop 模块不依赖 anthropic SDK

- **当** import `hostlens.agent.loop`
- **那么** 该模块的源码不含 `import anthropic`；模型调用仅通过注入的 `LLMBackend.messages_create` 发生

### 需求:构造期校验 `settings.agent` 必须存在

`Settings.agent` 类型为 `AgentSettings | None` 且默认 `None`。由于 `AgentLoop` 依赖 `settings.agent.{primary_model, max_turns, token_budget_input, token_budget_output}`，`AgentLoop.__init__` 必须在 `settings.agent is None` 时立即 raise `hostlens.core.exceptions.ConfigError`，**禁止**静默默认到 `AgentSettings()` 或延迟到 `run()` 才失败。

#### 场景:agent 节缺失构造即失败

- **当** 以 `settings.agent is None` 的 `Settings` 构造 `AgentLoop`
- **那么** `__init__` raise `ConfigError`，不返回可用实例

#### 场景:agent 节存在构造成功

- **当** 以含非 `None` `AgentSettings` 的 `Settings` 构造 `AgentLoop`
- **那么** 构造成功，后续 `run()` 使用该 `AgentSettings` 的 model 与预算参数

### 需求:多轮 tool-use 循环按 `stop_reason` 推进

`AgentLoop.run(intent: str)` 必须以 `[{"role": "user", "content": intent}]` 起始 messages，循环调用 `backend.messages_create`。当 `response.stop_reason == "end_turn"` 时必须终止并返回 `LoopResult`；当 `== "tool_use"` 时必须 dispatch 工具、把 assistant 消息与 tool_result 追加进 messages 后续跑下一轮。

#### 场景:单轮 end_turn 直接终止

- **当** `FakeBackend` 首个响应 `stop_reason="end_turn"` 且含一个 text block
- **那么** `run()` 返回 `LoopResult`，`turns == 1`，`terminal_status == "ok"`，`final_text` 等于该 text block 文本

#### 场景:tool_use 续跑再 end_turn

- **当** `FakeBackend` 依次返回 `stop_reason="tool_use"`（含一个 `list_inspectors` 的 ToolUseBlock）与 `stop_reason="end_turn"`
- **那么** `run()` 跑满 2 轮，`turns == 2`，`terminal_status == "ok"`，`tool_invocations` 含 1 条 `list_inspectors` 记录；第二轮发给 backend 的 messages 含 assistant 的 tool_use 消息与 role=user 的 tool_result 消息

### 需求:同一 turn 内多个 `tool_use` 并行 dispatch

当一个响应含多个 `ToolUseBlock` 时，`AgentLoop` 必须用 `asyncio.gather` 并行 dispatch 它们，并把每个结果按对应 `tool_use_id` 组装为 tool_result。当其中任一 dispatch 抛出 fail-loud 异常（`KeyError` / `ToolPolicyViolation` / `ToolError` / `asyncio.CancelledError`）时，`AgentLoop` 必须**取消并 drain 同 turn 其余尚未完成的并行 task** 后再向上抛出原异常 —— `asyncio.gather` 默认只传播首个异常、不取消 sibling，会留下 orphaned 长跑 handler（如仍在执行的 SSH/inspector 采集）泄漏资源。

#### 场景:两个 tool_use 并行执行且结果各归其位

- **当** 单个响应含两个 ToolUseBlock（不同 `id`）
- **那么** 两次 dispatch 并行发生（非串行），下一轮 user 消息含两个 tool_result，每个 `tool_use_id` 与发起的 block `id` 一一对应

#### 场景:并行中一个工具 fail-loud 时其余 sibling 被取消

- **当** 同一 turn 两个 tool_use 并行，其一 dispatch 抛 fail-loud 异常（如 `ToolError` / `ToolPolicyViolation`），另一是仍在运行的长跑 handler
- **那么** `run()` 向上抛出该 fail-loud 异常前，未完成的 sibling task 被 `cancel()` 并 drain（不留 orphaned 运行中的 handler）；向上抛的异常类型是原 fail-loud 异常本身（不被包装成 `ExceptionGroup`）

### 需求:`cache_control` 注入由 backend capability 决定

调用 `messages_create` 前，`AgentLoop` 必须检查 `backend.capabilities.prompt_caching`：为 `True` 且构造期注入的 `system` 为非空 `list[dict]` 时，在其最后一个 block 注入 `cache_control: ephemeral`；为 `False` 时**禁止**注入任何 `cache_control`。注入判定必须在 loop 端完成，禁止依赖 backend 静默丢弃。

#### 场景:prompt_caching=False 不注入

- **当** 以非空 `list[dict]` 的 `system` 构造 `AgentLoop`、backend 的 `capabilities.prompt_caching == False`，`run()` 触发一次 `messages_create`
- **那么** 传给 `messages_create` 的 `system` 任一 block 不含 `cache_control` key（因此 backend 的 `check_capability_consistency` 不会 raise `BackendCapabilityViolation`）

#### 场景:prompt_caching=True 注入 ephemeral

- **当** 以非空 `list[dict]` 的 `system` 构造 `AgentLoop`、backend 的 `capabilities.prompt_caching == True`，`run()` 触发一次 `messages_create`
- **那么** 传给 `messages_create` 的 `system` 至少一个 block 含 `{"cache_control": {"type": "ephemeral"}}`

### 需求:token 预算是 per-run 硬上限，逐轮收缩 max_tokens 强制收尾

`token_budget_output` 是**整个 `run()`** 的输出 token 上限（per-run，非 per-call）。`AgentLoop` 必须累加每轮 `response.usage` 的 input/output token，并据此：

1. **逐轮收缩 `max_tokens`**：每次调 `messages_create` 传的 `max_tokens` 必须是**剩余**输出预算 `token_budget_output - usage.output_tokens`（至少为 1），**禁止**每轮都传完整 `token_budget_output`（否则 per-run 上限退化成 per-call，单次 run 输出最坏可超支近一倍）。
2. **发起下一轮前的兜底闸**：若累计 input `>= token_budget_input` 或累计 output `>= token_budget_output`，必须停止循环、强制收尾，**禁止**再发起新一轮调用，返回 `terminal_status == "degraded_token_budget"`，携带已累积的 `tool_invocations`。

#### 场景:超预算后不再发起调用

- **当** 设置极小的 `token_budget_output`，记录调用次数的测试 backend 首轮返回 `tool_use` 且 usage 已达/超该预算
- **那么** 循环在收到首轮后停止，`terminal_status == "degraded_token_budget"`，backend 收到的 `messages_create` 调用次数为 1（未发起第二轮）

#### 场景:后续轮次 max_tokens 收缩为剩余预算

- **当** `token_budget_output == 100`，第一轮 `response.usage.output_tokens == 30`，第二轮继续（仍 tool_use）
- **那么** 第二轮传给 `messages_create` 的 `max_tokens == 70`（剩余预算），而非完整 100

### 需求:最大 turn 数兜底

`AgentLoop` 必须在 turn 计数达到 `settings.agent.max_turns` 时强制收尾，返回 `terminal_status == "degraded_max_turns"`，禁止超出上限继续调用。

#### 场景:达到 max_turns 上限收尾

- **当** `settings.agent.max_turns == 2`，记录调用次数的测试 backend 持续返回 `stop_reason="tool_use"`（永不 end_turn）
- **那么** `run()` 在第 2 轮后停止，`turns == 2`，`terminal_status == "degraded_max_turns"`，`messages_create` 调用次数为 2

### 需求:backend 限流按 retry-after 重试，超限降级

当 `messages_create` raise `BackendRateLimited` 时，`AgentLoop` 必须 honor `retry_after_seconds`（为 `None` 时用固定退避常量）重试，最多 3 次；连续超过 3 次后必须强制收尾，返回 `terminal_status == "degraded_rate_limited"`，携带已累积的结果而非向上抛裸异常。

#### 场景:限流重试后成功

- **当** backend 首次 raise `BackendRateLimited(retry_after_seconds=0)`，重试时返回 `end_turn`
- **那么** `run()` 最终返回 `terminal_status == "ok"`，对应轮的调用经历了一次重试

#### 场景:持续限流超过 3 次降级

- **当** backend 每次调用都 raise `BackendRateLimited`
- **那么** `run()` 重试至上限后停止，`terminal_status == "degraded_rate_limited"`，不向上抛 `BackendRateLimited`

### 需求:backend 不可达指数退避，超限按有无结果分流

当 `messages_create` raise `BackendUnavailable`（5xx / 连接超时）时，`AgentLoop` 必须指数退避重试最多 3 次；超限后：已累积 ≥1 条 `tool_invocations` 则收尾返回 `terminal_status == "degraded_no_planner"`；无任何结果则返回 `terminal_status == "failed_api_unavailable"`。

#### 场景:首轮即不可达且无结果

- **当** backend 首次调用即持续 raise `BackendUnavailable`，无任何 tool 已执行
- **那么** `run()` 重试至上限后返回 `terminal_status == "failed_api_unavailable"`，`tool_invocations` 为空

#### 场景:有结果后不可达降级为 degraded_no_planner

- **当** 第一轮成功执行了一个工具，第二轮起 backend 持续 raise `BackendUnavailable`
- **那么** `run()` 返回 `terminal_status == "degraded_no_planner"`，`tool_invocations` 含第一轮的记录

### 需求:不可重试的 backend 异常直接上抛

当 `messages_create` raise `BackendCapabilityViolation`，或 raise `BackendError` 且其 `kind` 属于不可重试域（如 `"auth_invalid"`）时，`AgentLoop` 必须**不重试**并向上抛出该异常（不映射为 `terminal_status`）—— 配置错误与 loop 自身 bug 必须立即暴露。

#### 场景:capability violation 上抛

- **当** backend `messages_create` raise `BackendCapabilityViolation`
- **那么** `run()` 不捕获该异常，原样向上传播

#### 场景:auth_invalid 不重试上抛

- **当** backend raise `BackendError(kind="auth_invalid")`
- **那么** `run()` 不重试、不降级，原样向上传播该异常

### 需求:工具结果按 `ToolsAdapter.dispatch` 真实契约分流，单工具失败不中断循环

`ToolsAdapter.dispatch(name, args_json, ctx) -> dict[str, Any]` 的真实语义（本变更必须据此实现，禁止假设它返回 `BaseModel` 或对 handler 异常 raise）：成功返回 `model_dump()` dict；**handler 内部异常已被 dispatch 捕获并经 `scrub_exception_message` 脱敏**，以 `{"is_error": True, "error_kind", "tool_name", "message", "cause"}` envelope dict **返回**（不 raise）；malformed args（输入不过 input schema）raise `TypeError`；output-schema 校验失败（handler 返回类型错误）raise `ToolError`（fail-loud，见独立需求）；模型幻觉的工具名 raise `KeyError`。

**幻觉工具名必须用「名字成员检查」拦截，禁止依赖 `KeyError` 判别。** 因为 `dispatch` 的 `KeyError` 同类型不可区分两种来源：① step1 `registry.get(name)` 查不到（裸 `KeyError`）；② handler 内部 re-raise 的裸 `KeyError`（adapter step6 原样 raise）。把所有 `KeyError` 当幻觉工具名回灌会**掩盖 handler 内部 bug**。`AgentLoop` 必须用 `list_for_agent()` 已得的 advertise 工具名集合做前置成员检查。

`AgentLoop` 的 `_dispatch_one` 必须：

- `block.name ∉ advertise 名字集` → 判定为模型幻觉工具名，**不调用 dispatch**，直接回灌「无此工具 `<name>`」的 `is_error` tool_result，记 `ToolInvocation(error=...)`，continue。
- `block.name ∈ advertise 名字集` → 调 dispatch，按下分流：
  - 返回 dict 且**不匹配** error envelope 签名 → 正常 tool_result，记 `ToolInvocation(output=...)`。
  - 返回 dict 且**匹配** error envelope 签名（`is_error is True` 且含 `error_kind` 与 `message` 键）→ 映射 Anthropic `tool_result(is_error=True)`，记 `ToolInvocation(error=...)`；**禁止二次 `scrub`**（dispatch 已脱敏）。判别**禁止**仅凭裸 `is_error` 真值（业务 output schema 理论上可含同名字段），必须校验 envelope 的多键签名。
  - raise `TypeError`（malformed args）→ 循环捕获，经 `scrub_exception_message` 后作 `is_error` tool_result 回灌，记 `ToolInvocation(error=...)`，continue。
  - raise `KeyError`（此时 name 已确认注册 → 只可能是 handler 内部 bug）→ **不捕获，向上抛**（fail-loud，禁止当幻觉工具名掩盖）。

**禁止**因 malformed args / 幻觉工具名 / handler envelope 中断整个循环；**禁止**把未脱敏异常原文写入 messages 或 `LoopResult`。

回灌给 backend 的 `tool_result` block 的 `content` 必须是 Anthropic API 接受的形态（字符串或 content-block 列表），**禁止**直接放裸 `dict`（`messages` 由 backend verbatim 透传给 SDK，裸 dict 在真实 backend 上非法）。loop 必须把 dispatch 返回的 dict（成功结果或 error envelope）序列化为 JSON 文本承载；结构化 dict 仍原样保留在 `ToolInvocation.output/error` 供调用方读取。

#### 场景:tool_result content 为 Anthropic-valid 形态

- **当** 任一工具 dispatch 成功（或返回 error envelope），其结果被组装进下一轮 user 消息
- **那么** 该 `tool_result` block 的 `content` 是字符串（JSON 序列化）或 content-block 列表，不是裸 `dict`；同一结果的结构化 dict 仍可在对应 `ToolInvocation.output`/`error` 读到

#### 场景:handler 内部异常以 dispatch 的 envelope 回灌且不二次脱敏

- **当** 某工具 handler 抛异常（dispatch 返回匹配签名的 `is_error` envelope），下一轮 backend 返回 `end_turn`
- **那么** 该 tool_result 标记 `is_error=True`、content 为 dispatch 返回的 envelope，循环继续到 `terminal_status == "ok"`；该 envelope 的 `message` 未被循环再次 `scrub`

#### 场景:malformed args 触发 TypeError 被循环捕获回灌

- **当** 某轮 ToolUseBlock 的 `input` 不满足该 ToolSpec 输入 schema（dispatch raise `TypeError`），下一轮 backend 返回 `end_turn`
- **那么** 循环捕获 `TypeError`、以脱敏后的 `is_error` tool_result 回灌，未中断，`terminal_status == "ok"`

#### 场景:幻觉工具名在 dispatch 前被成员检查拦截

- **当** 模型发出不在 `list_for_agent()` advertise 名字集中的工具名
- **那么** 循环不调用 `dispatch`，直接回灌「无此工具」的 `is_error` tool_result，未中断循环

#### 场景:已注册工具的 handler 内部 KeyError 向上抛不被掩盖

- **当** 某个 advertise 的工具其 handler 内部 raise `KeyError`（dispatch 原样 re-raise）
- **那么** `run()` 不捕获该 `KeyError`、不回灌为 tool_result，原样向上传播（fail-loud）

#### 场景:并行中单工具失败被隔离

- **当** 两个并行 tool_use 中一个失败（envelope 或 `TypeError` 或幻觉名）、另一个成功
- **那么** 两个 tool_result 都生成（一个 error、一个正常），各按其 `tool_use_id` 归位，循环 continue

### 需求:`ToolPolicyViolation` / `ToolError` / 取消异常向上传播

当 `ToolsAdapter.dispatch` raise `ToolPolicyViolation`（循环 advertise 了一个在 dispatch 被 policy gate 拒绝的工具 = registry/loop 配置 bug）或 `ToolError`（output-schema 校验失败 = handler 返回类型错误的代码 bug）时，`AgentLoop` 必须**不捕获、不回灌**，原样向上抛出 —— fail-loud 暴露代码/配置错误，禁止喂回模型掩盖。`asyncio.CancelledError` 同样必须向上传播以支持协作取消。注意：循环只 `except TypeError`（input-malformed 回灌），故 `ToolError`（非 `TypeError` 子类）天然不被捕获、自然向上传播。

#### 场景:ToolPolicyViolation 中断并上抛

- **当** 某轮 dispatch 一个工具时 raise `ToolPolicyViolation`
- **那么** `run()` 不捕获该异常，原样向上传播（不映射为 `terminal_status`、不回灌为 tool_result）

#### 场景:output-contract ToolError 中断并上抛

- **当** 某 advertise 工具的 handler 返回非 `output_schema` 类型，dispatch raise `ToolError`
- **那么** `run()` 不捕获该异常，原样向上传播（fail-loud，不回灌为 tool_result）

### 需求:全部 6 个 `stop_reason` 都有明确归属

`MessageResponse.stop_reason` 是 6 值 Literal（`end_turn`/`tool_use`/`max_tokens`/`stop_sequence`/`pause_turn`/`refusal`），`AgentLoop` 必须穷举处理、不留未定义分支：

- `end_turn`：有可用内容 → `ok`；`content` 空 → `empty_response`（保留原始响应便于调试）。
- `tool_use`：dispatch 工具并续跑。
- `refusal`：→ `empty_response`（ARCHITECTURE §9「拒绝回答」行映射到 empty_response，**禁止**抛 `UnexpectedStopReason`）。
- `max_tokens`：→ `degraded_token_budget`（单次输出被截断，语义等同预算耗尽）；必须用 `_join_text(response)` 保留截断前已生成的 assistant 文本到 `LoopResult.final_text`（不丢部分输出）。
- `stop_sequence` / `pause_turn`：Hostlens 不传 stop sequences、不使用 server-tool pause，出现即非预期 → raise `hostlens.core.exceptions.UnexpectedStopReason`（携带该 stop_reason 值）。

#### 场景:空 end_turn 标记 empty_response

- **当** backend 返回 `stop_reason="end_turn"` 且 `content == []`
- **那么** `run()` 返回 `terminal_status == "empty_response"`

#### 场景:refusal 映射 empty_response

- **当** backend 返回 `stop_reason="refusal"`
- **那么** `run()` 返回 `terminal_status == "empty_response"`（不抛异常）

#### 场景:max_tokens 映射 degraded_token_budget 且保留部分文本

- **当** backend 返回 `stop_reason="max_tokens"` 且 content 含一个 text block `"partial"`
- **那么** `run()` 返回 `terminal_status == "degraded_token_budget"` 且 `final_text == "partial"`（截断前的部分输出未被丢弃）

#### 场景:真正非预期 stop_reason 抛 UnexpectedStopReason

- **当** backend 返回 `stop_reason="stop_sequence"` 或 `"pause_turn"`
- **那么** `run()` raise `UnexpectedStopReason`，异常携带该 stop_reason 值

### 需求:`LoopResult` 输出 schema

`AgentLoop.run()` 必须返回 `hostlens.agent.loop.LoopResult`（Pydantic v2，`frozen=True`），字段恰好为：`final_text: str`、`tool_invocations: list[ToolInvocation]`、`turns: int`、`terminal_status: Literal[...]`、`usage_totals: LoopUsage`、`stop_reason: str | None`。`terminal_status` 的取值必须恰好为闭集 `{"ok", "degraded_rate_limited", "degraded_token_budget", "degraded_max_turns", "degraded_no_planner", "empty_response", "failed_api_unavailable"}`。`ToolInvocation` 必须记录 `tool_name` / `tool_use_id` / `input` 以及 `output`（成功）或 `error`（失败）二者之一。

#### 场景:terminal_status 取值受 Literal 约束

- **当** 以闭集之外的字符串构造 `LoopResult(terminal_status=...)`
- **那么** Pydantic 校验失败抛 `ValidationError`

#### 场景:usage_totals 累加多轮 token

- **当** 跑满 2 轮，两轮 usage 分别为 (input=10,output=5) 与 (input=20,output=7)
- **那么** `LoopResult.usage_totals.input_tokens == 30` 且 `output_tokens == 12`
