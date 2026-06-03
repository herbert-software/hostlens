## 修改需求

### 需求:`MessageResponse` 必须是 Pydantic v2 BaseModel 镜像 Anthropic Message 关键字段

`hostlens.agent.backend.MessageResponse` 必须继承 `pydantic.BaseModel`，字段集为：

- `id: str`（Anthropic message id，如 `"msg_01..."`）
- `model: str`（实际使用的 model，如 `"claude-opus-4-7-20260301"`）
- `role: Literal["assistant"]`（Anthropic 响应始终 assistant role）
- `content: list[ContentBlock]`（discriminated union by `type` field）
- `stop_reason: Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "pause_turn", "refusal"]`
- `usage: Usage`

`ContentBlock` 必须用 Pydantic v2 显式 discriminated union 形式定义：`ContentBlock = Annotated[TextBlock | ToolUseBlock | ThinkingBlock | RedactedThinkingBlock, Field(discriminator="type")]`（**禁止**裸联合，因后者在 list 上下文中 discriminator 行为不严格，未知 `type` 字段错误消息不稳定）；union 成员为：

- `TextBlock(type: Literal["text"], text: str)`，`ConfigDict(extra="ignore")`
- `ToolUseBlock(type: Literal["tool_use"], id: str, name: str, input: dict[str, Any])`，`ConfigDict(extra="ignore")`
- `ThinkingBlock(type: Literal["thinking"], thinking: str, signature: str)`，`ConfigDict(extra="allow")`
- `RedactedThinkingBlock(type: Literal["redacted_thinking"], data: str)`，`ConfigDict(extra="allow")`

`ThinkingBlock` / `RedactedThinkingBlock` 必须用 `extra="allow"`（区别于 `TextBlock` / `ToolUseBlock` 的 `extra="ignore"`）：thinking 块是 Agent loop verbatim 回传的对象（Anthropic / DeepSeek 类端点在带工具多轮中要求 thinking 块按序原样回传，否则续轮 400），`extra="allow"` 保证 `model_dump()` 不丢弃 provider 私有字段，使回传逐字保真。`signature` 必须为 required `str`（实测 DeepSeek pro/flash 与 Anthropic 原生 ThinkingBlock 均带该字段；DeepSeek 的 `signature` 值恰为 message `id`，对 Hostlens 无差别——它只是「必须 verbatim 回传的字符串」）。建模这两个块的目的是**容忍** provider 强制吐的 thinking 而不崩、并支持多轮回传，**不**是主动请求或消费 thinking（后者属未来 Path 2）。

`Usage` 必须含至少 4 个字段：

- `input_tokens: int`
- `output_tokens: int`
- `cache_creation_input_tokens: int`（缺省 0；用于校验 prompt caching 生效）
- `cache_read_input_tokens: int`（缺省 0；用于校验 cache hit）

`MessageResponse` 自身配置 `ConfigDict(extra="ignore")` 允许 Anthropic SDK 未来新增**顶层**字段时不 fail-fast（与各 content block 的 per-block config 相互独立）。

#### 场景:`content` discriminator 工作

- **当** 构造 `MessageResponse(content=[{"type": "text", "text": "hi"}, {"type": "tool_use", "id": "x", "name": "y", "input": {}}], ...)`
- **那么** `response.content[0]` 必须是 `TextBlock` 实例；`response.content[1]` 必须是 `ToolUseBlock` 实例

#### 场景:thinking 块解析为 ThinkingBlock

- **当** 构造 `MessageResponse(content=[{"type": "thinking", "thinking": "...", "signature": "abc"}, {"type": "text", "text": "hi"}], ...)`
- **那么** `response.content[0]` 必须是 `ThinkingBlock` 实例，`response.content[0].signature == "abc"`，**禁止** raise `ValidationError`

#### 场景:redacted_thinking 块解析为 RedactedThinkingBlock

- **当** 构造 `MessageResponse(content=[{"type": "redacted_thinking", "data": "..."}], ...)`
- **那么** `response.content[0]` 必须是 `RedactedThinkingBlock` 实例（按 `type` 过滤只认 `"thinking"` 会丢 redacted、破坏多轮回传协议，故必须独立建模）

#### 场景:thinking 块 verbatim round-trip 保真

- **当** 用含 provider 私有额外字段的 thinking dict（如 `{"type":"thinking","thinking":"x","signature":"s","vendor_field":1}`）构造 `ThinkingBlock`，再 `model_dump()`
- **那么** dump 结果必须保留 `signature` 与 `vendor_field`（`extra="allow"` 不丢额外字段），使 Agent loop 回传时逐字保真；**禁止**把缺省字段补成 `null` 改变 wire 形状

#### 场景:未知 `type` 字段拒绝

- **当** 构造 `MessageResponse(content=[{"type": "unknown_block_type", ...}], ...)`（既非 text/tool_use，也非 thinking/redacted_thinking）
- **那么** 必须 raise `pydantic.ValidationError`（discriminated union 严格匹配；建模 thinking/redacted 不削弱对真正未知 block 的拒绝）

#### 场景:Anthropic SDK 新增字段不破坏解析

- **当** 输入 dict 含 `stop_sequence` / `type: "message"` / `container` 等 Anthropic API 已知字段，但 `MessageResponse` 未声明
- **那么** `MessageResponse.model_validate(...)` 必须 exit 0（不 raise），未声明顶层字段被静默丢弃

#### 场景:Anthropic SDK Message.model_dump() 字段对齐契约

- **当** 从 Anthropic SDK `anthropic.types.Message` 对象调用 `message.model_dump()` 得到 dict，再用 `MessageResponse.model_validate(dump)` 解析
- **那么** 必须成功解析；解析后 `result.id == message.id` / `result.model == message.model` / `result.role == "assistant"` / `result.stop_reason == message.stop_reason` / `result.content[i].type == message.content[i].type` / `result.content[i].text == message.content[i].text`（TextBlock）/ `result.content[i].id == message.content[i].id` 与 `result.content[i].name == message.content[i].name` 与 `result.content[i].input == message.content[i].input`（ToolUseBlock）/ `result.usage.input_tokens == message.usage.input_tokens` / `result.usage.output_tokens == message.usage.output_tokens` / `result.usage.cache_creation_input_tokens == message.usage.cache_creation_input_tokens or 0` / `result.usage.cache_read_input_tokens == message.usage.cache_read_input_tokens or 0`；该契约测试**必须**用真实 SDK 对象（直接构造 `anthropic.types.Message(...)` 实例或在 live smoke 中从真实 API 响应获取，**不**允许用手写 dict 模拟 SDK 形状）

#### 场景:`cache_read_input_tokens` 字段存在性

- **当** `response = MessageResponse.model_validate({"usage": {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}, ...})`
- **那么** `response.usage.cache_read_input_tokens == 0`（字段必须存在；不允许 backend 实现省略此字段）

### 需求:响应解析失败必须归一成 `BackendError` 并按成因区分 kind

`AnthropicAPIBackend.messages_create` 在把 SDK 响应转成 `MessageResponse`（`model_validate`）时，若抛 `pydantic.ValidationError`，必须捕获并包装成 `BackendError`（保留 `cause` + 诊断 `message`），**禁止**让裸 `ValidationError` 传播。

包装的**作用**是把第三方/SDK 异常**归一到 `BackendError` 故障域**：包装后的 `BackendError`（`kind` 不属可重试族）按 agent-loop spec「不可重试 backend 异常直接上抛」由 Agent loop **fail-loud 原样上抛**，最终由 CLI 边界呈现为一行错误（非 pydantic traceback）。归一的价值是「类型稳定 + `__str__` 已脱敏 + 携带结构化 kind」，**不是**「被 loop 优雅处理 / 分类重试」。

包装必须按成因区分 `kind`（**禁止**一刀切）：

- 若 `ValidationError` 命中 `content[*]` 的判别联合 discriminator 类错误（出现**真正未建模**的 block 类型；判别按「loc 落 `content` + discriminator 类错误，含 `union_tag_invalid` / `union_tag_not_found`」，不钉死单一 tag 名）→ `kind="unsupported_content_block"`。**注意**：`thinking` / `redacted_thinking` 现已纳入 `ContentBlock` 联合并能成功解析，**不再**触发本路径；本 kind 现专指「SDK / 端点未来新增的、Hostlens 尚未建模的其它 block type」（如某未来 `server_tool_use` 块），而非 thinking。
- 其它校验失败（字段缺失 / 新枚举 / 结构漂移等）→ `kind="invalid_response"`（**禁止**把这些误标成 `unsupported_content_block`，以免把 SDK 不兼容指向 thinking 问题）。一个 thinking 块缺 required `signature` 字段属此类（字段缺失，非未知 block type）→ `invalid_response`。

`kind` 是 `BackendError` 的自由字符串参数，新增取值**无需**修改 `core/exceptions.py`。

#### 场景:真正未建模块导致解析失败 → unsupported_content_block

- **当** backend 收到含 Hostlens 未建模的**新** content block（既非 text/tool_use，也非 thinking/redacted_thinking，如 `type="some_future_block"`）的响应，`model_validate` 因 `content[*]` 判别联合 tag 失败
- **那么** `messages_create` 必须 raise `BackendError(kind="unsupported_content_block")`（含 backend_name 与 cause）
- **并且** 禁止裸抛 `pydantic.ValidationError`

#### 场景:thinking 块不再触发 unsupported_content_block

- **当** backend 收到含 `type="thinking"`（或 `type="redacted_thinking"`）块的响应
- **那么** `model_validate` 必须成功解析为 `ThinkingBlock` / `RedactedThinkingBlock`，**禁止** raise `BackendError(kind="unsupported_content_block")`

#### 场景:非内容块的格式不符 → invalid_response

- **当** `model_validate` 因非 content-block 原因失败（如缺 `usage` 字段 / `stop_reason` 出现未知枚举 / thinking 块缺 required `signature`）
- **那么** `messages_create` 必须 raise `BackendError(kind="invalid_response")`（含 backend_name 与 cause）
- **并且** 禁止误标成 `unsupported_content_block`

### 需求:`BackendCapabilities` 必须声明 Agent loop 使用的能力开关

`hostlens.agent.backend.BackendCapabilities` 必须是 `@dataclass(frozen=True)` 不可变数据类，含且仅含以下 7 个 `bool` 字段：

- `prompt_caching`：`cache_control: ephemeral` block 是否真正生效
- `tool_use`：是否支持 Anthropic `tool_use` API
- `structured_output`：是否支持把 `tool_use` schema 当 structured output 用（Hostlens Planner 用此能力强制 JSON 输出）
- `parallel_tool_use`：是否支持单个 turn 内多个 `tool_use` 并行
- `extended_thinking`：是否支持 extended thinking（M3+ Diagnostician 可能用；**M2 范围内所有 backend 必须声明 False**，因 M2 `LLMBackend.messages_create` Protocol 签名不含 `thinking` 参数、Hostlens 既不主动请求也不消费 thinking；待 M3 Path 2 真正消费此能力时，同步扩展 Protocol 签名 + 所有 backend 实现。**注意**：`ContentBlock` union 现已含 `ThinkingBlock` / `RedactedThinkingBlock`（`tolerate-inbound-thinking` 的 Path 1 容忍切片），但「能解析/容忍 inbound thinking」与「`extended_thinking=True` 主动请求 thinking」是两件事——容忍是无条件扩 union、无 capability branch，故不改本字段取值）
- `vision`：是否支持图像输入（预留位，Hostlens 当前不用）
- `streaming`：是否支持流式响应（预留位，M2 范围内全 False）

字段集严格定型，**不允许**增加未在 Agent loop 真实消费的能力字段（按需扩展原则）。

#### 场景:字段集恰好 7 个

- **当** 调用 `dataclasses.fields(BackendCapabilities)`
- **那么** 返回恰好 7 个 field，名字与上述清单严格一致

#### 场景:不可变

- **当** 构造 `caps = BackendCapabilities(...)` 后试图 `caps.prompt_caching = False`
- **那么** 必须 raise `dataclasses.FrozenInstanceError`

#### 场景:全字段必填（无默认值）

- **当** 试图 `BackendCapabilities()`（不传任何参数）
- **那么** 必须 raise `TypeError`（强制 backend 实现显式声明每个能力，**禁止**用默认值掩盖未声明的能力）

### 需求:`AnthropicAPIBackend` 必须支持 `disable_thinking` 抑制开关

`AnthropicAPIBackend` 必须新增构造参数 `disable_thinking: bool = False`（在既有「`AnthropicAPIBackend` 必须完整实现 `LLMBackend` 且关闭 SDK 内部重试」需求所定义的构造签名之上扩展一个可选参数，默认值保证既有调用方不变），控制每次 `messages_create` 是否指示 provider 抑制 extended-thinking 输出。此开关是**可选的 token 节省优化**：对「thinking 默认开」的 anthropic 兼容端点（如 DeepSeek v4 经 `https://api.deepseek.com/anthropic`），开启可让 provider 不生成 thinking 输出从而省 input token。**它不再是兼容必需**——含 `type="thinking"` 内容块的响应现已由 `ContentBlock` union 建模并容忍（`tolerate-inbound-thinking` Path 1），即便 `disable_thinking=False`（不抑制）也能成功解析并多轮 verbatim 回传、不崩；`disable_thinking=True` 与「容忍」互补而非冲突（抑制省 token、容忍保健壮）。

`AnthropicAPIBackend` 必须遵守：

- `disable_thinking=True` 时，底层 SDK `messages.create` 调用必须带上 `extra_body={"thinking": {"type": "disabled"}}`
- `disable_thinking=False`（默认）时，**禁止**向 SDK 调用添加任何 thinking 相关字段（既不传 `extra_body` 的 thinking 字段、也不传原生 `thinking=` kwarg；真 Anthropic 请求形状逐字不变）
- 无论开关取值，`capabilities.extended_thinking` 必须保持 `False`（本开关抑制 provider 默认的 thinking，**不**启用 Hostlens 对 thinking 输出的主动请求/消费；容忍 inbound thinking 不需此字段为 True）
- `extra_body` 注入必须在 `check_capability_consistency(...)` 之后、SDK 调用入参组装时进行，且不影响 prompt_caching / tool_use 的 capability gate（gate 只扫 `system`/`messages`/`tools`，不扫 `extra_body`）
- `health_check` **不**注入 disabled（它只 ping、不调 `model_validate`，thinking 块不影响它）

#### 场景:disable_thinking 开启时注入 thinking:disabled

- **当** 调用 `AnthropicAPIBackend(disable_thinking=True).messages_create(...)`
- **那么** 底层 SDK `messages.create` 调用必须收到 `extra_body={"thinking": {"type": "disabled"}}`
- **并且** `check_capability_consistency(...)` 的调用时机与参数不受影响（注入发生在 capability gate 之后），且 `extra_body` 只含 `thinking` 键、不改写 `model`/`system`/`messages`/`tools`/`max_tokens`

#### 场景:disable_thinking 关闭时不改请求形状

- **当** 调用 `AnthropicAPIBackend(disable_thinking=False).messages_create(...)`（默认）
- **那么** 底层 SDK `messages.create` 调用**禁止**含任何 thinking 相关字段（`extra_body` 的 thinking 字段与原生 `thinking=` kwarg 都不出现）
- **并且** 请求形状必须与本变更前逐字一致

#### 场景:thinking 默认开的端点多轮 tool 循环可用（抑制路径）

- **当** `disable_thinking=True` 的 backend 指向一个 thinking 默认开的 anthropic 兼容端点，并跑多轮 tool 循环（tool_use → tool_result → 续轮）
- **那么** 每一轮响应必须只含 `text` / `tool_use` 块（无 `thinking`）
- **并且** 续轮**禁止**因缺失 thinking 块而返回 HTTP 400

#### 场景:不抑制时 inbound thinking 被容忍（容忍路径）

- **当** `disable_thinking=False` 的 backend 指向一个 thinking 默认开的 anthropic 兼容端点，provider 返回含 `type="thinking"` 块的响应
- **那么** `MessageResponse.model_validate` 必须成功解析（thinking 块解析为 `ThinkingBlock`），**禁止**因 thinking 块崩成 `BackendError(kind="unsupported_content_block")`；多轮回传该 thinking 块后续轮**禁止** 400（容忍是健壮路径，不依赖抑制）。注意：「成功解析」这一半是 CI deterministically 可证（mock/cassette replay）；「续轮不 400」依赖外部 provider 不验签、属外部不变量，由 `@pytest.mark.live` 门禁在接入/升级前覆盖，**CI 不证**
