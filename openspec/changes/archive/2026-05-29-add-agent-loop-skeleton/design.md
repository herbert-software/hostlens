## 上下文

M2.2 把已落地的两块拼起来：模型层（`LLMBackend` Protocol + `MessageResponse` + backend 异常族，`add-llm-backend-protocol` 已归档）与能力层（双层 Tool Registry + agent-surface `ToolsAdapter`，`add-tool-registry-capability-layer` 已归档）。缺的是中间那个手写循环。

约束（来自 CLAUDE.md / ARCHITECTURE.md §9）：

- **禁用 LangChain** —— 循环必须手写，且代码可读、注释 WHY（这是简历核心展示点）。
- **backend 是 `AgentLoop` 私有依赖，不进 `ToolContext`**（ADR-008）—— 防止 tool handler 拿到 backend 自己调 LLM，破坏「Inspector 不能调 LLM」红线。
- **`cache_control` 由 loop 端按 capability 决定注入**，backend 不静默丢（CLAUDE.md §4.8 / §4.11 规则 2）。
- **重试单一收口在 loop**（ADR-005）—— backend `max_retries=0` 且把 SDK 异常按域分类 raise，loop 按 §9 Failure Semantics 统一处理。

既有可消费的真实接口（已核对源码）：

- `LLMBackend.messages_create(*, model, system, messages, tools, max_tokens, timeout) -> MessageResponse`；`MessageResponse` 有 `.content: list[TextBlock|ToolUseBlock]` / `.stop_reason`（Literal：`end_turn`/`tool_use`/`max_tokens`/`stop_sequence`/`pause_turn`/`refusal`）/ `.usage`。
- `BackendCapabilities` 7 字段，含 `prompt_caching` / `parallel_tool_use`。
- 异常（实证 `anthropic_api.py` 的映射）：`BackendRateLimited(retry_after_seconds)`（**429 与 529 都走它**，429 带 retry-after、529 为 `None`）/ `BackendUnavailable`（**5xx 与连接超时/`APITimeoutError` 都走它**，二者不可区分）/ `BackendError(kind="auth_invalid")`（401/403）/ `BackendCapabilityViolation`。
- `ToolsAdapter.list_for_agent() -> list[dict[str,Any]]`；**`ToolsAdapter.dispatch(name, args_json: dict, ctx=None) -> dict[str,Any]`**（实证：返回 `result.model_dump()` dict，**不是** BaseModel）。dispatch 真实错误语义：① 步骤5 input schema 校验失败 raise `TypeError`（malformed model args，可回灌）；② handler 内部异常被 dispatch **捕获并 scrub** 成 `{"is_error": True, "error_kind", "tool_name", "message", "cause"}` envelope dict **返回**（不 raise）；③ `ToolPolicyViolation` / `KeyError` / `asyncio.CancelledError` **不 wrap，原样 raise**；④ 步骤7 output schema 校验失败 raise `ToolError`（handler 返回类型错误的代码 bug，fail-loud；本变更新增此契约，见 agent-tool-adapter spec）；⑤ `spec.timeout` 非空时 dispatch 自带 `asyncio.wait_for` 超时；⑥ dispatch 内部已用 `scrub_exception_message`（循环对 ② 的 envelope 不可二次 scrub）。
- `ToolContext` 字段集**恰好** 6 项（`target_registry`/`inspector_registry`/`config`/`logger`/`approval_service`/`cancel`），ADR-008 禁含 backend；`ToolsAdapter` 持 `context_factory`，`dispatch(ctx=None)` 时自建 ctx —— 故循环无需也不应自造 `ToolContext`。
- `AgentSettings`：`primary_model` / `max_turns`(1–100,默20) / `token_budget_input`(默100K) / `token_budget_output`(默30K) / `fallback_model`(本变更不用)。**注意 `Settings.agent: AgentSettings | None = None`** —— 默认可为 `None`（见 D-7）。
- `FakeBackend(*, responses: list[MessageResponse], capabilities=None)`：实证只按序返回响应、耗尽 raise `IndexError`，**无异常注入、无 call recording** —— 故障路径测试需本地 scripted backend double（见 D-9）。
- `Report.from_inspector_results(...)` 需 `list[InspectorResult]`(min 1)；但 `run_inspector` ToolSpec 只回 `RunInspectorOutput(target_name, inspector_name, findings)`，**不含** status/duration/missing（实证 `tools/schemas/run_inspector.py`）。

## 目标 / 非目标

**目标：**

- 一个可单测、可读、零框架依赖的 `AgentLoop`，把意图驱动的多轮 tool-use 循环跑通。
- 同 turn 内多 tool_use **并行 dispatch**。
- capability-gated `cache_control` 注入。
- token 预算 + max-turns 兜底，触发时**强制收尾**而非裸抛。
- 按 §9 表把 backend 故障映射到 `terminal_status` 字符串闭集。

**非目标：**

- 不组装 `Report`（见 D-2）；不写 Planner system prompt（M2.4）；不接 CLI（M2.7）；不做 prompt-cache 命中策略文档（M2.5）；不实现 fallback_model 降级、extended thinking、streaming、`ReportStatus` typed enum（M3）。

## 决策

### D-1：`run()` 返回通用 `LoopResult`，不返回 `Report`

ARCHITECTURE §9 的示意代码写 `async def run(self, intent) -> Report`，但那是**说明性草图**。落地时改为返回 `LoopResult`，理由：

1. **循环是通用 tool dispatcher**，按 `ToolUseBlock.name` 经 `ToolsAdapter.dispatch` 派发，**不特判** `run_inspector`。若 `run()` 要产 `Report`，循环就得知道「哪个工具产 finding」并把输出塞进 `Report` —— 这把通用循环耦合到具体工具语义。
2. **数据不足**：`Report.from_inspector_results` 需 `InspectorResult`（带 status/duration/missing），而 `run_inspector` 的 ToolSpec 输出 `RunInspectorOutput` 只有 findings。循环拿不到足够字段去无损构造 `Report`。
3. **分层更干净**：`LoopResult → Report` 的组装是 M2.4 Planner / M2.7 CLI 的职责（它们知道意图语义、知道如何聚合）。骨架保持「纯循环机制」。

`LoopResult`（Pydantic v2，frozen）字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `final_text` | `str` | 终止时 assistant 的 text block 拼接（可空） |
| `tool_invocations` | `list[ToolInvocation]` | 每次 dispatch 的 `(tool_name, tool_use_id, input, output_json \| error)` |
| `turns` | `int` | 实际跑的 turn 数 |
| `terminal_status` | `Literal[...]` | 7 值闭集（见 D-4） |
| `usage_totals` | `LoopUsage` | 累加的 input/output/cache_creation/cache_read token |
| `stop_reason` | `str \| None` | 最后一次响应的 stop_reason（兜底退出时可能为 None） |

**替代方案：** (A) 返回 `Report` → 否决（上述耦合 + 数据不足）。(B) 返回裸 `MessageResponse` → 否决（丢了多轮累积的 tool 输出与预算/turn 元数据，调用方无法判断是否降级）。

### D-2：`system` 由**构造期注入**，`cache_control` 只在其上、且只由 loop 注入

M2.2 不构造 system prompt 内容（那是 M2.4 Planner 的职责）；`system` 由调用方在**构造期**注入：`AgentLoop(backend, tool_adapter, settings, *, system=None)`，存为实例属性，`None` 视为 `[]`。选构造期（非 `run()` 参数）因为 system prompt 对一个 loop 实例固定，匹配 ARCHITECTURE 的 `self._system_prompt_*` 实例属性草图；M2.4 Planner 构造 loop 时传入其提示词。

loop 在每次调 backend 前：当 `capabilities.prompt_caching == True` 且 `system` 是**非空** `list[dict]` 时，在**最后一个** block 上加 `{"cache_control": {"type": "ephemeral"}}`；为 False（或 system 为空 / `str`）时原样透传。

理由：骨架不该决定「缓存哪些内容」（那是 M2.5 策略），只该保证「机制开关由 capability 驱动且对 backend 诚实」。backend 端 `check_capability_consistency` 是兜底；loop 端是第一道闸。**注意：若 system 为空（如调用方未注入），即使 prompt_caching=True 也无 block 可标 —— gate 逻辑正确但无副作用，直到 M2.4 注入真实 system prompt 才生效。**

**替代：** (A) `run(intent, *, system=...)` 运行期注入 → 否决（system 对一个 Agent 固定，运行期灵活性用不上，且与 ARCHITECTURE 实例属性草图不符）。(B) 由调用方传两份（plain / cached）system → 推迟到 M2.4（届时 Planner 拥有 system prompt 全文，按 §4.8 决定缓存边界）。

### D-3：重试在 loop 内，按 backend **异常类**分派（非 HTTP 状态）

关键认知：ARCHITECTURE §9 的 Failure Semantics 表是按 **HTTP 状态**（429/529/5xx/连接超时）逐行写的，但 backend 抽象（`anthropic_api.py` 实证）已把状态**收敛**成更小的异常集，循环只能在异常层面决策：

| §9 HTTP 行 | backend 抛的异常 | 循环可区分？ |
|---|---|---|
| 429 (带 retry-after) | `BackendRateLimited(retry_after_seconds=float)` | ✓ retry_after 非 None |
| 529 (overloaded) | `BackendRateLimited(retry_after_seconds=None)` | ✓ retry_after 为 None |
| 5xx | `BackendUnavailable` | ✗ 与连接超时无法区分 |
| 连接超时 | `BackendUnavailable` | ✗ 与 5xx 无法区分 |
| 401/403 | `BackendError(kind="auth_invalid")` | ✓ |

故循环的重试策略**按异常类**，不假装能复刻 §9 每个 HTTP 行（529 专项退避、连接超时的 manifest fallback 已列入非目标）：

- `BackendRateLimited` → `await asyncio.sleep(retry_after_seconds if not None else _FIXED_BACKOFF)`，最多 3 次；超限 → `degraded_rate_limited` 收尾。
- `BackendUnavailable` → 指数退避 1/4/16s，最多 3 次；超限 → 有 tool 结果则 `degraded_no_planner` 收尾，无则 `failed_api_unavailable`。
- `BackendError(kind="auth_invalid")`（及任何不可重试 kind）→ 不重试，原样向上抛（配置错误该让用户立刻看到）。
- `BackendCapabilityViolation` → 不重试，向上抛（这是 loop 自己的 bug，必须暴露）。

退避用 `asyncio.sleep`；重试次数与退避秒数是模块级常量并注释 WHY 对齐 §9 数值意图。**不引新依赖**（不用 tenacity）。

**替代：** 让 backend 暴露更细的异常子类（`BackendOverloaded` vs `BackendServerError` vs `BackendTimeout`）以复刻 §9 每行 → 否决：那要改已归档的 llm-backend-protocol 契约（超出本变更范围），且 §9 的 529-专项/manifest-fallback 行的价值在 M2.4+ 才体现（需 Planner / Inspector 列表）。骨架按现有异常集做**§9-一致但更粗**的策略，并把细分显式列为非目标。

### D-4：`terminal_status` 是 M2 字符串闭集，不是 enum

`Literal["ok", "degraded_rate_limited", "degraded_token_budget", "degraded_max_turns", "degraded_no_planner", "empty_response", "failed_api_unavailable"]`。这是 ARCHITECTURE §9 `meta.status` / `Run.status` 值的子集，M2 用 `Literal` 字符串承载；M3 `add-report-persistence-and-diff` 升级为 typed `ReportStatus` / `RunStatus` enum 时，这里的字符串值原样映射（值不变，只换载体）。

理由：M2 `Report.metadata` 是 `dict[str,str]`，没有 typed status 字段；提前引 enum 会和 M3 的 enum 设计撞车。`Literal` 给即时类型检查又不锁死 M3。

### D-5：并行 tool dispatch + 按 `dispatch` 真实契约分流错误

同 turn 多个 `ToolUseBlock` → `asyncio.gather(*[_dispatch_one(b) for b ...])`。

**关键前置：幻觉工具名在 loop 端用「名字成员检查」拦截，不靠 `KeyError`。** 因为 `dispatch` 的 `KeyError` 有歧义：既可能来自 step1 `registry.get(name)` 的查不到（实证 `registry.get` raise 裸 `KeyError`），也可能来自 handler 内部 re-raise 的裸 `KeyError`（实证 adapter step6 `except KeyError: raise`）—— 两者同类型不可区分。若把所有 `KeyError` 当幻觉工具名回灌，会**掩盖 handler 内部 bug**。解法：loop 用 `list_for_agent()` 已经拿到了 advertise 给模型的工具名集合，`_dispatch_one` 先做成员检查 ——

- `block.name ∉ advertised 名字集` → 这是模型幻觉的工具名，**不调用 dispatch**，直接产「无此工具 `<name>`」`is_error` tool_result 回灌让模型改用真实工具；记 `ToolInvocation(error=...)`。
- `block.name ∈ advertised 名字集` → 调 `dispatch(block.name, block.input)`（ctx 用 adapter 的 context_factory，循环不自造），按下面分流。

调用 dispatch 后的分流（逐路明确）：

1. **返回 dict 且不是 error envelope** → 该 dict 即 tool_result content（已 `model_dump()`）；记 `ToolInvocation(output=dict)`。
2. **返回 dict 且匹配 error envelope 签名**（handler 内部异常被 dispatch 捕获 + scrub 的 envelope）→ 映射成 Anthropic `tool_result(is_error=True)`，content 用该 envelope；记 `ToolInvocation(error=envelope)`。**循环不再 scrub**（dispatch 已 scrub）。
   - **envelope 判别用完整签名而非裸 `is_error`**：`isinstance(r, dict) and r.get("is_error") is True and "error_kind" in r and "message" in r`。理由：`ToolSpec.output_schema` 是任意 `BaseModel`，目前内置 3 个 schema（已核对 `tools/schemas/`）都无 `is_error` 字段，但契约层无保留字段约束，未来某业务工具输出恰含 `is_error=True` 会被裸判误伤；用 dispatch envelope 的固定 5 键签名（`is_error/error_kind/tool_name/message/cause`）的子集做判别更稳。
3. **dispatch raise `TypeError`**（模型给的 args 不过 input schema = malformed tool args）→ 循环捕获，`scrub_exception_message(str(exc))` 后作 `is_error` tool_result 回灌；记 `ToolInvocation(error=...)`。这是循环**唯一**需要自己 scrub 的路径。
4. **dispatch raise `KeyError`**（此时 name 已确认注册，故只可能是 handler 内部 bug）→ **不捕获，向上抛**（fail-loud，和 `ToolPolicyViolation` 同级，不掩盖代码 bug 为模型行为）。
5. **dispatch raise `ToolPolicyViolation`** → **不捕获，向上抛**。语义：循环只 advertise `list_for_agent()` 的工具，若其中之一在 dispatch 被 policy gate 拒（如 surface=agent 但 side_effects=write），是 registry/loop 配置 bug，必须 fail-loud。
5b. **dispatch raise `ToolError`**（output-schema 校验失败 = handler 返回类型错误的代码 bug）→ **不捕获，向上抛**。`ToolError` 不是 `TypeError` 子类，故循环的 `except TypeError` 天然不会误捕它。**注意区分**：input-malformed 是 `TypeError`（路径 3，回灌让模型自纠）；output-contract 是 `ToolError`（代码 bug，fail-loud）—— 二者用**异常类型**区分而非脆弱的 message 匹配（见 agent-tool-adapter spec 的 output-schema 失败需求）。
6. **`asyncio.CancelledError`** → 向上传播（协作取消，dispatch 已正确不吞）。

`gather` 不用 `return_exceptions=True`：路径 1–3 与幻觉拦截的 `_dispatch_one` 自己产出 tool_result dict 永不抛；路径 4–6（含 5b 的 `ToolError`）必须中断整轮。但 **`asyncio.gather` 默认只传播首个异常、不取消其余 sibling** —— 留下 orphaned 长跑 handler（仍在跑的 SSH/inspector 采集）泄漏资源。故 `_run_tool_turn` 必须用显式 `asyncio.create_task` 包裹每个 `_dispatch_one`，`gather` 抛异常时 `cancel()` 所有未完成 task 并 `await gather(..., return_exceptions=True)` drain，再 `raise` 原异常。**不用 `asyncio.TaskGroup`**：它会把异常包成 `ExceptionGroup`，破坏「run() 原样抛出 `ToolError`/`ToolPolicyViolation`」的 fail-loud 类型契约。每个 tool_result 用 `block.id` 作 `tool_use_id` 一一对应（`gather` 保序，结果顺序 = 输入 task 顺序）。

**替代：** 把路径 5 的 `ToolPolicyViolation` 也喂回模型 → 否决（掩盖配置 bug，模型对此无能为力）；把路径 3/4 的 `TypeError`/`KeyError` 也向上抛 → 否决（malformed args / 幻觉工具名是模型可自纠的正常 tool-use 现象，喂回比崩溃整轮更符合 §9「Malformed tool_use args → 回灌」）。

**tool_result content 序列化**：dispatch 返回 dict，但 Anthropic 的 `tool_result.content` 只接受字符串或 content-block 列表（`messages` 由 backend verbatim 透传给 SDK，loop 负责产出 SDK-valid 形态）。故 loop 把成功 dict / error envelope `json.dumps` 成文本承载进 `tool_result.content`；结构化 dict 仍原样存入 `ToolInvocation.output/error` 供调用方读取（二者分离：wire 形态给模型，结构化形态给程序）。

### D-6：token 预算与 max-turns 在「下一轮发起前」检查

每轮结束 `_track_token_usage` 后，进入下一轮前检查：累计 input `>=` `token_budget_input` 或累计 output `>=` `token_budget_output` → `degraded_token_budget` 收尾；turn 计数 ≥ `max_turns` → `degraded_max_turns` 收尾。**先收尾再返回**，绝不在超限后再发一次 `messages_create`（防止「检查完又烧一轮」）。

**`token_budget_output` 是 per-run 硬上限，必须逐轮收缩 `max_tokens`**：每次调 `messages_create` 传的 `max_tokens` = 剩余预算 `token_budget_output - usage.output_tokens`（兜底闸保证 > 0），**不是**每轮都传完整 `token_budget_output`。否则「单次 loop 预算上限」（ARCH §9）退化成 per-call 上限——某轮把 output 用到接近 budget 后，下一轮仍以完整 budget 再发一次，单次 run 输出最坏可达 ~2×budget。逐轮收缩使总输出严格 ≤ budget。守卫用 `>=`（非 `>`）以正确处理「恰好用满」边界。`_call_with_retry` 接受 `max_tokens` 参数（由 `run()` 按剩余预算算出传入），不再硬编码读 settings。

### D-7：`settings.agent is None` 在构造期 raise `ConfigError`

`Settings.agent` 默认 `None`（实证），但循环依赖 `settings.agent.{primary_model, max_turns, token_budget_*}`。选择**构造期校验**：`AgentLoop.__init__` 若 `settings.agent is None` 立即 raise `ConfigError`。

**替代：** (A) 内部默认到 `AgentSettings()` → 否决（静默用默认 model/预算掩盖了「用户没配 agent 节」，违反 §9 doctor 应能预检的精神）。(B) 在 `run()` 时才查 → 否决（构造一个不可用的 loop 是延迟失败，越早暴露越好）。

### D-8：全部 6 个 `stop_reason` 都有明确归属

`MessageResponse.stop_reason` 是 6 值 Literal，循环必须穷举（不留未定义分支）：

| stop_reason | 循环行为 | 依据 |
|---|---|---|
| `end_turn` | 有可用内容 → `ok`；content 空 → `empty_response` | §9 |
| `tool_use` | dispatch 工具 + continue | §9 |
| `refusal` | `empty_response`（§9「拒绝回答」行映射到 empty_response，**不**抛异常） | §9 |
| `max_tokens` | `degraded_token_budget` 收尾（单次输出被截断，等同预算耗尽语义） | §9 token 预算行 |
| `stop_sequence` | raise `UnexpectedStopReason`（Hostlens 不传 stop_sequences，出现即异常） | 防御 |
| `pause_turn` | raise `UnexpectedStopReason`（server-tool pause，Hostlens 不用） | 防御 |

**修正自初稿：** 初稿把 `refusal` 也归 `UnexpectedStopReason`，与 §9「拒绝回答 → empty_response」冲突；现 `refusal → empty_response`，`UnexpectedStopReason` 仅留给真正不该出现的 `stop_sequence`/`pause_turn`。

### D-9：故障路径测试用本地 scripted backend double，不扩展 `FakeBackend`

`FakeBackend` 实证只能按序返回 `MessageResponse`、耗尽 raise `IndexError`，**不能注入异常、不记录调用**。故：

- happy-path / 并行 / cache-gate / 兜底（靠 usage 数值触发）→ `FakeBackend(responses=[...])`，capability 通过构造参数覆盖。
- 故障注入（`BackendRateLimited`/`BackendUnavailable`/`BackendError` 序列）与「`messages_create` 调用次数」断言 → `tests/agent/` 内本地定义一个 `_ScriptedBackend`（实现 `LLMBackend` Protocol：按 `events: list[MessageResponse | Exception]` 依次返回或 raise，并自增 `calls` 计数 + 留存最后一次 `messages` 入参）。

**替代：** 给 `FakeBackend` 加异常注入 + call recording → 否决（扩大已发布测试夹具的契约面，且只有本变更需要；本地 double 局部、零外溢）。spec 的故障场景文字用「stub/测试 backend」而非点名 `FakeBackend`，与此一致。

## 风险 / 权衡

- **[LoopResult 不是 Report，调用方需自己组装]** → 缓解：D-1 已说明这是 M2.4/M2.7 的明确职责；spec 给出 `LoopResult` 完整字段契约，M2.4 据此组装无歧义。
- **[单 system block + loop 注入 cache_control 不够灵活]** → 缓解：M2.2 只需验证 gate 机制；M2.5 拥有 prompt 全文时再细化缓存边界。骨架接口不阻碍演进（system 入参形态不变）。
- **[重试退避常量硬编码]** → 缓解：与 ARCHITECTURE §9 表的数值对齐并注释；后续需要可参数化进 `AgentSettings`，不破坏现有签名。
- **[messages 历史无界增长]** → 缓解：token 预算兜底是事实上界；M3+ 视需要再加历史压缩，本变更不提前优化（§6 不写防御性 fallback）。
- **[`terminal_status` 字符串与 M3 enum 漂移]** → 缓解：D-4 锁定值集为 §9 子集，M3 迁移是「换载体不换值」；spec 把 7 个值写死为可测断言。
- **[`ToolsAdapter.dispatch` 成功结果与 error envelope 同为 `dict[str,Any]`，靠字段判别]** → 根因在已归档 adapter 契约的返回类型是裸 dict union，不是显式 `Ok | Err` 类型。缓解：D-5 路径 2 用完整 envelope 签名（多键）判别而非裸 `is_error`，且当前内置 schema 均无 `is_error` 字段（已核对）。见待解决问题第 3 条的根治方向。

## 迁移计划

纯新增，无迁移 —— 不改任何既有 spec / 公开 schema / 数据。回滚 = 删 `agent/loop.py` + `UnexpectedStopReason` + 相关测试，无遗留状态。分支 `feat/add-agent-loop-skeleton`，PR squash-merge 到 main。

## 待解决问题

- M2.5 落地时，`cache_control` 是否需要也标记到 `tools` 数组（tool schema 缓存）？本骨架先只做 system；待 M2.5 用真实/cassette backend 测 `cache_read_input_tokens > 0` 时定夺。
- `messages_create` 的 `timeout` 是否进 `AgentSettings`？M2.2 先硬编码 60.0 常量；若 M2.7 CLI 需要可配再提。
- **`ToolsAdapter.dispatch` 的 dict-union 返回类型根治**：是否在 tool-registry-capability-layer spec 给 `ToolSpec` 加「output schema 禁含保留字 `is_error`」约束，或把 dispatch 返回改为显式 `ToolResult | ToolErrorEnvelope` 判别类型？这要改已归档契约，超出本骨架范围 —— 留作后续 tool-registry 强化提案；本骨架先用多键签名判别 + 内置 schema 无冲突的事实兜住。
