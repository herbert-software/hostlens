## 修改需求

### 需求:`Settings` 必须支持 `backend` 与 `agent` 两个独立 namespace

`hostlens.core.config.Settings` 必须新增两个嵌套 sub-model 字段：

- `backend: BackendSettings | None = None`（缺省允许，向后兼容 M0/M1 既有配置文件）
- `agent: AgentSettings | None = None`（缺省允许）

`BackendSettings` Pydantic 模型字段：

- `type: Literal["anthropic_api", "fake", "playback", "bedrock", "vertex", "claude_subscription"]`（必填）
- `api_key: SecretStr | None = None`（type=anthropic_api 时校验非空；其他 type 允许 None）
- `base_url: HttpUrl | None = None`
- `cassette_path: Path | None = None`（type=playback 时校验非空）
- `aws_region: str | None = None`（type=bedrock 预留位）
- `aws_profile: str | None = None`（type=bedrock 预留位）
- `oauth_token: SecretStr | None = None`（type=claude_subscription 预留位）
- `accept_subscription_risks: bool = False`（type=claude_subscription 预留位）
- `disable_thinking: bool = False`（抑制「thinking 默认开」的 anthropic 兼容端点输出；为 True 时由 `create_backend` 接入 `AnthropicAPIBackend`、令其 `messages_create` 注入 `extra_body={"thinking":{"type":"disabled"}}`，行为见 llm-backend-protocol spec。与 `backend.type` **无耦合校验**：任意 type 都允许设置，仅 `anthropic_api` 路径在 `create_backend` 中真正消费它；默认 `False` 使既有配置与真 Anthropic 请求路径不变）
- `extra_headers: dict[str, str] | None = None`（注入到出站 HTTP 请求的自定义 header，透传给 Anthropic SDK 的 `default_headers`，行为见 llm-backend-protocol spec。用途：OpenRouter 等 anthropic 兼容端点推荐的统计 header（`HTTP-Referer` / `X-OpenRouter-Title`）。与 `backend.type` **无耦合校验**：任意 type 都允许设置，仅 `anthropic_api` 路径在 `create_backend` 中真正消费它；默认 `None` 使既有出站请求 header 不变。接线层**禁止**令 `extra_headers` 覆盖 SDK 认证 header（`x-api-key` / `authorization`）—— 同名键必须被丢弃，认证以 `api_key` 字段为唯一来源）
- `prompt_caching: bool | None = None`（定向覆盖 `AnthropicAPIBackend` 实例的 `capabilities.prompt_caching`；为 `False` 时由 `create_backend` 注入，使 backend 声明不支持 prompt caching，Agent loop 据此**不注入** `cache_control`。用途：OpenRouter 上非 Claude 模型不支持 `cache_control`、`cache_creation_input_tokens` 恒 0，置 `False` 避免 cache hit rate 指标失真。与 `backend.type` **无耦合校验**：任意 type 都允许设置，仅 `anthropic_api` 路径在 `create_backend` 中真正消费它；默认 `None` 等价 `True`（真 Anthropic 默认 prompt caching 生效，既有行为不变）。**仅覆盖 `prompt_caching` 单项**：只有 `prompt_caching` 既被 loop/gate branch、又随模型变；其余 6 个无此需求（`tool_use` 恒真、`structured_output` 是 Planner 语义依赖、另 4 个无消费者），不开放 per-config 覆盖）

`AgentSettings` Pydantic 模型字段：

- `primary_model: str = "claude-opus-4-7"`（M2 默认 Anthropic Opus 4.7 model id；用户可在 yaml 覆盖）
- `fallback_model: str | None = None`
- `health_check_model: str = "claude-haiku-4-5"`（doctor / BackendDiagnostics.health_check 用的廉价探测 model；与 primary 解耦防止占用 Opus 配额）
- `health_check_timeout_seconds: float = 10.0`（doctor 包裹 `BackendDiagnostics.health_check()` 调用的硬超时秒数；**必须 1-120 范围**。默认 10.0 取代旧的硬编码 5.0，给「健康但慢」的 backend——含经 OpenRouter 路由的 DeepSeek / Qwen 等推理系，一次 `max_tokens=10` 的 ping 常 >5s——留余量、避免误报 timeout；上界 120 保证 doctor 始终有界、不会因 backend 挂死而无限阻塞。env 覆盖 `HOSTLENS_AGENT__HEALTH_CHECK_TIMEOUT_SECONDS`；doctor 消费见 llm-backend-protocol spec）
- `max_turns: int = 20`（必须 1-100 范围）
- `token_budget_input: int = 100_000`（必须 1-1_000_000 范围）
- `token_budget_output: int = 30_000`（必须 1-200_000 范围）

`Settings` 必须遵守：

- 缺省 `backend=None` 时**不**触发 backend 相关字段校验（M0 / M1 既有配置文件可直接升级到 M2 无破坏）
- `backend=None` 时调 `create_backend(settings)` 必须 raise `ConfigError("backend.type required to use LLM features")`
- `backend.api_key` 字段类型 Pydantic v2 `SecretStr` 在序列化路径上脱敏：**`model_dump_json()` 与 `model_dump(mode="json")`** 必须输出 `"**********"` 字符串（**禁止**输出原值）；纯 `model_dump()`（默认 mode="python"）保留 `SecretStr` 对象本身（**未**字符串化；实现真实 backend 时通过 `.get_secret_value()` 解包；任何把 `model_dump()` 直接序列化成日志 / doctor JSON / Notifier payload 的代码**必须**走 json 路径，否则 SecretStr 对象的 `str()` 输出 `"**********"` 但 `repr()` 输出 `SecretStr('**********')` 仍可能让原值绕过其他渠道泄露）
- `backend.type` 字段值若为 `"bedrock"` / `"vertex"` / `"claude_subscription"`，配置加载阶段**不** raise，仅在 `create_backend` 分派阶段 raise `NotImplementedError`（schema 占位但不实现）

#### 场景:M0/M1 配置无 backend 字段不破坏

- **当** 配置 yaml **不**含 `backend:` 或 `agent:` 节，调 `load_settings()`
- **那么** 必须 exit 0；`settings.backend is None` 且 `settings.agent is None`

#### 场景:`backend.type = anthropic_api` 必填 api_key

- **当** 配置含 `backend: {type: anthropic_api, api_key: null}`，调 `load_settings()`
- **那么** 必须 raise `ConfigError`，消息含 `"api_key required for type=anthropic_api"` 子串

#### 场景:`backend.type = playback` 必填 cassette_path

- **当** 配置含 `backend: {type: playback, cassette_path: null}`，调 `load_settings()`
- **那么** 必须 raise `ConfigError`，消息含 `"cassette_path required for type=playback"` 子串

#### 场景:`SecretStr` model_dump 脱敏

- **当** `settings.backend.api_key = SecretStr("sk-ant-<real>")`，调 `settings.model_dump_json()`
- **那么** 输出 JSON 中 `api_key` 字段值必须为 `"**********"`；**禁止**含 `sk-ant-<real>` 子串

#### 场景:`backend.type = bedrock` 加载阶段不 raise

- **当** 配置含 `backend: {type: bedrock, aws_region: us-east-1}`，调 `load_settings()`
- **那么** 必须 exit 0；只在后续调 `create_backend(settings)` 时 raise `NotImplementedError`

#### 场景:`agent.max_turns` 范围校验

- **当** 配置含 `agent: {primary_model: x, max_turns: 200}`，调 `load_settings()`
- **那么** 必须 raise `ConfigError`，消息含 `"max_turns must be in range 1-100"` 子串

#### 场景:`backend.api_key` 在 ConfigError 中脱敏

- **当** 配置含 `backend: {type: anthropic_api, api_key: "sk-ant-<leakvalue>"}` 但触发其他字段校验失败（如非法 base_url），调 `load_settings()`
- **那么** raise 的 `ConfigError` 消息中**禁止**含 `sk-ant-<leakvalue>` 子串

#### 场景:`backend` 字段 doctor JSON 输出脱敏

- **当** 调用 `hostlens doctor --json` 时 `settings.backend.api_key = SecretStr("sk-ant-<real>")`
- **那么** 输出 JSON 中**禁止**含 `api_key` 完整原值；可含 `api_key_set: true` 与 `api_key_fingerprint: "sk-a...real"` 形式指纹

#### 场景:`backend.disable_thinking` 缺省为 False

- **当** 配置 `backend:` 节**不**含 `disable_thinking`，调 `load_settings()`
- **那么** 必须 exit 0；`settings.backend.disable_thinking is False`

#### 场景:`backend.disable_thinking` 经 env 加载为 True

- **当** 设置 `HOSTLENS_BACKEND__DISABLE_THINKING=true`（且其余 backend 必填字段满足），调 `load_settings()`
- **那么** 加载出的 `settings.backend.disable_thinking is True`

#### 场景:非 anthropic_api type 设置 disable_thinking 被静默忽略

- **当** 配置含 `backend: {type: playback, cassette_path: "...", disable_thinking: true}`，调 `load_settings()`
- **那么** 必须 exit 0；`settings.backend.disable_thinking is True`，但 playback 路径不消费该字段（不报错、不影响回放）

#### 场景:`backend.extra_headers` 缺省为 None

- **当** 配置 `backend:` 节**不**含 `extra_headers`，调 `load_settings()`
- **那么** 必须 exit 0；`settings.backend.extra_headers is None`

#### 场景:`backend.extra_headers` 经 env 加载为 dict

- **当** 设置 `HOSTLENS_BACKEND__EXTRA_HEADERS='{"HTTP-Referer":"https://example.com","X-OpenRouter-Title":"hostlens"}'`（且其余 backend 必填字段满足），调 `load_settings()`
- **那么** 加载出的 `settings.backend.extra_headers == {"HTTP-Referer": "https://example.com", "X-OpenRouter-Title": "hostlens"}`

#### 场景:`backend.prompt_caching` 缺省为 None

- **当** 配置 `backend:` 节**不**含 `prompt_caching`，调 `load_settings()`
- **那么** 必须 exit 0；`settings.backend.prompt_caching is None`（语义等价 `True`，既有真 Anthropic 行为不变）

#### 场景:`backend.prompt_caching` 经 env 加载为 False

- **当** 设置 `HOSTLENS_BACKEND__PROMPT_CACHING=false`（且其余 backend 必填字段满足），调 `load_settings()`
- **那么** 加载出的 `settings.backend.prompt_caching is False`

#### 场景:`agent.health_check_timeout_seconds` 缺省为 10.0

- **当** 配置含 `agent:` 节但**不**含 `health_check_timeout_seconds`（其余 agent 字段满足），调 `load_settings()`
- **那么** 必须 exit 0；`settings.agent.health_check_timeout_seconds == 10.0`

#### 场景:`agent.health_check_timeout_seconds` 经 env 加载

- **当** 设置 `HOSTLENS_AGENT__HEALTH_CHECK_TIMEOUT_SECONDS=40`（且其余 agent 必填字段满足），调 `load_settings()`
- **那么** 加载出的 `settings.agent.health_check_timeout_seconds == 40.0`

#### 场景:`agent.health_check_timeout_seconds` 范围校验

- **当** 配置含 `agent: {primary_model: x, health_check_timeout_seconds: 0}`（或 `200` / 负数）调 `load_settings()`
- **那么** 必须 raise `ConfigError`，消息指明 `health_check_timeout_seconds` 越界（必须在 1-120 范围）；**禁止**带病加载
