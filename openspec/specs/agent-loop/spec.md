# agent-loop 规范

## 目的
待定 - 由归档变更 add-agent-loop-skeleton 创建。归档后请更新目的。
## 需求
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

调用 `messages_create` 前，`AgentLoop` 必须按**两层缓存策略**注入 `cache_control: ephemeral`，且仅当 `backend.capabilities.prompt_caching == True` 时注入；为 `False` 时**禁止**在 `system` / `tools` / `messages` 任一处注入任何 `cache_control`。注入判定必须在 loop 端完成，禁止依赖 backend 静默丢弃。

两层断点：

- **断点 A（静态前缀）**：当构造期注入的 `system` 为非空 `list[dict]` 时，在其最后一个 block 注入 `cache_control: ephemeral`。该断点缓存 `tools + system` 这段跨 run 稳定的最长前缀（Anthropic 前缀顺序为 `tools → system → messages`，system 断点天然吞掉前面的 tools）。`tools` 数组**禁止**单独携带 `cache_control` —— 它已被断点 A 的前缀覆盖，单独标记只会浪费断点预算。`system` 为裸字符串或空 list 时跳过断点 A（不报错）。
- **断点 B（滚动对话前缀）**：每次调用 `messages_create` 前，仅在当前 `messages` 最后一个 message 的最后一个 content block 上注入 `cache_control: ephemeral`，且在 messages 浅拷贝上操作（只浅拷贝被标记的末 message 及其末块），不得 mutate loop 持有的 `messages`。因注入只作用于请求快照、从不写回存储的 messages（见本需求末段），历史 message 天然不带 `cache_control`，故**无需也不得**额外做「清除历史断点」的归一化（那是对不可能分支的防御）。当末 message 的 `content` 不是非空 block 列表（如裸字符串）时跳过断点 B（不强转、不报错），断点 A 仍生效。

断点注入必须作用于「即将发出的请求快照」，不得 mutate loop 持有的 `self._system` 或累积的 `messages` 列表。任一请求携带的 `cache_control` 断点数必须恒 ≤ 2（A + B），不得随 turn 数增长。

#### 场景:prompt_caching=False 三处零注入

- **当** 以非空 `list[dict]` 的 `system` 构造 `AgentLoop`、backend 的 `capabilities.prompt_caching == False`，`run()` 触发若干轮 `messages_create`
- **那么** 每次传给 `messages_create` 的 `system` / `tools` / `messages` 中任一 block 都不含 `cache_control` key（因此 backend 的 `check_capability_consistency` 不会 raise `BackendCapabilityViolation`）

#### 场景:prompt_caching=True 断点A在system末块且tools无断点

- **当** 以非空 `list[dict]` 的 `system` 构造 `AgentLoop`、backend 的 `capabilities.prompt_caching == True`，`run()` 触发一次 `messages_create`
- **那么** 传给 `messages_create` 的 `system` 最后一个 block 含 `{"cache_control": {"type": "ephemeral"}}`
- **并且** 传给 `messages_create` 的 `tools` 数组中没有任何元素含 `cache_control` key

#### 场景:prompt_caching=True 滚动断点B只在最新message末块

- **当** backend `capabilities.prompt_caching == True`，loop 跑到第二轮（`messages` 已含 user / assistant / tool_result 多个 message）触发 `messages_create`
- **那么** 传给 `messages_create` 的 `messages` 中，仅最后一个 message 的最后一个 content block 含 `{"cache_control": {"type": "ephemeral"}}`，其余 message 的所有 block 都不含 `cache_control` key

#### 场景:断点数恒不超过2且不随turn增长

- **当** backend `capabilities.prompt_caching == True`、`system` 为非空 list，loop 从 `run(intent)` 起连续触发多轮 `messages_create`（首轮末 message 为裸字符串 `intent`，后续轮末 message 为 `tool_result` block 列表）
- **那么** 首轮请求 `system` + `tools` + `messages` 携带的 `cache_control` 断点总数为 1（仅断点 A；断点 B 因末 message 为裸字符串被跳过）
- **并且** 后续每一轮请求的断点总数为 2（断点 A 一个、断点 B 一个）
- **并且** 任一轮请求的断点总数都不超过 2，且不随 turn 数增加

#### 场景:末message为裸字符串时跳过断点B保留断点A

- **当** backend `capabilities.prompt_caching == True`、`system` 为非空 list，但当前 `messages` 最后一个 message 的 `content` 为裸字符串，触发 `messages_create`
- **那么** 该 message 不含 `cache_control`（断点 B 被跳过），且 `system` 最后一个 block 仍含 `{"cache_control": {"type": "ephemeral"}}`（断点 A 生效）

#### 场景:第二次调用真实命中静态前缀缓存

- **当**（`@pytest.mark.live`，opt-in 真实 Anthropic API）以非空 list 的 `system`、`prompt_caching == True`，且 `tools + system`（或 padded `system`）静态前缀 token 数已**超过所用 model 的最小可缓存阈值**（Sonnet/Opus ≈1024、Haiku ≈2048；前缀不足时由测试显式 pad 越过阈值），发起至少第二轮 `messages_create`
- **那么** 第二轮响应的 `cache_read_input_tokens > 0`（静态前缀在第二轮被复用）
- **测试方式（spec↔test 如实声明）** 该 live 验收驱动 `AgentLoop` 的真实注入函数（`_inject_cache_control` / `_roll_message_cache_breakpoint`）以「`run()` 发出的请求形态」连续发起 ≥3 次 `messages_create`，而**非**驱动 `AgentLoop.run()` 经真实模型多轮 tool-use —— 因真实模型未必每轮确定性 emit `tool_use`，经 `run()` 跑真模型会让 live 测试间歇假阴性。两种路径发出的请求前缀形态一致，故本场景命题（第 2/3 次调用复用静态前缀）被等价覆盖；loop 的多轮控制流由第 2 节 CI 结构测试经 `run()` 端到端验证
- **覆盖范围（必须如实声明，不得过度宣称）** 此 live 验收**只覆盖静态前缀断点 A 的真实命中**：`cache_read_input_tokens` 是单一聚合值，断点 B（对话前缀）在 turn2 才 create、turn3 才首次 read，且其 read 量无法从聚合值中干净地与 A 分离。因此断点 B 的正确性由 CI 结构断言（B 落在正确位置、不写回存储 messages、断点数序列 `[1,2,2,…]`）保证，**不**由本 live 场景宣称验证。前缀低于阈值时 Anthropic 不缓存属环境前提不满足、非实现缺陷，故前置条件必须由测试保证

#### 场景:多轮真实命中持续有效

- **当**（`@pytest.mark.live`，承前置条件与上一场景的测试方式）发起至少第三轮 `messages_create`
- **那么** 第三轮响应的 `cache_read_input_tokens > 0`（缓存命中在多轮中持续有效；聚合值不区分 A / B 的贡献，断点 B 的位置与断点数不变量由 CI 结构断言保证、不由本场景宣称）
- **说明** 此处只断言聚合命中持续 > 0，**不**对 A / B 各自的 read 量做拆分归因（聚合值无法区分），拆分归因属过度验证、本提案不做

### 需求:token 预算是 per-run 硬上限,逐轮收缩 max_tokens 强制收尾

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

### 需求:backend 限流按 retry-after 重试,超限降级

当 `messages_create` raise `BackendRateLimited` 时，`AgentLoop` 必须 honor `retry_after_seconds`（为 `None` 时用固定退避常量）重试，最多 3 次；连续超过 3 次后必须强制收尾，返回 `terminal_status == "degraded_rate_limited"`，携带已累积的结果而非向上抛裸异常。

#### 场景:限流重试后成功

- **当** backend 首次 raise `BackendRateLimited(retry_after_seconds=0)`，重试时返回 `end_turn`
- **那么** `run()` 最终返回 `terminal_status == "ok"`，对应轮的调用经历了一次重试

#### 场景:持续限流超过 3 次降级

- **当** backend 每次调用都 raise `BackendRateLimited`
- **那么** `run()` 重试至上限后停止，`terminal_status == "degraded_rate_limited"`，不向上抛 `BackendRateLimited`

### 需求:backend 不可达指数退避,超限按有无结果分流

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

### 需求:工具结果按 `ToolsAdapter.dispatch` 真实契约分流,单工具失败不中断循环

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

### 需求:`AgentLoop.run` 可选 observer 接收类型化 LoopEvent

`AgentLoop.run` 必须接受一个可选关键字参数 `observer`（`LoopObserver | None`，默认 `None`）。当 `observer` 为 `None` 时，loop 的行为必须与未引入 observer 前完全一致 —— 不发任何事件、不改变控制流、不影响 `LoopResult`、既有测试不变。

当 `observer` 非 `None` 时，loop 必须在以下边界发出类型化 `LoopEvent` 并调用 `observer.on_event(event)`：
- 每轮发起模型调用前发 `TurnStarted`；
- 收到模型响应并完成本轮 usage 累计后发 `ModelResponded`（含 `stop_reason` 与本轮 assistant 文本 `text`）；
- **每个** `tool_use` 块在进入 `_dispatch_one` 时（任何分支判断之前）发 `ToolStarted`；对**产出 `ToolInvocation` 的块**——成功、幻觉工具名、malformed args（`TypeError`）、handler 异常 error envelope——在得到该 `ToolInvocation` 后发 `ToolCompleted`（与 `LoopResult.tool_invocations` 一一对应）；
- 终态收尾时发 `RunFinalized`（含 `terminal_status` 与 `turns`）。

**fail-loud 路径不发 `ToolCompleted`**（保持 loop 既有错误路由不变、本提案 additive 不改）：`ToolPolicyViolation`、output-contract `ToolError`、已注册 handler 内部 `KeyError`、`CancelledError` 等在 `_dispatch_one` **不**产出 `ToolInvocation`、直接上抛中断该 turn（取消 sibling）。这些块可能已发过 `ToolStarted`，但**不会**有配对的 `ToolCompleted`，且整个 `run` 抛出异常、**不**发 `RunFinalized`、**不**返回 `LoopResult`。observer 不得假设每个 `ToolStarted` 必有 `ToolCompleted`。

**`on_event` 契约**：observer 实现的 `on_event` 必须同步、非阻塞、且**不得抛出异常**。loop 直接调用 `observer.on_event(event)`，**不**对其做防御性 try/except 包裹（遵循「错误只在边界处理、不写防御性 fallback」红线）—— observer 自身负责吞掉并隔离其内部（如渲染）错误。`LoopEvent` 必须是不可变值对象（frozen dataclass），`LoopObserver` 必须是结构化 `@runtime_checkable` Protocol（实现方无需继承）。

**`ModelResponded.text`**：仅供展示用，取自该轮响应的文本块拼接（`_join_text`）；在 `tool_use` 轮次中模型常只发 tool_use 块而无文本，故 `text` 允许为空字符串。observer 不得假设它非空，也不得依赖它表达「thinking」（M2 无 extended_thinking / streaming）。

**事件顺序保证（偏序，非全序）**：单轮内若并行 dispatch 多个 `tool_use` 块（loop 用 `asyncio.gather`），各块的 `ToolStarted` / `ToolCompleted` **可能交错**，loop 仅保证：(a) turn 级顺序正确（`TurnStarted` 先于该轮的工具事件、晚于工具事件的下一轮 `TurnStarted`）；(b) 同一块的 `ToolStarted` 先于其 `ToolCompleted`；(c) 可通过事件携带的 `turn` 与 `tool_use_id` 关联归属。observer / 测试不得假设多工具的事件全序。

#### 场景:observer=None 时行为不变
- **当** 以默认 `observer=None` 调用 `AgentLoop.run(intent)`
- **那么** 不得发出任何事件，`LoopResult` 与控制流必须与引入 observer 前一致

#### 场景:单工具多轮发出有序事件序列
- **当** 传入一个记录事件的 observer，Agent 经「单个 tool_use 工具的 tool_use 轮 → end_turn 轮」完成
- **那么** observer 必须按序收到 `TurnStarted` → `ModelResponded` → `ToolStarted` → `ToolCompleted` → `TurnStarted` → `ModelResponded` → `RunFinalized`，且 `RunFinalized.terminal_status` 与返回的 `LoopResult.terminal_status` 一致

#### 场景:同轮多工具并行事件按偏序与关联校验
- **当** 某轮并行 dispatch 两个 `tool_use` 块
- **那么** 每个块必须各发一对 `ToolStarted`/`ToolCompleted`（同块 Started 先于 Completed），两块之间的事件允许交错；测试必须按 `tool_use_id` 关联而非假设全序

#### 场景:工具完成事件携带对应 invocation
- **当** 某轮 dispatch 一个 `run_inspector` 工具
- **那么** 该次 `ToolCompleted.invocation` 必须等于最终出现在 `LoopResult.tool_invocations` 中的同一条记录（成功填 output / 失败填 error）

#### 场景:幻觉工具名也配对发出工具事件
- **当** 某轮的 `tool_use` 块是一个未注册（幻觉）工具名，loop 不调用 handler 直接产出 error invocation
- **那么** observer 仍必须收到该块的 `ToolStarted` 与 `ToolCompleted`（`ToolCompleted.invocation.error` 非空），事件流不遗漏该工具边界

#### 场景:fail-loud 工具路径不发 ToolCompleted
- **当** 某轮的 `tool_use` 块触发 fail-loud 路径（如 dispatch 抛 `ToolPolicyViolation`，不产出 `ToolInvocation`）
- **那么** 该块可能已发 `ToolStarted`，但**不得**有配对的 `ToolCompleted`；异常上抛中断该 turn，`run` 抛出异常、**不**发 `RunFinalized`、**不**返回 `LoopResult`（loop 既有 fail-loud 路由不被 observer 改变）

#### 场景:observer 不被 loop 防御性包裹
- **当** observer 的 `on_event` 抛出异常
- **那么** loop **不**捕获该异常（无防御性 try/except）—— 异常按正常 Python 语义传播；observer 实现有责任保证 `on_event` 不抛（如 CLI observer 内部自吞渲染错误）
