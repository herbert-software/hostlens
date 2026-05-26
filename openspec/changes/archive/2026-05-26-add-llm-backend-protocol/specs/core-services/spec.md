## 新增需求

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

`AgentSettings` Pydantic 模型字段：

- `primary_model: str = "claude-opus-4-7"`（M2 默认 Anthropic Opus 4.7 model id；用户可在 yaml 覆盖）
- `fallback_model: str | None = None`
- `health_check_model: str = "claude-haiku-4-5"`（doctor / BackendDiagnostics.health_check 用的廉价探测 model；与 primary 解耦防止占用 Opus 配额）
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
