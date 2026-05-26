# llm-backend-protocol 规范

## 目的
待定 - 由归档变更 add-llm-backend-protocol 创建。归档后请更新目的。
## 需求
### 需求:`LLMBackend` Protocol 必须是 Anthropic-schema-first 薄抽象

`hostlens.agent.backend.LLMBackend` 必须定义为 `typing.Protocol`，含以下成员：

- `name: str`（类属性；backend 标识符，如 `"anthropic_api"` / `"fake"` / `"playback"`）
- `capabilities: BackendCapabilities`（实例属性；详见后续 `BackendCapabilities` 需求）
- `async def messages_create(self, *, model: str, system: list[dict] | str, messages: list[dict], tools: list[dict], max_tokens: int, timeout: float) -> MessageResponse`（async 方法）

Protocol 约束：

- `system` / `messages` / `tools` 入参类型与 Anthropic Messages API 完全对齐（**禁止**自定义 Pydantic 类替代 dict 结构）
- backend 实现**必须严格透传**入参，不允许做静默规范化（如 strip 不支持字段）；不一致情况必须 raise 异常（详见 `BackendCapabilityViolation`）
- backend **禁止**实现 vendor-agnostic 泛化抽象（如把入参映射成 OpenAI Chat Completions 风格）
- 返回 `MessageResponse` Pydantic 模型，**不**返回原始 SDK 对象

#### 场景:Protocol 成员完整

- **当** 调用 `from hostlens.agent.backend import LLMBackend; LLMBackend.__protocol_attrs__`
- **那么** 必须含 `name` / `capabilities` / `messages_create` 三个名字（最少集；未来扩展按需添加）

#### 场景:Protocol 是 structural typing

- **当** 一个 class 实现了 `name` / `capabilities` / `messages_create` 三个成员但**不**显式继承 `LLMBackend`
- **那么** `isinstance(instance, LLMBackend)` 必须返回 True（`@runtime_checkable` Protocol 行为）

#### 场景:`messages_create` 是 async

- **当** 检查 `inspect.iscoroutinefunction(SomeBackend.messages_create)`
- **那么** 必须返回 True；调用方必须 `await` 之

#### 场景:`system` 入参支持 list[dict] 与 str 两种形式

- **当** Agent loop 调 `backend.messages_create(system="plain text", ...)` 或 `system=[{"type": "text", "text": "...", "cache_control": {"type": "ephemeral"}}]`
- **那么** backend 必须接受两种形式并透传给底层 SDK（**禁止**强制规范化为单一形式）

---

### 需求:`BackendCapabilities` 必须声明 Agent loop 使用的能力开关

`hostlens.agent.backend.BackendCapabilities` 必须是 `@dataclass(frozen=True)` 不可变数据类，含且仅含以下 7 个 `bool` 字段：

- `prompt_caching`：`cache_control: ephemeral` block 是否真正生效
- `tool_use`：是否支持 Anthropic `tool_use` API
- `structured_output`：是否支持把 `tool_use` schema 当 structured output 用（Hostlens Planner 用此能力强制 JSON 输出）
- `parallel_tool_use`：是否支持单个 turn 内多个 `tool_use` 并行
- `extended_thinking`：是否支持 extended thinking（M3+ Diagnostician 可能用；**M2 范围内所有 backend 必须声明 False**，因 M2 `LLMBackend.messages_create` Protocol 签名不含 `thinking` 参数且 `ContentBlock` union 不含 `ThinkingBlock`；待 M3 真正消费此能力时，同步扩展 Protocol 签名 + ContentBlock union + 所有 backend 实现）
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

---

### 需求:`MessageResponse` 必须是 Pydantic v2 BaseModel 镜像 Anthropic Message 关键字段

`hostlens.agent.backend.MessageResponse` 必须继承 `pydantic.BaseModel`，字段集为：

- `id: str`（Anthropic message id，如 `"msg_01..."`）
- `model: str`（实际使用的 model，如 `"claude-opus-4-7-20260301"`）
- `role: Literal["assistant"]`（Anthropic 响应始终 assistant role）
- `content: list[ContentBlock]`（discriminated union by `type` field）
- `stop_reason: Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "pause_turn", "refusal"]`
- `usage: Usage`

`ContentBlock` 必须用 Pydantic v2 显式 discriminated union 形式定义：`ContentBlock = Annotated[TextBlock | ToolUseBlock, Field(discriminator="type")]`（**禁止**裸 `TextBlock | ToolUseBlock` 联合，因后者在 list 上下文中 discriminator 行为不严格，未知 `type` 字段错误消息不稳定）；M2 范围 union 成员为：

- `TextBlock(type: Literal["text"], text: str)`
- `ToolUseBlock(type: Literal["tool_use"], id: str, name: str, input: dict[str, Any])`

`Usage` 必须含至少 4 个字段：

- `input_tokens: int`
- `output_tokens: int`
- `cache_creation_input_tokens: int`（缺省 0；用于校验 prompt caching 生效）
- `cache_read_input_tokens: int`（缺省 0；用于校验 cache hit）

模型配置 `ConfigDict(extra="ignore")` 允许 Anthropic SDK 未来新增字段时不 fail-fast。

#### 场景:`content` discriminator 工作

- **当** 构造 `MessageResponse(content=[{"type": "text", "text": "hi"}, {"type": "tool_use", "id": "x", "name": "y", "input": {}}], ...)`
- **那么** `response.content[0]` 必须是 `TextBlock` 实例；`response.content[1]` 必须是 `ToolUseBlock` 实例

#### 场景:未知 `type` 字段拒绝

- **当** 构造 `MessageResponse(content=[{"type": "unknown_block_type", ...}], ...)`
- **那么** 必须 raise `pydantic.ValidationError`（discriminated union 严格匹配）

#### 场景:Anthropic SDK 新增字段不破坏解析

- **当** 输入 dict 含 `stop_sequence` / `type: "message"` / `container` 等 Anthropic API 已知字段，但 `MessageResponse` 未声明
- **那么** `MessageResponse.model_validate(...)` 必须 exit 0（不 raise），未声明字段被静默丢弃

#### 场景:Anthropic SDK Message.model_dump() 字段对齐契约

- **当** 从 Anthropic SDK `anthropic.types.Message` 对象调用 `message.model_dump()` 得到 dict，再用 `MessageResponse.model_validate(dump)` 解析
- **那么** 必须成功解析；解析后 `result.id == message.id` / `result.model == message.model` / `result.role == "assistant"` / `result.stop_reason == message.stop_reason` / `result.content[i].type == message.content[i].type` / `result.content[i].text == message.content[i].text`（TextBlock）/ `result.content[i].id == message.content[i].id` 与 `result.content[i].name == message.content[i].name` 与 `result.content[i].input == message.content[i].input`（ToolUseBlock）/ `result.usage.input_tokens == message.usage.input_tokens` / `result.usage.output_tokens == message.usage.output_tokens` / `result.usage.cache_creation_input_tokens == message.usage.cache_creation_input_tokens or 0` / `result.usage.cache_read_input_tokens == message.usage.cache_read_input_tokens or 0`；该契约测试**必须**用真实 SDK 对象（直接构造 `anthropic.types.Message(...)` 实例或在 live smoke 中从真实 API 响应获取，**不**允许用手写 dict 模拟 SDK 形状）

#### 场景:`cache_read_input_tokens` 字段存在性

- **当** `response = MessageResponse.model_validate({"usage": {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}, ...})`
- **那么** `response.usage.cache_read_input_tokens == 0`（字段必须存在；不允许 backend 实现省略此字段）

---

### 需求:`BackendDiagnostics` 必须是独立可选 Protocol（duck-type）

`hostlens.agent.backend.BackendDiagnostics` 必须定义为独立 `@runtime_checkable typing.Protocol`（**不**与 `LLMBackend` 继承/组合；`@runtime_checkable` 装饰器**必填**，否则 `isinstance(backend, BackendDiagnostics)` 会 raise `TypeError`），含以下成员：

- `async def health_check(self) -> BackendHealth`
- `async def quota_check(self) -> QuotaStatus | None`（返回 None 表示该 backend 不支持配额探测）
- `def ensure_safe_for_daemon(self) -> None`（不安全场景 raise `BackendDaemonUnsafe`；no-op 默认行为）

`BackendHealth` Pydantic 模型字段：

- `is_healthy: bool`
- `backend_name: str`
- `latency_ms: float | None`（最近一次 ping 延迟）
- `error: str | None`（不健康时的脱敏错误消息）

`QuotaStatus` Pydantic 模型字段：

- `remaining_input_tokens: int | None`
- `remaining_output_tokens: int | None`
- `reset_at: datetime | None`

`hostlens doctor` 命令必须 duck-type 检测 backend 是否实现 `BackendDiagnostics`，是则调 `health_check`；**禁止**强制所有 backend 实现 diagnostics。

#### 场景:`AnthropicAPIBackend` 实现 BackendDiagnostics

- **当** 构造 `backend = AnthropicAPIBackend(...)` 后调 `isinstance(backend, BackendDiagnostics)`
- **那么** 必须返回 True

#### 场景:`PlaybackBackend` 不实现 BackendDiagnostics

- **当** 构造 `backend = PlaybackBackend(...)` 后调 `isinstance(backend, BackendDiagnostics)`
- **那么** 必须返回 False（cassette 模式无真实健康概念）

#### 场景:`ensure_safe_for_daemon` 默认 no-op

- **当** `backend = AnthropicAPIBackend(...)` 且 `is_daemon_mode(settings) == True`，调 `backend.ensure_safe_for_daemon()`
- **那么** 必须正常返回 None（API key 在 daemon 模式安全）

#### 场景:`isinstance` 不抛 TypeError

- **当** 调 `isinstance(some_backend, BackendDiagnostics)`
- **那么** 必须正常返回 bool（**禁止** raise `TypeError`）；此场景保证 `@runtime_checkable` 装饰器正确加载

---

### 需求:`AnthropicAPIBackend` 必须完整实现 `LLMBackend` 且关闭 SDK 内部重试

`hostlens.agent.backends.anthropic_api.AnthropicAPIBackend` 必须：

- 实现 `LLMBackend` Protocol 全部成员
- 实现 `BackendDiagnostics` Protocol 全部成员
- `name = "anthropic_api"` 类属性
- `capabilities = BackendCapabilities(prompt_caching=True, tool_use=True, structured_output=True, parallel_tool_use=True, extended_thinking=False, vision=True, streaming=False)` 类属性
- 构造 `anthropic.AsyncAnthropic` client 时**必须**显式 `max_retries=0` 关闭 SDK 内部重试
- `messages_create` 把 Anthropic SDK 异常包装成 backend 层异常（**异常构造与字段访问必须对齐 SDK 真实 API**：`RateLimitError` 继承 `APIStatusError`，构造签名 `(message, *, response, body)`，状态从 `exc.status_code`，retry-after 从 `exc.response.headers.get("retry-after")` 读，转 float；529 在 SDK 中映射为 `anthropic.OverloadedError`，同样继承 `APIStatusError` 且 `status_code == 529`）：
  - `anthropic.RateLimitError`（429）→ 从 `exc.response.headers.get("retry-after")` 读 retry-after 转 float（缺省 None），raise `BackendRateLimited(backend_name="anthropic_api", retry_after_seconds=<value>, cause=exc)`
  - `anthropic.OverloadedError` 或其他 `anthropic.APIStatusError` 且 `exc.status_code == 529` → raise `BackendRateLimited(backend_name="anthropic_api", retry_after_seconds=None, cause=exc)`
  - 其他 `anthropic.APIStatusError`（5xx 非 529）→ raise `BackendUnavailable(backend_name="anthropic_api", cause=exc)`
  - `anthropic.APIConnectionError` / `anthropic.APITimeoutError` → raise `BackendUnavailable(backend_name="anthropic_api", cause=exc)`
  - `anthropic.AuthenticationError` → raise `BackendError(backend_name="anthropic_api", kind="auth_invalid", cause=exc)`
- `health_check` 调一次 `messages.create`，model 入参**必须**从构造时注入的 `health_check_model: str` 字段读取（构造签名扩展为 `__init__(self, *, api_key: str, base_url: str | None = None, health_check_model: str = "claude-haiku-4-5")` —— 默认走 Haiku 最便宜的 model 探测连通性，不走 primary Opus；调用方如需自定义可在 `create_backend` 时按 `settings.agent.primary_model` 或 fallback_model 显式覆盖）；其余入参 `messages=[{"role": "user", "content": "ping"}], max_tokens=10`；成功返回 `BackendHealth(is_healthy=True, ...)`，失败返回 `BackendHealth(is_healthy=False, error=<scrubbed>, ...)`
- `quota_check` M2 范围**必须**返回 `None`（Anthropic Console quota API 未公开标准接口）
- `ensure_safe_for_daemon` no-op（API key 在 daemon 模式安全）

#### 场景:SDK client `max_retries=0`

- **当** 构造 `backend = AnthropicAPIBackend(api_key="...", ...)`，访问 `backend._client.max_retries`（或等价的 SDK 内部属性）
- **那么** 必须为 0（**禁止**使用 SDK 默认重试）

#### 场景:capabilities 全字段声明

- **当** 访问 `AnthropicAPIBackend.capabilities`
- **那么** 必须等于 `BackendCapabilities(prompt_caching=True, tool_use=True, structured_output=True, parallel_tool_use=True, extended_thinking=False, vision=True, streaming=False)`（`extended_thinking` / `streaming` 必须 False —— M2 Protocol 签名不含 `thinking` 参数与流式响应）

#### 场景:429 包装成 BackendRateLimited

- **当** SDK 抛 `anthropic.RateLimitError(message="rate limited", response=httpx.Response(429, headers={"retry-after": "30"}, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")), body=None)`，调用 `backend.messages_create(...)`
- **那么** 必须 raise `BackendRateLimited`，且 `exc.retry_after_seconds == 30.0`；实现路径必须经 `exc.response.headers.get("retry-after")` 读取（不经 SDK message 字符串解析）；**禁止** backend 内部重试

#### 场景:529 无 retry-after 包装成 BackendRateLimited

- **当** SDK 抛 `anthropic.OverloadedError(message="overloaded", response=httpx.Response(529, request=...), body=None)`（或任意 `APIStatusError` 子类满足 `exc.status_code == 529`），不带 retry-after header
- **那么** 必须 raise `BackendRateLimited`，且 `exc.retry_after_seconds is None`

#### 场景:其他 5xx 包装成 BackendUnavailable

- **当** SDK 抛 `anthropic.APIStatusError`（status_code ∈ {500, 502, 503, 504}），不是 429 也不是 529
- **那么** 必须 raise `BackendUnavailable`，`exc.__cause__` 链回原 SDK 异常

#### 场景:网络错误包装成 BackendUnavailable

- **当** SDK 抛 `anthropic.APIConnectionError(...)` 或 `anthropic.APITimeoutError(...)`
- **那么** 必须 raise `BackendUnavailable`，且 `exc.__cause__` 链回原 SDK 异常

#### 场景:认证错误包装成 BackendError

- **当** SDK 抛 `anthropic.AuthenticationError(...)`
- **那么** 必须 raise `BackendError`，`exc.kind == "auth_invalid"`，message 中**禁止**含 api_key 完整值（仅含前 4 + 后 4 字符指纹形式）

#### 场景:health_check 成功

- **当** `await backend.health_check()` 在 API 可用时
- **那么** 返回 `BackendHealth(is_healthy=True, backend_name="anthropic_api", latency_ms=<float>, error=None)`

#### 场景:health_check 失败时脱敏

- **当** `await backend.health_check()` 在 API 401 时
- **那么** 返回 `BackendHealth(is_healthy=False, ...)`，且 `error` 字段**禁止**含 api_key 原值（如有，必须替换为 `***`）

---

### 需求:`FakeBackend` 必须支持顺序响应 + capability 自定义

`hostlens.agent.backends.fake.FakeBackend` 必须：

- 实现 `LLMBackend` Protocol 全部成员
- `name = "fake"` 类属性
- 构造签名 `__init__(self, *, responses: list[MessageResponse], capabilities: BackendCapabilities | None = None)`
- `capabilities` 默认值 = `BackendCapabilities(prompt_caching=True, tool_use=True, structured_output=True, parallel_tool_use=True, extended_thinking=False, vision=True, streaming=False)`（**`extended_thinking` / `streaming` 必须 False**，与 `AnthropicAPIBackend` 默认对齐；方便测正常 path 同时不出现 Protocol 签名不支持的 capability）
- 内部维护 `_response_idx: int = 0`，每次 `messages_create` 返回 `responses[self._response_idx]` 并 `_response_idx += 1`
- 当 `_response_idx >= len(responses)` 时 raise `IndexError("FakeBackend exhausted: ...")`
- 不实现 `BackendDiagnostics`（测试 backend 无真实健康概念）

#### 场景:顺序返回响应

- **当** 构造 `FakeBackend(responses=[r1, r2, r3])` 后调用 `messages_create` 3 次
- **那么** 依次返回 `r1` / `r2` / `r3`

#### 场景:耗尽 raise IndexError

- **当** 构造 `FakeBackend(responses=[r1])` 后调用 `messages_create` 2 次
- **那么** 第 2 次必须 raise `IndexError`，消息含 `"FakeBackend exhausted"` 子串

#### 场景:capability 自定义

- **当** 构造 `FakeBackend(responses=[r], capabilities=BackendCapabilities(prompt_caching=False, ...))`
- **那么** `backend.capabilities.prompt_caching == False`（用于测试 capability gate 路径）

#### 场景:capability 默认对齐 AnthropicAPIBackend

- **当** 构造 `FakeBackend(responses=[r])` 不传 `capabilities`
- **那么** `backend.capabilities` 必须等于 `BackendCapabilities(prompt_caching=True, tool_use=True, structured_output=True, parallel_tool_use=True, extended_thinking=False, vision=True, streaming=False)`（`extended_thinking` / `streaming` 均 False，与 `AnthropicAPIBackend` 一致 —— M2 Protocol 签名不含相应参数）

---

### 需求:`PlaybackBackend` 必须从 JSON Lines cassette 回放且 miss 时 fail-fast

`hostlens.agent.backends.playback.PlaybackBackend` 必须：

- 实现 `LLMBackend` Protocol 全部成员
- `name = "playback"` 类属性
- `capabilities` **固定**为 `BackendCapabilities(prompt_caching=True, tool_use=True, structured_output=True, parallel_tool_use=True, extended_thinking=False, vision=True, streaming=False)`（M2 范围不支持 cassette metadata 行解析；与 `AnthropicAPIBackend` / `FakeBackend` 默认对齐让 capability gate 行为一致；未来若需 cassette 模拟其他 capability 组合，再扩 metadata 协议）
- 构造签名 `__init__(self, *, cassette_path: Path)`
- 启动时加载 cassette 文件（JSON Lines，每行 `{"request": {...}, "response": {...}, "tools_schema_hash": "<hex>"?}`；`tools_schema_hash` 是可选 lint-only metadata，运行时**不**参与匹配）
- `messages_create` 计算 request key（按 `SHA256(json.dumps({"model": ..., "messages": ..., "tools_count": len(tools)}, sort_keys=True))` 算法），在 cassette 中查 key。**key 排除的字段**（conscious trade-off）：(a) `system`（system prompt 在 M2 范围内由 Agent loop 单一来源生成且其内容稳定；让 system 变化触发 cassette miss 会让任何 prompt iteration 都报错；测试如需断言 system 变化必须独立配 cassette）(b) `max_tokens`（与响应等价性无关；不同 max_tokens 同样 cassette 可复用）(c) `tools` 内容（仅 `tools_count` 入 key，drift 检测由 `tools_schema_hash` lint-only metadata 负责，见后述）(d) `timeout`（client 侧参数，不影响响应）；任何要求严格匹配 system / max_tokens / tools schema 的测试场景必须显式说明并独立录制 cassette。
- 找到匹配 → 返回 `MessageResponse.model_validate(record["response"])`
- 找不到匹配 → **必须** raise `CassetteMiss(request_key=<hash>, cassette_path=<relative path>)`；**禁止**回落到真实 API 调用（即使 `ANTHROPIC_API_KEY` 在环境变量中）
- **trade-off 与 schema drift 检测**：`tools_count` 而非 tools 完整哈希作为 key 是 conscious trade-off（tools schema 微小变化不让所有 cassette miss）；为防 schema drift 让 cassette 失实，cassette record **可选**含 `tools_schema_hash` 字段（`SHA256(json.dumps(tools, sort_keys=True))` 在录制时的值）；`scripts/cassette_lint.py --check-schema-drift --current-tools-hash <hex>` 把 CLI 传入的当前 tool schema hash 与 cassette 内存档对比，drift 时输出 warning（不 fail；让开发者评估是否需要重录）；M2 范围内 `--current-tools-hash` 由 CI 显式传入（CI 在调用 cassette_lint 前用 `python -c "import json, hashlib; from hostlens.tools import ToolRegistry, register_default_tools; r = ToolRegistry(); register_default_tools(r); tools = adapter.list_for_agent(); print(hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest())"` 算出值），**不**在 cassette_lint 内部 import ToolRegistry（避免 cassette_lint 引入业务包依赖；保持 lint 工具独立）
- 不实现 `BackendDiagnostics`

`CassetteMiss` 异常必须继承 `BackendError`，构造签名 `__init__(self, *, request_key: str, cassette_path: str)`；内部**必须**调用 `super().__init__(backend_name="playback", kind="cassette_miss", cause=None)` 以满足 `BackendError` 基类的 `backend_name` 必填要求；字段 `request_key: str` 与 `cassette_path: str` 挂在实例上（相对路径，**不**含绝对路径完整值）；`isinstance(exc, BackendError)` 与 `isinstance(exc, HostlensError)` 均必须为 True。

#### 场景:正常回放

- **当** cassette 含 1 条 record，request key 与调用入参匹配
- **那么** `messages_create` 返回 `MessageResponse` 与 cassette 内 response 字段一致

#### 场景:miss raise CassetteMiss

- **当** 调用入参的 request key 不在 cassette 中
- **那么** 必须 raise `CassetteMiss`；`exc.request_key` 含 SHA256 hash（截断到 16 字符的可读形式）；`exc.cassette_path` 是相对路径

#### 场景:miss 时禁止回落真实 API

- **当** miss 发生且环境变量 `ANTHROPIC_API_KEY="sk-xxx"` 已设
- **那么** **仍**必须 raise `CassetteMiss`（**禁止**调真实 Anthropic API）；通过 unit test 用 mock anthropic SDK 拦截验证（如真打到 SDK 必 fail）

#### 场景:cassette 文件 JSON 格式校验

- **当** cassette 文件含一行 invalid JSON
- **那么** `PlaybackBackend(cassette_path=...)` 构造时必须 raise `ValueError("invalid cassette format at line N")`

#### 场景:`tools_schema_hash` lint-only drift warning

- **当** cassette record 含 `tools_schema_hash="abc..."`，调用 `scripts/cassette_lint.py --check-schema-drift --current-tools-hash xyz...`（不同 hash）
- **那么** stdout 必须输出 `WARNING: tools_schema_hash drift in cassette <path>: cassette=abc... current=xyz...`；**不** exit 1（只 warning，不 fail）

#### 场景:`--check-schema-drift` 缺 `--current-tools-hash` 参数

- **当** 调用 `scripts/cassette_lint.py --check-schema-drift`（未传 `--current-tools-hash`）
- **那么** 必须 exit 2 + stderr 输出 `--current-tools-hash required when using --check-schema-drift`（fail-fast，不静默跳过）

---

### 需求:`BackendCapabilityViolation` 必须在 `cache_control` 与 capability 不一致时 raise

任何 backend 实现 `messages_create` 时必须递归扫描 `system` / `messages[*].content[*]` / `tools[*]` 三处入参中是否含 `cache_control` block；若任一位置含 `cache_control` 且 `self.capabilities.prompt_caching == False`，**必须** raise `BackendCapabilityViolation`；**禁止**静默丢弃 `cache_control` 字段或假装成功返回。

注：Anthropic Messages API 的 `cache_control` block 可出现在三个位置 —— (a) `system` 入参（当 `system` 是 `list[dict]` 形式，每个 block 可带 `cache_control`）(b) `messages[*].content[*]`（同样 list 形式时每个 content block 可带）(c) `tools[*]` 数组中的每个 `ToolParam`（可带 `cache_control`，用于缓存 tool definitions）。capability gate 必须三处都覆盖。

`BackendCapabilityViolation` 必须继承 `BackendError`（与本提案 §需求:Backend 异常体系 §异常体系汇总一致；间接继承 `HostlensError`，因 `BackendError(HostlensError)`），字段：

- `backend_name: str`（由 `BackendError` 基类要求）
- `capability: Literal["prompt_caching", "tool_use", "structured_output", "parallel_tool_use", "extended_thinking", "vision", "streaming"]`
- `attempted_feature: Literal["cache_control_in_system_block", "cache_control_in_messages_block", "cache_control_in_tools_array", "tools_array_non_empty"]`（**受约束 Literal 取值域**，禁止自由文本以防 prompt/log injection；新增取值必须同步更新 Literal 与正则）

#### 场景:prompt_caching=False + system cache_control raise

- **当** backend `capabilities.prompt_caching == False`，调 `messages_create(system=[{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}], ...)`
- **那么** 必须 raise `BackendCapabilityViolation`，`exc.capability == "prompt_caching"`，`exc.attempted_feature == "cache_control_in_system_block"`

#### 场景:prompt_caching=False + messages cache_control raise

- **当** backend `capabilities.prompt_caching == False`，调 `messages_create(messages=[{"role": "user", "content": [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}]}], ...)`
- **那么** 必须 raise `BackendCapabilityViolation`，`exc.capability == "prompt_caching"`，`exc.attempted_feature == "cache_control_in_messages_block"`

#### 场景:prompt_caching=False + tools cache_control raise

- **当** backend `capabilities.prompt_caching == False`，调 `messages_create(tools=[{"name": "x", "input_schema": {}, "cache_control": {"type": "ephemeral"}}], ...)`
- **那么** 必须 raise `BackendCapabilityViolation`，`exc.capability == "prompt_caching"`，`exc.attempted_feature == "cache_control_in_tools_array"`

#### 场景:tool_use=False + tools 非空 raise

- **当** backend `capabilities.tool_use == False`，调 `messages_create(tools=[{"name": "x", ...}], ...)`
- **那么** 必须 raise `BackendCapabilityViolation`，`exc.capability == "tool_use"`，`exc.attempted_feature == "tools_array_non_empty"`

#### 场景:`AnthropicAPIBackend` 正常 path 不触发

- **当** `AnthropicAPIBackend`（`prompt_caching=True`）收到 `cache_control` block 在任意位置（system / messages / tools）
- **那么** 必须正常透传给 SDK，不 raise `BackendCapabilityViolation`

#### 场景:attempted_feature 字段受约束

- **当** 试图构造 `BackendCapabilityViolation(backend_name="x", capability="prompt_caching", attempted_feature="cache_control; rm -rf /")`
- **那么** 必须 raise `ValueError`（**禁止** Literal 集合外的值）

---

### 需求:`create_backend` 工厂必须按 `Settings.backend.type` 分派 + daemon-safe 守门

`hostlens.agent.backend.create_backend(settings: Settings) -> LLMBackend` 必须：

- 读取 `settings.backend.type` 字段
- 按以下映射构造 backend 实例（**所有 `SecretStr` 字段必须经 `.get_secret_value()` 解包**，**禁止**直接传 `SecretStr` 对象或 `str(secret)`，否则 SDK 会拿到脱敏后的 `"**********"` 字符串）：
  - `"anthropic_api"` → 校验 `settings.backend.api_key is not None`（否则 raise `ConfigError`），随后 `AnthropicAPIBackend(api_key=settings.backend.api_key.get_secret_value(), base_url=str(settings.backend.base_url) if settings.backend.base_url else None, health_check_model=settings.agent.health_check_model if settings.agent else "claude-haiku-4-5")`（`health_check_model` 默认走 Haiku 4.5，最便宜；用户可在 `agent.health_check_model` 配置覆盖；`settings.agent` 缺省时回落默认值）
  - `"fake"` → `FakeBackend(responses=[])`（M2 范围 fake backend 不从 config 读 responses，由测试 fixture 构造）
  - `"playback"` → 校验 `settings.backend.cassette_path is not None`（否则 raise `ConfigError`），随后 `PlaybackBackend(cassette_path=settings.backend.cassette_path)`
  - `"bedrock"` / `"vertex"` / `"claude_subscription"` → raise `NotImplementedError("backend type X 将在 M10.5 / 1.0 落地；当前请使用 anthropic_api")`
- 构造完成后**必须**调 `is_daemon_mode(settings)` 与 `backend.ensure_safe_for_daemon()`（如 backend 实现 `BackendDiagnostics`）
- 若 `ensure_safe_for_daemon` raise `BackendDaemonUnsafe`，则 `create_backend` 必须**不**捕获，让异常向上传播

#### 场景:`anthropic_api` 分派

- **当** `settings.backend.type == "anthropic_api"` 且 `api_key` 非空，调 `create_backend(settings)`
- **那么** 返回 `AnthropicAPIBackend` 实例；`backend.name == "anthropic_api"`；构造时传入的 `api_key` 参数必须是 `SecretStr.get_secret_value()` 解包后的真实 `str`（用 mock SDK 拦截 `AsyncAnthropic.__init__` 入参验证非 `"**********"` 占位字符串）

#### 场景:`anthropic_api` 缺 api_key raise ConfigError

- **当** `settings.backend.type == "anthropic_api"` 且 `api_key is None`，调 `create_backend(settings)`
- **那么** 必须 raise `ConfigError`，消息含 `"api_key required"` 子串（此校验与 `BackendSettings.@model_validator` 双重保险；后者在配置加载时已校验，此处守 fallback path）

#### 场景:`bedrock` raise NotImplementedError

- **当** `settings.backend.type == "bedrock"`，调 `create_backend(settings)`
- **那么** 必须 raise `NotImplementedError`，消息含 `"M10.5"` 子串

#### 场景:`playback` 缺 cassette_path raise ConfigError

- **当** `settings.backend.type == "playback"` 且 `settings.backend.cassette_path is None`
- **那么** 必须 raise `ConfigError`，消息含 `"cassette_path required"` 子串

#### 场景:daemon-safe 守门触发

- **当** `is_daemon_mode(settings) == True` 且 backend.`ensure_safe_for_daemon` raise `BackendDaemonUnsafe`
- **那么** `create_backend` 必须不捕获，让 `BackendDaemonUnsafe` 向上传播

---

### 需求:`is_daemon_mode` M2 stub 必须返回 False

`hostlens.agent.backend.is_daemon_mode(settings: Settings) -> bool` 必须在 M2 范围内永远返回 False；函数签名稳定，M5 Scheduler 落地时改实现不动调用点。

#### 场景:M2 始终 False

- **当** 任意 `settings` 调 `is_daemon_mode(settings)`
- **那么** 必须返回 False

#### 场景:函数签名稳定

- **当** 检查 `inspect.signature(is_daemon_mode)`
- **那么** 参数恰好为 `settings: Settings`，返回值类型注解为 `bool`

---

### 需求:Backend 异常体系必须按故障域结构化

`hostlens.core.exceptions` 必须新增以下 5 个异常类（`BackendError` 直接继承 `HostlensError`；其余 4 个子类继承 `BackendError`，间接继承 `HostlensError`；`isinstance(exc, HostlensError)` 对所有 5 个子类都为 True）：

- `BackendError(HostlensError)`：backend 通信错误基类；字段 `backend_name: str`、可选 `kind: str` 与 `cause: Exception | None`
- `BackendUnavailable(BackendError)`：网络 / 5xx / 完全宕机
- `BackendRateLimited(BackendError)`：429 / 529 / 订阅软限制；字段 `retry_after_seconds: float | None`
- `BackendCapabilityViolation(BackendError)`：capability 与请求不一致（详见前文需求）
- `BackendDaemonUnsafe(BackendError)`：`ensure_safe_for_daemon()` 拒绝；字段 `reason: str`（受约束 Literal 防 injection）

加上之前 M0 / M2 tool registry 已落地的 6 个异常（`HostlensError` / `ConfigError` / `TargetError` / `InspectorError` / `ToolError` / `ToolPolicyViolation`），本提案完成后 `hostlens.core.exceptions.__all__` 必须含**恰好 11 个**符号。

#### 场景:异常继承链

- **当** 构造 `BackendRateLimited(backend_name="anthropic_api", retry_after_seconds=30)`
- **那么** `isinstance(exc, BackendError)` 与 `isinstance(exc, HostlensError)` 必须均为 True

#### 场景:`retry_after_seconds` 字段保留

- **当** `exc = BackendRateLimited(backend_name="x", retry_after_seconds=30.5)`
- **那么** `exc.retry_after_seconds == 30.5`

#### 场景:`retry_after_seconds` 可为 None

- **当** `exc = BackendRateLimited(backend_name="x", retry_after_seconds=None)`
- **那么** `exc.retry_after_seconds is None`（用于 529 / 订阅软限制等无 retry-after header 场景）

#### 场景:`__all__` 恰好 11 个

- **当** 调用 `from hostlens.core.exceptions import __all__`
- **那么** `len(__all__) == 11`，且 `sorted(__all__)` 等于 `sorted(["HostlensError", "ConfigError", "TargetError", "InspectorError", "ToolError", "ToolPolicyViolation", "BackendError", "BackendUnavailable", "BackendRateLimited", "BackendCapabilityViolation", "BackendDaemonUnsafe"])`

#### 场景:异常 `__str__` 不泄露敏感信息

- **当** `exc = BackendError(backend_name="anthropic_api", kind="auth_invalid", cause=Exception("Invalid API key sk-ant-<abcdefghijklmn>"))`，调 `str(exc)`
- **那么** 输出**禁止**含 `sk-ant-<abcdefghijklmn>` 子串（cause 消息必须经 OPERABILITY.md §7.2 脱敏规则过滤后再嵌入）

#### 场景:`__str__` 字段白名单，不 dump SDK exception 对象

- **当** `exc = BackendError(backend_name="x", kind="auth_invalid", cause=anthropic.AuthenticationError("err", response=httpx.Response(401, headers={"x-api-key": "sk-ant-<secret>", "Authorization": "Bearer sk-ant-<secret>"}, request=...), body={"error": "bad token"}))`，调 `str(exc)`
- **那么** 输出**禁止**含 `sk-ant-<secret>` / `Bearer` / `x-api-key` 任意子串；输出**只**含白名单字段 `backend_name` / `kind` / 脱敏后的 cause 文本（限 200 字符）/ `cause.status_code`（如有）/ `cause.request_id`（如有，限 40 字符），**禁止** dump `cause.__dict__` 或 `cause.response.headers` 或 `cause.response.text` 原文

#### 场景:`__str__` 对空 args / 非字符串 args[0] 防御

- **当** `exc = BackendError(backend_name="x", kind=None, cause=Exception())`（cause.args 为空 tuple）或 `cause=Exception(b"binary-bytes-not-str")` 或 `cause=Exception({"dict": "value"})`（args[0] 不是 str）
- **那么** `str(exc)` 必须不 raise；cause 文本提取按如下顺序回退：(1) 若 `hasattr(cause, "message")` 且 `isinstance(cause.message, str)` 用之；(2) 否则 `cause.args` 非空且 `isinstance(cause.args[0], str)` 用 `cause.args[0]`；(3) 否则 `cause.args` 非空但 `args[0]` 非 str 用 `type(cause.args[0]).__name__`；(4) 否则 `cause.args` 为空用 `type(cause).__name__`；任何分支取到的文本必须经 redact + 限长 200 字符再嵌入

---

### 需求:Anthropic SDK runtime 依赖必须按版本锁定

`pyproject.toml` `[project].dependencies` 必须含 `anthropic>=0.45,<2`。理由：

- `>=0.45`：含 `cache_control` block 与 `tool_use` 完整支持，是本提案功能下限
- `<2`：允许 SDK 在 0.x / 1.x 范围内升级（Anthropic SDK 当前处于 pre-1.0，未来跨 1.0 时也属正常迭代），但屏蔽 2.0 主版本跨越；dependabot 触发的 minor / patch 升级仍需 §15.3 对抗性 review 流程把关，live smoke test（§17.2）是 SDK 兼容性的最终验收门

`pyproject.toml` `[project.optional-dependencies]` **禁止**在本提案中新增 `anthropic[bedrock]` extra（M10.5 范围）。

#### 场景:依赖范围正确

- **当** 读取 `pyproject.toml` 中 `[project].dependencies` 数组
- **那么** 必须含 `"anthropic>=0.45,<2"` 形式约束（**禁止**裸写 `"anthropic"` 不带版本约束；**禁止** `<1.0` 这种永远不允许升级到 1.x 的过窄上限）

#### 场景:M2 范围不引入 boto3

- **当** 读取 `pyproject.toml` 完整 dependencies 列表
- **那么** 必须**不含** `boto3` / `google-cloud-aiplatform`（M10.5 / 1.0 范围）

---

### 需求:Backend 实现必须脱敏所有敏感字段

任何 backend 实现（含 `AnthropicAPIBackend` / `FakeBackend` / `PlaybackBackend`）必须遵守：

- `__repr__` 输出**禁止**含 `api_key` / `base_url` 包含 token 的 URL 子串；如有 SDK client 字段，必须显式过滤（覆盖 `__repr__` 而非依赖默认 dataclass repr）
- **API key 指纹算法**：`api_key_fingerprint(secret: str) -> str` —— 当 `len(secret) >= 12` 时输出 `f"{secret[:4]}...{secret[-4:]}"`；当 `len(secret) < 12` 时统一输出 `"<redacted>"`（**禁止**对短 key 拼接前后切片，否则切片会重叠导致几乎完整 key 泄露）；`api_key is None` / 空字符串时输出 `"<unset>"`
- `BackendHealth.error` 字段值在 `BackendDiagnostics.health_check` 返回前必须经 `hostlens.core.redact.redact_sensitive` 过滤
- `BackendError.__str__` 在 `cause` 含异常消息时必须经 OPERABILITY.md §7.2 脱敏规则过滤再嵌入
- `PlaybackBackend.cassette_path` 在 `CassetteMiss` 异常的字符串表达中只输出相对路径片段，**不**输出绝对路径

#### 场景:`AnthropicAPIBackend.__repr__` 不含 api_key

- **当** 构造 `backend = AnthropicAPIBackend(api_key="sk-ant-<abcdefghijklmn>", base_url=None)`，调 `repr(backend)`
- **那么** 输出**禁止**含 `sk-ant-<abcdefghijklmn>`；可含 `api_key_fingerprint="sk-a...klmn"` 形式指纹

#### 场景:短 api_key 不切片泄露

- **当** 构造 `api_key_fingerprint("short")` （len=5 < 12）
- **那么** 必须返回 `"<redacted>"`（**禁止**返回 `"shor...hort"` 等切片，因切片会重叠近完整原值）

#### 场景:边界长度 api_key

- **当** `api_key_fingerprint("123456789012")`（len=12，恰好达到阈值）
- **那么** 必须返回 `"1234...9012"`（首 4 + 末 4，中间 4 字符被省略号替代，无重叠）

#### 场景:空 api_key 占位

- **当** `api_key_fingerprint(None)` 或 `api_key_fingerprint("")`
- **那么** 必须返回 `"<unset>"`

#### 场景:`CassetteMiss` 不含绝对路径

- **当** `exc = CassetteMiss(request_key="abc", cassette_path="/Users/alice/project/cassettes/x.jsonl")`，调 `str(exc)`
- **那么** 输出**禁止**含 `/Users/alice` 子串；只可含相对路径 `cassettes/x.jsonl` 形式

#### 场景:`BackendHealth.error` 脱敏

- **当** `health = await backend.health_check()` 在 API 错误返回时，error message 含 `"failed: api_key=sk-ant-<real>..."`
- **那么** `health.error` 字段值**禁止**含 `sk-ant-<real>` 子串（经 redact 过滤后保留前 4 + 后 4 字符指纹）
