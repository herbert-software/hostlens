## 1. 模块布局与依赖更新

- [x] 1.1 在 `pyproject.toml` `[project].dependencies` 添加 `anthropic>=0.45,<2`；运行 `pip install -e ".[dev]"` 成功；`python -c "import anthropic; print(anthropic.__version__)"` exit 0 且版本 ≥ 0.45
- [x] 1.2 创建 `src/hostlens/agent/__init__.py`（如不存在）；创建 `src/hostlens/agent/backend.py` 与 `src/hostlens/agent/backends/__init__.py`；验收：`python -c "from hostlens.agent import backend"` exit 0
- [x] 1.3 验收：`mypy --strict src/hostlens/agent/` exit 0（空模块阶段也要通过 strict 模式）

## 2. 异常体系扩展

- [x] 2.1 在 `src/hostlens/core/exceptions.py` 新增 `BackendError(HostlensError)`：构造签名 `__init__(self, message: str = "", *, backend_name: str, kind: str | None = None, cause: Exception | None = None)`；实现 `def _extract_cause_text(cause: Exception | None) -> str` 按以下回退顺序提取 cause 文本（**不允许** raise）：(1) `cause is None` → `""` (2) `hasattr(cause, "message") and isinstance(cause.message, str)` → `cause.message` (3) `cause.args and isinstance(cause.args[0], str)` → `cause.args[0]` (4) `cause.args and not isinstance(cause.args[0], str)` → `type(cause.args[0]).__name__` (5) 否则 → `type(cause).__name__`；`__str__` 输出格式 `"BackendError(backend={backend_name}, kind={kind}, cause={cause_text_redacted_200char}, status={cause_status_or_None}, request_id={cause_request_id_40char_or_None})"`；`status` 从 `getattr(cause, "status_code", None)` 安全提取；`request_id` 从 `getattr(cause, "request_id", None)` 安全提取并截 40 字符；**禁止** dump `cause.__dict__` / `cause.response.headers` / `cause.response.text` / `cause.body`；cause 文本必须经 `hostlens.core.redact.redact_sensitive` 过滤后嵌入并限长 200 字符
- [x] 2.1b 写 `tests/core/test_backend_exceptions_repr.py`：(a) 主路径：构造 cause = `Exception("hello sk-ant-<leakvalue> Bearer xyz123")` 加 `response.headers = {"x-api-key": "sk-ant-<secret>"}` 模拟属性，调 `str(BackendError(cause=cause, ...))`；断言输出**不含** `sk-ant-<leakvalue>` / `Bearer xyz123` / `sk-ant-<secret>` 任意子串；断言**不含** `cause.__dict__` 内部 key 名 / `response` / `headers` / `body` 等字符串 (b) **空 args 防御**：`Exception()`（args 空 tuple）→ `str(BackendError(cause=exc, ...))` 不 raise；cause 文本 = `"Exception"`（type name fallback）(c) **非字符串 args 防御**：`Exception(b"binary")` → 不 raise；cause 文本 = `"bytes"` (d) **dict args 防御**：`Exception({"k": "v"})` → 不 raise；cause 文本 = `"dict"` (e) **None cause**：`BackendError(cause=None, ...)` → 不 raise；输出中 `cause=""` (f) **`cause.message` 取代 args[0]**：构造 `e = Exception("ignored"); e.message = "preferred message"` → cause 文本 = `"preferred message"`（验证 fallback 顺序 1 优先于 2）(g) **status_code / request_id 安全提取**：`Exception` 没有 status_code 属性时输出 `status=None` 不 raise
- [x] 2.2 新增 `BackendUnavailable(BackendError)`：无新字段，仅继承
- [x] 2.3 新增 `BackendRateLimited(BackendError)`：构造签名 `__init__(self, *, backend_name: str, retry_after_seconds: float | None = None, cause: Exception | None = None)`；`retry_after_seconds` 字段挂在实例上；`__str__` 含 `retry_after={...}`
- [x] 2.4 新增 `BackendCapabilityViolation(BackendError)`：构造签名 `__init__(self, *, backend_name: str, capability: Literal["prompt_caching", "tool_use", "structured_output", "parallel_tool_use", "extended_thinking", "vision", "streaming"], attempted_feature: Literal["cache_control_in_system_block", "cache_control_in_messages_block", "cache_control_in_tools_array", "tools_array_non_empty"])`；`attempted_feature` 取值不在 Literal 集合内 raise `ValueError`（防 prompt/log injection）
- [x] 2.5 新增 `BackendDaemonUnsafe(BackendError)`：构造签名 `__init__(self, *, backend_name: str, reason: Literal["subscription_in_daemon", "concurrent_request_limit_exceeded"])`
- [x] 2.6 更新 `src/hostlens/core/exceptions.py` 的 `__all__` 从 6 个扩展到 11 个；验收：`from hostlens.core.exceptions import __all__; assert sorted(__all__) == sorted(["HostlensError", "ConfigError", "TargetError", "InspectorError", "ToolError", "ToolPolicyViolation", "BackendError", "BackendUnavailable", "BackendRateLimited", "BackendCapabilityViolation", "BackendDaemonUnsafe"])`
- [x] 2.7 更新 `tests/core/test_exceptions.py::test_module_exports_exactly_six_exception_classes_after_m2` → `test_module_exports_exactly_eleven_exception_classes_after_m2_backend`：把"恰好 6 个"断言改为"恰好 11 个"；验收：`pytest tests/core/test_exceptions.py -v` exit 0
- [x] 2.8 写 `tests/core/test_backend_exceptions.py`：(a) `BackendRateLimited(retry_after_seconds=30.5).retry_after_seconds == 30.5` (b) `BackendCapabilityViolation` 非法 `attempted_feature="x; rm -rf /"` raise `ValueError` (c) `str(BackendError(cause=Exception("api_key=sk-ant-<leak>")))` 不含 `sk-ant-<leak>` 子串 (d) `isinstance(BackendRateLimited(...), BackendError)` 与 `isinstance(..., HostlensError)` 均为 True；密钥不进异常 message 脱敏测试

## 3. BackendCapabilities dataclass

- [x] 3.1 在 `src/hostlens/agent/backend.py` 定义 `@dataclass(frozen=True) class BackendCapabilities`，字段恰好 7 个 `bool`：`prompt_caching` / `tool_use` / `structured_output` / `parallel_tool_use` / `extended_thinking` / `vision` / `streaming`；**全字段无默认值**（强制 backend 显式声明）
- [x] 3.2 写 `tests/agent/test_backend_capabilities.py`：(a) `dataclasses.fields(BackendCapabilities)` 返回恰好 7 field (b) 不传参 raise `TypeError` (c) `caps.prompt_caching = False` raise `FrozenInstanceError`；验收：3 个测试 pass
- [x] 3.3 验收：`mypy --strict src/hostlens/agent/backend.py` exit 0

## 4. MessageResponse Pydantic 模型

- [x] 4.1 在 `src/hostlens/agent/backend.py` 定义 `TextBlock(BaseModel)`：`type: Literal["text"]` / `text: str`
- [x] 4.2 定义 `ToolUseBlock(BaseModel)`：`type: Literal["tool_use"]` / `id: str` / `name: str` / `input: dict[str, Any]`
- [x] 4.3 定义 `ContentBlock = Annotated[TextBlock | ToolUseBlock, Field(discriminator="type")]`
- [x] 4.4 定义 `Usage(BaseModel)`：`input_tokens: int` / `output_tokens: int` / `cache_creation_input_tokens: int = 0` / `cache_read_input_tokens: int = 0`
- [x] 4.5 定义 `MessageResponse(BaseModel)`：`id: str` / `model: str` / `role: Literal["assistant"]` / `content: list[ContentBlock]` / `stop_reason: Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "pause_turn", "refusal"]` / `usage: Usage`；`model_config = ConfigDict(extra="ignore")`
- [x] 4.6 写 `tests/agent/test_message_response.py`：(a) discriminator 路径：text + tool_use 混合 content 解析正确 (b) 未知 type raise `ValidationError` (c) 未声明字段（如 `container`）被忽略不 raise (d) `cache_read_input_tokens` 字段存在性（即使 cassette 没传也默认 0）；验收：4 个测试 pass
- [x] 4.7 写 `tests/agent/test_message_response_sdk_contract.py`（SDK 字段对齐 contract test，spec §需求:MessageResponse §场景:Anthropic SDK Message.model_dump() 字段对齐契约）：构造一个真实 `anthropic.types.Message(...)` 对象（用 SDK 公开构造器或 `anthropic.types.Message.model_validate(...)` 从 JSON 构造），含 text + tool_use 混合 content + 完整 Usage；调用 `MessageResponse.model_validate(real_message.model_dump())`；逐字段断言：`id` / `model` / `role` / `stop_reason` / `content[*].type` / TextBlock 的 `text` / ToolUseBlock 的 `id` / `name` / `input` / `usage.input_tokens` / `usage.output_tokens` / `usage.cache_creation_input_tokens` / `usage.cache_read_input_tokens` 全部对齐；**禁止**用手写 dict 模拟 SDK 形状（手写 dict 不能暴露 SDK 真实字段名变化）；测试在 SDK 升级时若字段名变化会立刻 fail
- [x] 4.8 把 task §17.2 的 live smoke 测试扩展：除已有的 `stop_reason == "end_turn"` 断言，新增对真实 API 响应的 `MessageResponse.model_validate(message.model_dump())` 圆环验证（与 §4.7 同样的字段对齐断言），让 live smoke 也守 SDK 兼容性（**实际代码扩展由组 5 在创建 `tests/agent/backends/test_anthropic_api_live.py` 时实现** —— 本组只标完成）

## 5. LLMBackend Protocol

- [x] 5.1 在 `src/hostlens/agent/backend.py` 定义 `@runtime_checkable class LLMBackend(Protocol)`：`name: str` 类属性 / `capabilities: BackendCapabilities` 实例属性 / `async def messages_create(self, *, model: str, system: list[dict] | str, messages: list[dict], tools: list[dict], max_tokens: int, timeout: float) -> MessageResponse`
- [x] 5.2 写 `tests/agent/test_llm_backend_protocol.py`：(a) Protocol 成员 `LLMBackend.__protocol_attrs__` 含 `name` / `capabilities` / `messages_create` (b) 隐式实现 `LLMBackend` 的 stub class（不继承 Protocol）通过 `isinstance(x, LLMBackend)` 检查 (c) `inspect.iscoroutinefunction(StubBackend.messages_create)` 为 True；验收：3 个测试 pass

## 6. BackendDiagnostics Protocol + BackendHealth / QuotaStatus

- [x] 6.1 在 `src/hostlens/agent/backend.py` 定义 `BackendHealth(BaseModel)`：`is_healthy: bool` / `backend_name: str` / `latency_ms: float | None = None` / `error: str | None = None`
- [x] 6.2 定义 `QuotaStatus(BaseModel)`：`remaining_input_tokens: int | None = None` / `remaining_output_tokens: int | None = None` / `reset_at: datetime | None = None`
- [x] 6.3 定义 `@runtime_checkable class BackendDiagnostics(Protocol)`：`async def health_check(self) -> BackendHealth` / `async def quota_check(self) -> QuotaStatus | None` / `def ensure_safe_for_daemon(self) -> None`
- [x] 6.4 写 `tests/agent/test_backend_diagnostics_protocol.py`：(a) 实现 3 方法的 stub class 通过 `isinstance(x, BackendDiagnostics)` (b) 缺一个方法的 stub class 不通过 (c) `BackendHealth(is_healthy=False, backend_name="x", error="api_key=sk-ant-<leak> failed")` —— **注意**：脱敏发生在 backend 内部 health_check 实现，不在 BackendHealth 构造时；本测试只验证 BackendHealth 字段可接受任意 error string（脱敏由 §8 测试）；验收：3 个测试 pass

## 7. AnthropicAPIBackend 实现

- [x] 7.1 在 `src/hostlens/agent/backends/anthropic_api.py` 实现 `class AnthropicAPIBackend`：构造签名 `__init__(self, *, api_key: str, base_url: str | None = None, health_check_model: str = "claude-haiku-4-5")`；内部 `self._client = anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=0)`；把 `health_check_model` 存实例属性 `self._health_check_model`；`name = "anthropic_api"` 类属性；`capabilities = BackendCapabilities(prompt_caching=True, tool_use=True, structured_output=True, parallel_tool_use=True, extended_thinking=False, vision=True, streaming=False)` 类属性（**`extended_thinking` 与 `streaming` 在 M2 范围内必须为 False**，因 Protocol 签名不含相应参数）
- [x] 7.2 实现 `async def messages_create(self, *, model, system, messages, tools, max_tokens, timeout) -> MessageResponse`：调 `self._client.messages.create(...)`，try/except 包装异常（详见后续 task）；正常 path 把 SDK `Message` 对象 → `MessageResponse.model_validate(message.model_dump())`
- [x] 7.3 在 `messages_create` 异常处理路径包装（**严格对齐 Anthropic SDK 真实 API**：`RateLimitError` / `OverloadedError` 均继承 `APIStatusError`；状态从 `exc.status_code` 读，header 从 `exc.response.headers` 读）：catch `anthropic.RateLimitError` → 读 `exc.response.headers.get("retry-after")`，转 float（解析失败为 None）→ raise `BackendRateLimited(backend_name="anthropic_api", retry_after_seconds=<value>, cause=exc)`；catch `anthropic.OverloadedError` **或** 任意 `anthropic.APIStatusError` 且 `exc.status_code == 529` → raise `BackendRateLimited(backend_name="anthropic_api", retry_after_seconds=None, cause=exc)`；catch 其他 `anthropic.APIStatusError`（5xx 非 529）→ raise `BackendUnavailable(backend_name="anthropic_api", cause=exc)`；catch `anthropic.APIConnectionError | anthropic.APITimeoutError` → raise `BackendUnavailable(backend_name="anthropic_api", cause=exc)`；catch `anthropic.AuthenticationError` → raise `BackendError(backend_name="anthropic_api", kind="auth_invalid", cause=exc)`
- [x] 7.4 在 `src/hostlens/agent/backend.py` 实现 `def api_key_fingerprint(secret: str | None) -> str`：`secret is None or secret == ""` → 返回 `"<unset>"`；`len(secret) < 12` → 返回 `"<redacted>"`（**禁止**短 key 走切片路径，因切片会重叠泄露）；`len(secret) >= 12` → 返回 `f"{secret[:4]}...{secret[-4:]}"`；写 `tests/agent/test_api_key_fingerprint.py` 覆盖 5 个 case（None / 空字符串 / len=5 短 key / len=12 边界 / len=20 长 key）；然后在 `AnthropicAPIBackend.__repr__` 内调用此函数：返回 `f"AnthropicAPIBackend(api_key_fingerprint={api_key_fingerprint(api_key)!r}, base_url={base_url!r})"`；**禁止**含 api_key 完整原值
- [x] 7.5 实现 `async def health_check(self) -> BackendHealth`：调 `await self._client.messages.create(model=self._health_check_model, messages=[{"role": "user", "content": "ping"}], max_tokens=10)`（**model 必须**从构造时注入的 `self._health_check_model` 读取，不允许硬编码字符串字面值或从 `settings.agent.primary_model` 隐式读取 —— backend 不应依赖 Settings 对象）；用 `time.perf_counter()` 测延迟；成功返回 `BackendHealth(is_healthy=True, backend_name="anthropic_api", latency_ms=<measured>)`；异常 catch 后返回 `BackendHealth(is_healthy=False, backend_name="anthropic_api", error=redact_sensitive(str(exc)))`
- [x] 7.6 实现 `async def quota_check(self) -> QuotaStatus | None`：M2 范围**必须**返回 `None`
- [x] 7.7 实现 `def ensure_safe_for_daemon(self) -> None`：no-op（API key 在 daemon 模式安全）
- [x] 7.8 写 `tests/agent/backends/test_anthropic_api.py`（**所有 SDK 异常构造必须用真实 httpx.Response 而非 MockResponse**：`httpx.Response(<status>, headers=..., request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))`）：(a) `backend._client.max_retries == 0`（用 mock SDK 验证构造参数）(b) `backend.capabilities == BackendCapabilities(prompt_caching=True, tool_use=True, structured_output=True, parallel_tool_use=True, extended_thinking=False, vision=True, streaming=False)` (c) `repr(backend)` 不含 `sk-ant-<abcdefghijklmn>` 完整子串 (d) mock SDK raise `RateLimitError("...", response=httpx.Response(429, headers={"retry-after": "30"}, request=...), body=None)` → backend raise `BackendRateLimited` 且 `retry_after_seconds == 30.0` (e) mock SDK raise `OverloadedError("...", response=httpx.Response(529, request=...), body=None)` → backend raise `BackendRateLimited(retry_after_seconds=None)` (f) mock SDK raise `APIStatusError("...", response=httpx.Response(502, request=...), body=None)` → backend raise `BackendUnavailable` (g) mock SDK raise `APIConnectionError(request=...)` → backend raise `BackendUnavailable` (h) mock SDK raise `AuthenticationError("...", response=httpx.Response(401, request=...), body=None)` → backend raise `BackendError(kind="auth_invalid")` (i) `health_check` 成功路径返回 `is_healthy=True` (j) `health_check` 失败时 `error` 字段不含 `sk-ant-` 完整子串
- [x] 7.9 写 `tests/agent/backends/test_anthropic_api_capability_gate.py`：构造 `backend` 后调 `messages_create(system=[{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}], ...)` 不 raise `BackendCapabilityViolation`（`prompt_caching=True` 正常 path）
- [x] 7.10 写 `tests/agent/backends/test_anthropic_api_live.py`：标记 `@pytest.mark.live`；从 env 读 `ANTHROPIC_API_KEY` 跑一次真实 ping；CI 不跑（pytest config 默认 `-m "not live"`）；本地开发者用 `pytest -m live` 跑

## 8. FakeBackend 实现

- [x] 8.1 在 `src/hostlens/agent/backends/fake.py` 实现 `class FakeBackend`：构造签名 `__init__(self, *, responses: list[MessageResponse], capabilities: BackendCapabilities | None = None)`；`capabilities` 默认值 = `BackendCapabilities(prompt_caching=True, tool_use=True, structured_output=True, parallel_tool_use=True, extended_thinking=False, vision=True, streaming=False)`；`name = "fake"` 类属性；内部 `self._responses = list(responses)` / `self._response_idx = 0`
- [x] 8.2 实现 `async def messages_create(self, *, model, system, messages, tools, max_tokens, timeout) -> MessageResponse`：检查 capability gate（详见 §10）；返回 `self._responses[self._response_idx]`；`self._response_idx += 1`；耗尽时 raise `IndexError("FakeBackend exhausted: ...")`
- [x] 8.3 写 `tests/agent/backends/test_fake.py`：(a) 顺序返回 3 个 response (b) 耗尽 raise `IndexError` (c) capability 默认值 = `BackendCapabilities(prompt_caching=True, tool_use=True, structured_output=True, parallel_tool_use=True, extended_thinking=False, vision=True, streaming=False)`（**`extended_thinking` 与 `streaming` 必须 False**）(d) 自定义 capability 生效 (e) `name == "fake"`；验收：5 个测试 pass

## 9. PlaybackBackend 实现 + CassetteMiss

- [x] 9.1 在 `src/hostlens/agent/backends/playback.py` 实现 `class CassetteMiss(BackendError)`：构造签名 `__init__(self, *, request_key: str, cassette_path: str)`；**实现关键**：内部调 `super().__init__(backend_name="playback", kind="cassette_miss", cause=None)` 以满足 `BackendError` 基类 `backend_name` 必填关键字参数；把 `request_key` / `cassette_path` 挂实例属性；覆盖 `__str__` 返回 `f"CassetteMiss(request_key={request_key[:16]}..., cassette={Path(cassette_path).name})"`；**禁止**输出绝对路径完整值；mypy --strict 通过（`super().__init__` 入参完整）
- [x] 9.2 实现 `class PlaybackBackend`：构造签名 `__init__(self, *, cassette_path: Path)`；`name = "playback"` 类属性；`capabilities` 同 FakeBackend 默认值；构造时加载 cassette（JSON Lines 解析，每行 `{"request": ..., "response": ...}`）；解析失败 raise `ValueError("invalid cassette format at line N")`
- [x] 9.3 实现 `def _request_key(self, *, model, messages, tools) -> str`：计算 `hashlib.sha256(json.dumps({"model": model, "messages": messages, "tools_count": len(tools)}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()`；返回完整 hex（CassetteMiss `__str__` 时截断显示）
- [x] 9.4 实现 `async def messages_create(...)`：检查 capability gate；计算 request_key；在 `self._records` 中查 key；找到返回 `MessageResponse.model_validate(record["response"])`；miss raise `CassetteMiss`
- [x] 9.5 写 `tests/agent/backends/test_playback.py`：(a) 正常回放路径 (b) miss raise `CassetteMiss` (c) `str(exc)` 不含绝对路径 (d) cassette 格式 invalid raise `ValueError` (e) miss 时即使 env 有 `ANTHROPIC_API_KEY` 也不调真实 API（用 mock anthropic 验证：如调到了 mock 则 fail）(f) **`isinstance` 链**：`exc = CassetteMiss(request_key="x", cassette_path="cassettes/x.jsonl")`；断言 `isinstance(exc, BackendError) and isinstance(exc, HostlensError)`；断言 `exc.backend_name == "playback"`（验证 `super().__init__` 真把 `backend_name` 传上去；不传上去 mypy --strict 会过但运行时 `exc.backend_name` 会 AttributeError）
- [x] 9.6 写 `tests/fixtures/cassettes/list_inspectors_demo.jsonl`：手工录制 1 条 record，request key **必须**与 proposal Demo Path 步骤 3 的入参完全对齐 —— `request = {"model": "claude-opus-4-7", "messages": [{"role": "user", "content": "list inspectors"}], "tools_count": 0}`（**tools=[] 故 tools_count=0**）；response 含 `stop_reason="tool_use"` 的合法 MessageResponse 完整结构（id / model="claude-opus-4-7" / role="assistant" / content list 含 1 个 ToolUseBlock / usage 完整 4 字段）；可选 `tools_schema_hash` lint metadata 字段（M2 阶段可 omit）

## 10. BackendCapabilityViolation 守门（在所有 backend 实现 messages_create 入口）

- [x] 10.1 在 `src/hostlens/agent/backend.py` 实现辅助函数 `def check_capability_consistency(*, backend_name: str, capabilities: BackendCapabilities, system: list[dict] | str, messages: list[dict], tools: list[dict]) -> None`：分三步扫描 `cache_control` block（按 spec §需求:BackendCapabilityViolation 描述）：(a) `system` 为 list[dict] 时遍历每个 block 查 `"cache_control"` key，命中且 `not capabilities.prompt_caching` raise with `attempted_feature="cache_control_in_system_block"` (b) `messages[*].content[*]`（content 为 list 时遍历每个 content block）查 `"cache_control"` key，命中且 `not capabilities.prompt_caching` raise with `attempted_feature="cache_control_in_messages_block"` (c) `tools[*]` 遍历每个 tool dict 查 `"cache_control"` key，命中且 `not capabilities.prompt_caching` raise with `attempted_feature="cache_control_in_tools_array"`；若 `len(tools) > 0` 且 `capabilities.tool_use == False` raise with `capability="tool_use"`，`attempted_feature="tools_array_non_empty"`
- [x] 10.2 在 `AnthropicAPIBackend.messages_create` 调用 `self._client.messages.create(...)` **之前**调一次 `check_capability_consistency(...)`
- [x] 10.3 在 `FakeBackend.messages_create` 与 `PlaybackBackend.messages_create` 同样调用 `check_capability_consistency(...)`
- [x] 10.4 写 `tests/agent/test_capability_gate.py`：(a) `FakeBackend(capabilities=BackendCapabilities(prompt_caching=False, ...))` + `system=[{"type": "text", "text": "x", "cache_control": ...}]` → raise `BackendCapabilityViolation(attempted_feature="cache_control_in_system_block")` (b) 同上 backend + `messages=[{"role": "user", "content": [{"type": "text", "text": "x", "cache_control": ...}]}]` → raise with `attempted_feature="cache_control_in_messages_block"` (c) 同上 backend + `tools=[{"name": "x", "input_schema": {}, "cache_control": ...}]` → raise with `attempted_feature="cache_control_in_tools_array"` (d) `FakeBackend(capabilities=BackendCapabilities(tool_use=False, ...))` + `tools=[{"name": "x", ...}]` → raise `BackendCapabilityViolation(capability="tool_use")` (e) `AnthropicAPIBackend` 默认 `prompt_caching=True` + `cache_control` 在 system / messages / tools 三处任意位置都不 raise (f) `attempted_feature` 字段值是 Literal 取值（不允许自由文本）；验收：6 个测试 pass

## 11. Settings.backend / Settings.agent 配置 schema

- [x] 11.1 在 `src/hostlens/core/config.py` 新增 `class BackendSettings(BaseModel)`：字段按 spec §需求:Settings 必须支持 backend 与 agent 两个独立 namespace 列出；`model_config = ConfigDict(extra="forbid")`；`type` 字段用 `Literal[...]`；`api_key` / `oauth_token` 字段类型 `SecretStr | None`；`accept_subscription_risks: bool = False`
- [x] 11.2 在 `BackendSettings` 添加 `@model_validator(mode="after")`：
  - `type == "anthropic_api"` 时 `api_key` 必须非 None（否则 raise `ValueError("api_key required for type=anthropic_api")`）
  - `type == "playback"` 时 `cassette_path` 必须非 None（否则 raise `ValueError("cassette_path required for type=playback"`)
  - `type ∈ {"bedrock", "vertex", "claude_subscription"}` 时**不**做字段必填校验（schema 占位允许加载，与 spec §需求:Settings 必须支持 backend 与 agent 两个独立 namespace §场景:`backend.type = bedrock` 加载阶段不 raise 严格对齐）；失败必须在 `create_backend` 分派阶段 raise `NotImplementedError`，**不**在 schema 层提前 raise，避免 M2 范围尚未实现的 backend 的字段约束意外阻塞配置文件加载
- [x] 11.3 新增 `class AgentSettings(BaseModel)`：字段按 spec 列出：`primary_model: str = "claude-opus-4-7"` / `fallback_model: str | None = None` / `health_check_model: str = "claude-haiku-4-5"` / `max_turns: int = Field(default=20, ge=1, le=100)` / `token_budget_input: int = Field(default=100_000, ge=1, le=1_000_000)` / `token_budget_output: int = Field(default=30_000, ge=1, le=200_000)`；`model_config = ConfigDict(extra="forbid")`
- [x] 11.4 在 `Settings` 类新增 `backend: BackendSettings | None = None` 与 `agent: AgentSettings | None = None`；M0/M1 既有配置无破坏（缺省允许）
- [x] 11.5 写 `tests/core/test_config_backend.py`：(a) 缺 backend 字段 `settings.backend is None` 不 raise (b) `backend: {type: anthropic_api, api_key: null}` raise `ConfigError` 含 `api_key required` 子串 (c) `backend: {type: playback, cassette_path: null}` raise `ConfigError` 含 `cassette_path required` 子串 (d) `agent.max_turns = 200` raise `ConfigError` 含 `max_turns must be in range` 子串 (e) `backend.api_key = SecretStr("sk-ant-<real>")` 后 `settings.model_dump_json()` 不含 `sk-ant-<real>` 子串 (f) `backend: {type: bedrock, aws_region: us-east-1}` 加载不 raise（schema 占位）(g) `backend.api_key = SecretStr("sk-ant-<leakvalue>")` 触发 base_url 校验失败时 `ConfigError` message 不含 `sk-ant-<leakvalue>` 子串
- [x] 11.6 验收：`pytest tests/core/test_config_backend.py -v` exit 0

## 12. create_backend 工厂 + is_daemon_mode hook

- [x] 12.1 在 `src/hostlens/agent/backend.py` 实现 `def is_daemon_mode(settings: Settings) -> bool`：M2 范围**永远返回 False**；函数签名 `(settings: Settings) -> bool` 严格固定；**在 `create_backend` 调用点必须通过模块属性引用调用**（`from hostlens.agent import backend; backend.is_daemon_mode(settings)` 或同模块内 `is_daemon_mode(settings)` 直接调用），**禁止** `from hostlens.agent.backend import is_daemon_mode` 后在 `create_backend` 内部用裸名字调用（会让 monkey-patch 在测试中需要 patch `hostlens.agent.backend.is_daemon_mode` 但实际调用路径已绑定到 importing module 的本地引用，导致 patch 失效；正确做法是把 `is_daemon_mode` 与 `create_backend` 同模块定义并通过模块级名字调用，测试 monkey-patch `hostlens.agent.backend.is_daemon_mode` 即可生效）
- [x] 12.2 实现 `def create_backend(settings: Settings) -> LLMBackend`：
  - `settings.backend is None` raise `ConfigError("backend.type required to use LLM features")`
  - `settings.backend.type == "anthropic_api"` → 返回 `AnthropicAPIBackend(api_key=settings.backend.api_key.get_secret_value(), base_url=str(settings.backend.base_url) if settings.backend.base_url else None, health_check_model=settings.agent.health_check_model if settings.agent else "claude-haiku-4-5")`
  - `settings.backend.type == "fake"` → 返回 `FakeBackend(responses=[])`（注：测试 fixture 通常直接构造 FakeBackend，本工厂路径用于通过配置驱动 fake 模式时；空 responses 列表，调用方负责后续配置）
  - `settings.backend.type == "playback"` → 返回 `PlaybackBackend(cassette_path=settings.backend.cassette_path)`
  - `settings.backend.type in ("bedrock", "vertex", "claude_subscription")` → raise `NotImplementedError("backend type X 将在 M10.5 / 1.0 落地；当前请使用 anthropic_api")`
  - 构造完成后 `if is_daemon_mode(settings) and isinstance(backend, BackendDiagnostics): backend.ensure_safe_for_daemon()`；异常向上传播（不捕获）
- [x] 12.3 写 `tests/agent/test_create_backend.py`：(a) `settings.backend is None` raise `ConfigError` (b) `anthropic_api` 分派返回 `AnthropicAPIBackend` 实例 (c) **关键**：patch `anthropic.AsyncAnthropic.__init__` 为 spy，构造 `settings.backend.api_key = SecretStr("sk-ant-<realvalue123>")` 后调 `create_backend(settings)`；断言 spy 收到的 `api_key` kwarg **恰好等于** `"sk-ant-<realvalue123>"`，**不**等于 `"**********"` / `"<redacted>"` / SecretStr 对象 / `str(SecretStr(...))`（保证 §需求:create_backend 工厂 §场景:anthropic_api 分派 的 `.get_secret_value()` 解包路径生效）(d) `anthropic_api` + `api_key is None` raise `ConfigError` 含 `"api_key required"` 子串 (e) `playback` 分派返回 `PlaybackBackend` 实例 (f) `bedrock` raise `NotImplementedError("...M10.5...")` (g) monkey-patch `is_daemon_mode` 返回 True + monkey-patch backend `ensure_safe_for_daemon` raise `BackendDaemonUnsafe` → `create_backend` 不捕获让异常传播；验收：7 个测试 pass
- [x] 12.4 写 `tests/agent/test_is_daemon_mode.py`：(a) 任意 settings 调 `is_daemon_mode` 返回 False (b) `inspect.signature(is_daemon_mode)` 参数 `settings: Settings` 返回 `bool`；验收：2 个测试 pass

## 13. doctor 集成（保 M2 doctor 命令能识别 backend 字段）

- [x] 13.1 修改 `src/hostlens/cli/doctor.py`（M0 已落地）：当 `settings.backend is not None` 时新增一段 backend 健康输出：`backend.type` / `backend.api_key_set: bool` / `backend.api_key_fingerprint: str | None`（**禁止**输出 api_key 完整值）；若 backend 实现 `BackendDiagnostics`，调一次 `backend.health_check()`（带 5s timeout）并把 `BackendHealth.is_healthy` / `latency_ms` / `error`（脱敏后）加入输出
- [x] 13.2 写 `tests/cli/test_doctor_backend.py`：(a) 配置无 backend 节时 doctor 输出无 `backend` section 不 raise (b) 配置 `backend: {type: fake}` doctor 输出含 `backend.type: fake`（fake backend 无 BackendDiagnostics，doctor 不调 health_check）(c) 配置 `backend: {type: anthropic_api, api_key: "sk-ant-<realxxxxxxx>"}` doctor `--json` 输出**不**含 `sk-ant-<realxxxxxxx>` 子串；含 `api_key_set: true` 与 `api_key_fingerprint: "sk-a...xxxx"` (d) 配置 `backend: {type: anthropic_api, api_key: "sk-ant-<validkey1234>"}`（**有效配置使 load 通过**）且 monkey-patch `AnthropicAPIBackend.health_check` 返回 `BackendHealth(is_healthy=False, error="failed: connect to api.anthropic.com via sk-ant-<leakkey>")` 时 doctor 输出的 `error` 字段必须**不**含 `sk-ant-<leakkey>` 子串（脱敏路径生效）；验收：4 个测试 pass

## 14. Cassette 与 scripts/cassette_lint.py

- [x] 14.1 创建 `tests/fixtures/cassettes/` 目录与 `tests/fixtures/cassettes/README.md`（说明 cassette 格式与录制流程；不写 emoji；不超过 2 KB）
- [x] 14.2 `tests/fixtures/cassettes/list_inspectors_demo.jsonl` 由 §9.6 单一来源创建，本任务**不**重复；本任务仅追加确认 cassette 文件已落地（`test -f` exit 0）；如未来需要额外 demo cassette，在此分配独立文件名（如 `run_inspector_demo.jsonl`）防止 key 冲突
- [x] 14.3 创建 `scripts/cassette_lint.py`：扫描 `tests/fixtures/cassettes/*.jsonl` 每行 JSON；对每个 record 用 `MessageResponse.model_validate(record["response"])` 校验 schema；用 `hostlens.core.redact.redact_sensitive` 的完整正则集（**含** API key 形态 `sk-ant-...` / `sk-...` / Bearer token / JWT / 路径 `/Users/` `/home/` `.ssh` / IPv4 / 邮件 `user@host` / hostname / credential key-value 如 `password=...` 等）扫描整行 string；命中任一规则 fail-exit 1 且 stderr 输出 `sensitive substring detected: <pattern>` 提示；clean exit 0；新增 `--check-schema-drift` flag + 必填 `--current-tools-hash <hex>` 参数（用 argparse）单独跑 tools_schema_hash drift 检查；drift 输出 warning 到 stdout 不 exit 1；缺 `--current-tools-hash` 时 exit 2 + stderr 报错；**禁止** cassette_lint.py 内部 `import hostlens.tools` 等业务包（保持 lint 工具独立，由 CI 在外部算 hash 后注入）
- [x] 14.4 写 `tests/test_cassette_lint.py`：(a) 跑 `scripts/cassette_lint.py` exit 0（验证现有 cassette clean）(b) 在 tmp 目录构造一个含 `"api_key": "sk-ant-<leak>"` 的 cassette 行 → lint exit 1 且 stderr 含 `"sensitive substring detected"` 子串 (c) 构造一个含路径 `"snippet": "/Users/alice/.ssh/id_rsa"` 的 cassette 行 → lint exit 1（业务敏感扩展规则生效）(d) 构造一个含 IPv4 `"host": "10.0.0.5"` 的 cassette 行 → lint exit 1 (e) 构造 record 含 `tools_schema_hash="abc"`，跑 `--check-schema-drift --current-tools-hash xyz` → stdout 含 `WARNING: tools_schema_hash drift`，**不** exit 1 (f) 缺 `--current-tools-hash` 跑 `--check-schema-drift` → exit 2 + stderr 含 `--current-tools-hash required`

## 15. 文档同步

- [x] 15.1 创建 `docs/MIGRATION.md`（如不存在）：写入 M1 → M2 升级最小配置 diff（按 design.md「迁移计划」段落）；不超过 1 页；不写 emoji
- [x] 15.2 验收：`grep -F "backend:" docs/MIGRATION.md` 命中至少 1 次；`grep -F "primary_model" docs/MIGRATION.md` 命中至少 1 次

## 16. CI / 静态检查

- [x] 16.1 运行 `ruff check . && ruff format --check .` exit 0
- [x] 16.2 运行 `mypy --strict src/` exit 0；**关键**：所有新增模块（`hostlens.agent.backend` / `hostlens.agent.backends.*`）的导出符号都有完整类型注解；`anthropic` 包的类型 stub 通过 `mypy --strict` 走 `# type: ignore[import]` 或 SDK 提供的 stub
- [x] 16.3 运行 `pytest --cov=hostlens.agent --cov-report=term` exit 0；`hostlens.agent.backend` + `hostlens.agent.backends` 子模块覆盖率 ≥85%
- [x] 16.4 运行 `pre-commit run --all-files` exit 0
- [x] 16.5 运行 `scripts/cassette_lint.py` exit 0；CI 加一步在 `.github/workflows/ci.yml` 调此 lint

## 17. Anthropic API 真实调用 smoke（本地开发者跑 / CI 跳过）

- [x] 17.1 `pyproject.toml` `[tool.pytest.ini_options]` 配置 `markers = ["live: tests that hit real Anthropic API"]` 与 `addopts = "-m 'not live'"`（CI 默认跳过 live 测试）
- [x] 17.2 本地验收（开发者必跑一次）：`ANTHROPIC_API_KEY=sk-ant-<xxx> pytest -m live tests/agent/backends/test_anthropic_api_live.py -v` exit 0 且响应含 `stop_reason="end_turn"`；**关键**：本测试验证 SDK 集成正确，特别是 429 with retry-after 严格 honor 路径（如有限流可 retry）

## 18. Demo Path 验收

- [x] 18.1 在干净 venv 跑 proposal Demo Path 步骤 1（`create_backend` 工厂分派 + capability 声明）exit 0 且输出符合预期
- [x] 18.2 跑步骤 2（`FakeBackend` messages_create 完整路径）exit 0
- [x] 18.3 跑步骤 3（`PlaybackBackend` cassette 回放）exit 0；前提：步骤 14.2 cassette 文件已落地
- [x] 18.4 跑步骤 4（`pytest tests/agent/test_backend_*.py tests/agent/backends/ -v`）exit 0；至少含本提案新增的全部测试

## 19. Git 工作流与归档准备

- [x] 19.1 完成所有上述任务后 commit 到 feature branch `feat/add-llm-backend-protocol`；commit message 含 OpenSpec change name 引用
- [x] 19.2 commit 后、push 前跑对抗性 review（CLAUDE.md §5.3）：默认 `/review-loop-codex`，APPROVE/CLEAR 才 push；若跳过 review 必须在 PR 描述说明理由
- [x] 19.3 push branch + 开 PR 到 main；PR 描述含 spec 引用（`openspec/changes/add-llm-backend-protocol/`）与 Demo Path
- [x] 19.4 等 CI 全绿 + review 通过后 squash merge：`\gh pr merge <num> --squash --delete-branch`
- [x] 19.5 准备归档：跑 `openspec-cn validate add-llm-backend-protocol` 确认变更可归档；后续运行 `/opsx:archive` 推进到 `openspec/specs/llm-backend-protocol/` 与 `openspec/specs/core-services/`（合并 core-services delta）
