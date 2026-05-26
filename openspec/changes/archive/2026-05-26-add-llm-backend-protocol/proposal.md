## 为什么

M2 手写 Agent loop 是 Hostlens 简历价值的核心展示点 —— HR/面试官打开 `src/hostlens/agent/loop.py` 能直接看到 Agent 怎么跑 tool-use 循环。这条循环不能直接 `import anthropic` 调 `client.messages.create(...)`，否则会把两件事死焊在一起：

1. **可测试性消失**：单测只能 monkey-patch Anthropic SDK，集成测试拿不到稳定的 cassette replay 路径，VCR 也只能在 HTTP 层 hack 一遍 SDK 内部行为
2. **认证方式被锁死**：未来切到 Bedrock（IAM）/ Vertex（GCP SA）/ 临时用订阅 OAuth 时，Agent loop 必须改代码 —— 违反 CLAUDE.md §4.11「不锁死单一认证方式」原则
3. **`cache_control` 行为隐式**：Anthropic SDK 静默接受不支持 prompt caching 的 model id 而**不报错**，会让"cache hit rate"指标失真，发现不了线上 prompt 没被缓存的 bug

CLAUDE.md §4.11 与 docs/ARCHITECTURE.md §9「模型层」已把抽象形状钦点死：`LLMBackend` 是 **Anthropic-schema-first 的薄抽象**，不是 vendor-agnostic 通用 LLM 包装层（那是 LangChain / LiteLLM 的事）。ADR-008 进一步规定 backend **不进 `ToolContext`**，避免 Inspector handler 拿到 backend 后绕过「Inspector 不能调 LLM」红线。这些约束已在 `add-tool-registry-capability-layer` proposal 的非目标里显式 forward-reference 到本 proposal，本 proposal 是 M2 `add-agent-loop-skeleton` 落地的最后一块前置依赖。

## 变更内容

**新增（核心抽象）：**

- `BackendCapabilities` dataclass（frozen）：7 字段能力声明（`prompt_caching` / `tool_use` / `structured_output` / `parallel_tool_use` / `extended_thinking` / `vision` / `streaming`）—— 按需扩展原则，只列 Agent loop 真实使用的能力
- `LLMBackend` Protocol（Anthropic-schema-first）：`name: str` / `capabilities: BackendCapabilities` / `async def messages_create(*, model, system, messages, tools, max_tokens, timeout) -> MessageResponse`
- `MessageResponse` Pydantic 模型：镜像 Anthropic `Message` 关键字段（`id` / `model` / `role` / `content: list[ContentBlock]` / `stop_reason` / `usage`），含 `cache_creation_input_tokens` / `cache_read_input_tokens` 两个 prompt caching 关键指标
- `ContentBlock` 联合类型：`TextBlock` / `ToolUseBlock`（M2 范围；`ToolResultBlock` 在 Agent loop 一侧构造，不在 backend 返回值里）
- `BackendDiagnostics` Protocol（**接口必须 M2 存在**，实现按 backend 决定）：`async def health_check() -> BackendHealth` / `async def quota_check() -> QuotaStatus | None` / `def ensure_safe_for_daemon() -> None`
- `BackendCapabilityViolation(BackendError)` 异常：当 Agent loop 在 `capabilities.prompt_caching=False` 时仍传 `cache_control` block，backend **必须 raise** 此异常暴露 bug（不允许静默丢弃假装成功）；继承 `BackendError` 间接继承 `HostlensError`
- `BackendError(HostlensError)`：backend 通信错误基类
- `BackendUnavailable(BackendError)`：网络 / 5xx / 完全宕机
- `BackendRateLimited(BackendError)`：429 / 529 / 订阅软限制（附 `retry_after_seconds: float | None`）
- `BackendDaemonUnsafe(BackendError)`：`ensure_safe_for_daemon()` 拒绝时 raise

**新增（M2 范围的 3 个 Backend 实现）：**

- `AnthropicAPIBackend`：完整 `LLMBackend` 实现 + 基础 `BackendDiagnostics`
  - `capabilities = BackendCapabilities(prompt_caching=True, tool_use=True, structured_output=True, parallel_tool_use=True, extended_thinking=False, vision=True, streaming=False)`（M2 范围内 `extended_thinking` / `streaming` 均 False —— Protocol 签名不含 `thinking` 参数与流式分块返回；待 M3 真正消费时同步扩展）
  - 认证：从 `Settings.backend.api_key` 取（支持 `${ANTHROPIC_API_KEY}` 占位展开），可选 `base_url` 走自建代理
  - 构造参数 `health_check_model: str = "claude-haiku-4-5"` —— `health_check` 用的廉价探测 model，与 primary Opus 解耦防止占用配额；由 `create_backend` 从 `agent.health_check_model` 注入
  - `health_check`：调用 `messages.create(model=self._health_check_model, ...)` 跑 1 个 `"ping"` 短 prompt（≤10 input tokens）确认凭据可用
  - `quota_check`：M2 返回 `None`（Anthropic Console quota API 1.0 后再接）
  - `ensure_safe_for_daemon`：no-op（API key 在 daemon 模式下安全）
- `FakeBackend`：单元测试 stub
  - 构造时传入 `responses: list[MessageResponse]`，按顺序返回；耗尽后 raise `IndexError`
  - `capabilities` 由构造参数控制（默认全 True，除 `extended_thinking=False` / `streaming=False`，方便测 capability gate 路径）
  - **不实现** `BackendDiagnostics` —— 与 `PlaybackBackend` 一致；`create_backend` 工厂只对实现了 `BackendDiagnostics` 的 backend 调用 `ensure_safe_for_daemon`（duck-type 检测），FakeBackend 走 `isinstance` 检查 False 路径无需提供 no-op 桩
- `PlaybackBackend`：cassette 回放（集成测试 CI 必备）
  - 启动时从 `cassette_path` 读 JSON Lines（每行一个 `{"request": {...}, "response": {...}, "tools_schema_hash": "<hex>"?}`；`tools_schema_hash` 是可选 lint-only metadata）
  - `messages_create` 计算 cassette key = `SHA256(json.dumps({"model": ..., "messages": ..., "tools_count": len(tools)}, sort_keys=True))`，按 key 查记录；找不到时 raise `CassetteMiss(request_key=..., cassette_path=...)`（构造签名 `(*, request_key: str, cassette_path: str)`；继承 `BackendError`，内部 `super().__init__(backend_name="playback", kind="cassette_miss")`），**不**允许"录制模式"在 CI 静默通过
  - 不实现 `BackendDiagnostics`（cassette 模式没有"真实健康"概念）

**新增（配置 schema）：**

- `Settings.backend` 与 `Settings.agent` 严格分两个 namespace（CLAUDE.md §4.11 第 4 条）
- `backend.type: Literal["anthropic_api", "fake", "playback"]`（M2 三选一；`bedrock` / `vertex` / `claude_subscription` 在 schema 里**预留** Literal 取值但加载时 raise `NotImplementedError("backend type X 将在 M10.5 / 1.0 落地")`）
- `backend.api_key: SecretStr | None`、`backend.base_url: HttpUrl | None`、`backend.cassette_path: Path | None`
- `agent.primary_model: str`（M2 默认 `"claude-opus-4-7"`）、`agent.fallback_model: str | None`、`agent.max_turns: int = 20`、`agent.token_budget_input: int = 100_000`、`agent.token_budget_output: int = 30_000`

**新增（工厂与装配）：**

- `create_backend(settings: Settings) -> LLMBackend`：单一工厂入口，按 `settings.backend.type` 分派；负责 `ensure_safe_for_daemon()` 在 daemon 模式启动时调用
- `is_daemon_mode(settings: Settings) -> bool`：M2 stub 返回 `False`（daemon 模式由 M5 Scheduler 引入；提前定义函数签名让 `create_backend` 调用点稳定）

**非目标（Non-Goals）：**

- ❌ **不**实现 `BedrockBackend`（M10.5 范围；本提案的 `LLMBackend` Protocol 是 Bedrock 落地的前置依赖）
- ❌ **不**实现 `VertexBackend`（1.0 后）
- ❌ **不**实现 `ClaudeSubscriptionBackend`（M10.5 experimental；本提案预留 `BackendDaemonUnsafe` 异常与 `ensure_safe_for_daemon` 接口契约，让订阅 backend 后续落地不需要改 Protocol）
- ❌ **不**实现 Agent loop 本体（`add-agent-loop-skeleton` proposal 范围；本 proposal 只交付 backend 抽象与 3 个实现）
- ❌ **不**实现 streaming（M2 范围内 non-streaming，`streaming: bool` 字段为 capability 预留位）
- ❌ **不**实现 token bucket / API budget 排队（OPERABILITY.md §3.1 范围，由 M5 Scheduler 或独立 proposal 落地；本提案 `messages_create` 只透传 Anthropic 原生 429 / 529 错误，不在 backend 层做配额管理）
- ❌ **不**实现 cassette **录制模式**（M2 集成测试用预录制的 cassette；录制工具未来再加，本提案的 PlaybackBackend 是纯回放）
- ❌ **不**实现 fallback model 降级（OPERABILITY.md §3.3 标注"待 M2 后评估"；本 proposal `agent.fallback_model` 字段预留但 Agent loop 实施时再消费）
- ❌ **不**做 backend 版本兼容协商（capability 字段未来按需扩展，不维护向后兼容矩阵）
- ❌ **不**改 M0 / M1 任何现有契约（CLI 命令 / config loader / Inspector schema / ExecutionTarget Protocol 不变）

## 功能 (Capabilities)

### 新增功能

- `llm-backend-protocol`: `LLMBackend` Protocol 与 `BackendCapabilities` / `MessageResponse` / `ContentBlock` 数据模型；`BackendDiagnostics` Protocol；`BackendCapabilityViolation` / `BackendError` / `BackendUnavailable` / `BackendRateLimited` / `BackendDaemonUnsafe` 异常语义；`Settings.backend` / `Settings.agent` 两个 namespace 的配置 schema；`create_backend` 工厂；`AnthropicAPIBackend` / `FakeBackend` / `PlaybackBackend` 三个实现的契约与 capability 取值。

### 修改功能

- `core-services`: `Settings` 增加 `backend` 与 `agent` 两个 sub-section；配置加载时 `${ENV_VAR}` 占位展开规则扩展到 `backend.api_key` 字段；`SecretStr` 类型在 `model_dump_json` / `model_dump(mode="json")` 自动脱敏为 `"**********"`（防止 doctor JSON 输出泄露 API key；纯 `model_dump()` 保留 SecretStr 对象，业务代码通过 `.get_secret_value()` 解包）。

## 影响

### 对外契约影响

- **新增 Python public API（M2 落地后用户可 import）**：
  - `hostlens.agent.backend.LLMBackend` / `BackendCapabilities` / `MessageResponse` / `ContentBlock` / `TextBlock` / `ToolUseBlock`
  - `hostlens.agent.backend.BackendDiagnostics` / `BackendHealth` / `QuotaStatus`
  - `hostlens.agent.backend.create_backend` / `is_daemon_mode`
  - `hostlens.agent.backends.anthropic_api.AnthropicAPIBackend`
  - `hostlens.agent.backends.fake.FakeBackend`
  - `hostlens.agent.backends.playback.PlaybackBackend` / `CassetteMiss`
  - `hostlens.core.exceptions.BackendError` / `BackendUnavailable` / `BackendRateLimited` / `BackendCapabilityViolation` / `BackendDaemonUnsafe`
- **M0 异常完整性测试更新（同 PR 必修）**：M0 `tests/core/test_exceptions.py::test_module_exports_exactly_four_exception_classes` 已在 `add-tool-registry-capability-layer` 提案中演进为「恰好 6 个」（增加 `ToolError` / `ToolPolicyViolation`）。本提案再增加 5 个 backend 异常子类，需要把断言推进为「恰好 11 个」，并同步更新 `src/hostlens/core/exceptions.py` 的 `__all__`。
- **新增 runtime 依赖**：
  - `anthropic>=0.45` —— `AnthropicAPIBackend` 直接依赖官方 SDK 透传请求；M2 必须 pin 到带 `cache_control` block 与 `tool_use` 完整支持的版本
  - 不引入 `boto3` / `google-cloud-aiplatform`（M2 范围不含 Bedrock / Vertex）
- **新增 dev 依赖**：无（cassette 用 JSON Lines 自实现，不引入 `vcrpy`；理由：vcrpy 在 HTTP 层拦截，与 backend 层抽象正交，本提案在 backend 层做 cassette 让 LLM 决策路径可独立 replay）
- **配置文件破坏性变更**：M0 / M1 的 `Settings` 没有 `backend` / `agent` namespace；M2 起 `~/.config/hostlens/config.yaml` 必须含 `backend.type` 字段才能调 `create_backend`。M0 / M1 既有命令（`hostlens doctor`、`hostlens inspect`）在 M2 起调 `create_backend` 路径上加载，但**老配置不强制升级** —— `Settings` 解析时 `backend` 字段缺省允许（保留向后兼容），仅在 Agent loop 真正需要 backend 时才 raise `ConfigError("backend.type required")`；同 PR 在 `docs/MIGRATION.md` 写明从 M1 升级到 M2 的最小配置 diff
- **doc 修订**：CLAUDE.md §4.11 第 2 条「backend 严格透传不做静默丢弃」与本 proposal 的 `BackendCapabilityViolation` 实现严格对齐，无措辞改动；docs/ARCHITECTURE.md §9「模型层」段落已含完整设计，本 proposal 把"M2 必须交付"清单从设计稿转成 spec scenario，无文档大改

### Failure Modes

| 故障场景 | 表现 | 降级行为 |
|---|---|---|
| `AnthropicAPIBackend` 收到 401 / 403（API key 无效） | SDK 抛 `AuthenticationError` | `messages_create` 包装成 `BackendError("authentication failed", kind="auth_invalid")` raise；不重试；CLI 入口捕获后提示用户检查 `ANTHROPIC_API_KEY`；doctor 在 `health_check` 阶段早于业务路径发现 |
| Anthropic 429（带 retry-after） | SDK 抛 `RateLimitError` | 包装成 `BackendRateLimited(retry_after_seconds=<header value>)` raise；**backend 层不重试**，重试由 Agent loop（`add-agent-loop-skeleton`）按 ARCHITECTURE.md §9 Failure Semantics 表实现（最多 3 次 honor retry-after） |
| Anthropic 529 (overloaded) | SDK 抛 `anthropic.OverloadedError`（或任意 `APIStatusError` 子类满足 `exc.status_code == 529`）| 包装成 `BackendRateLimited(retry_after_seconds=None)` raise；Agent loop 固定退避 30s 最多 2 次 |
| Anthropic 5xx / 网络超时 | SDK 抛 `APIConnectionError` / `APITimeoutError` | 包装成 `BackendUnavailable(cause=...)` raise；Agent loop 指数退避 1s/4s/16s 最多 3 次 |
| Agent loop 误传 `cache_control` 到不支持 prompt caching 的 backend（未来 backend 实现） | Backend 检查 `not self.capabilities.prompt_caching and any(...cache_control...)` | **必须** raise `BackendCapabilityViolation`；不允许静默丢弃 `cache_control` 字段；CI 在单测覆盖此路径 |
| `PlaybackBackend` cassette 找不到匹配 request | `messages_create` 查表 miss | Raise `CassetteMiss(request_key=<hash>, cassette_path=<path>)`；**禁止**回落到真实 API 调用（防止 CI 静默打到生产） |
| `FakeBackend` 配置 responses 用尽 | `messages_create` 调用次数超过预设 | Raise `IndexError("FakeBackend exhausted: ...")`；测试用例需要在 setup 时配够响应或验证调用次数 |
| 配置 `backend.type = "claude_subscription"` 但本 proposal 未实现 | `create_backend` 分派阶段 | Raise `NotImplementedError("backend type claude_subscription 将在 M10.5 落地；当前请使用 anthropic_api")`；fail-fast 不进 Agent loop |
| `ensure_safe_for_daemon` 在 daemon 模式拒绝（未来 `ClaudeSubscriptionBackend` 用） | daemon 启动时 `create_backend` 调用 | Raise `BackendDaemonUnsafe(backend_name=..., reason=...)`；Scheduler daemon 进程 exit 1（详见 OPERABILITY.md §3.4） |

### Operational Limits

| 维度 | 上限 / 行为 |
|---|---|
| `create_backend` 总耗时 | <500ms（含 `AnthropicAPIBackend` 客户端初始化与 capability 探测） |
| `AnthropicAPIBackend.health_check` 耗时 | <5s（1 次 messages.create with ≤10 input tokens；timeout 由调用方控制） |
| 单次 `messages_create` 默认 timeout | 由 Agent loop 入参控制（默认 60s）；backend 层不强加内部 timeout |
| 并发预算 | 不适用（M2 单 Agent loop 同时只发 1 个 `messages.create`；并行 tool_use 由 Anthropic API 端 fanout，backend 不在客户端做 fanout） |
| 内存预算 | `AnthropicAPIBackend` 实例 ≤2 MB（含 SDK client 初始化）；`PlaybackBackend` cassette 按需 lazy load，≤50 MB（大 cassette 单元）；`FakeBackend` 实例 ≤500 KB |
| Anthropic SDK 内部重试 | **禁用** —— `AnthropicAPIBackend` 构造 client 时显式 `max_retries=0`；重试策略归 Agent loop 单一收口（ARCHITECTURE.md §9 Failure Semantics 表），避免双层重试放大配额消耗 |
| `PlaybackBackend` cassette 单元最大 | 10K 条 request/response（足够覆盖 1.0 前所有集成测试用例） |

完整运维约束（daemon 并发 / API quota / 报告存储等）见 [docs/OPERABILITY.md](../../../docs/OPERABILITY.md)，本提案**不**引入新的运维约束（沿用 §1 并发预算 / §3 配额 / §3.4 backend 选型 / §3.2 限流策略）。

### Security & Secrets

- **引入新密钥**：`ANTHROPIC_API_KEY`（环境变量优先；明文 yaml 仅 dev，doctor warning）—— 已在 OPERABILITY.md §7.1 密钥来源优先级表覆盖，本 proposal 不增加新规则
- **API key 脱敏**：
  - `Settings.backend.api_key` 类型为 Pydantic v2 `SecretStr`；`model_dump_json()` 与 `model_dump(mode="json")` 输出 `"**********"`；纯 `model_dump()` 保留 SecretStr 对象（解包走 `.get_secret_value()`）
  - `hostlens doctor --json` 输出 backend 字段时**禁止**含 api_key 完整值；只能输出 `api_key_set: bool` 与 `api_key_fingerprint: str`（前 4 + 后 4 字符按 OPERABILITY.md §7.2 默认脱敏规则）
  - `AnthropicAPIBackend` 日志（structlog）的 `__repr__` 不含 api_key（覆盖 `__repr__` 显式过滤 `_client` 字段）
- **base_url 脱敏**：自建代理 URL 可能含路径中的 token（`https://proxy.internal/team-X/`），日志输出走 `core.redact.redact_url`
- **cassette 文件安全**：
  - `PlaybackBackend` 读取的 cassette **必须**在仓库内 commit 时通过 OPERABILITY.md §7.2 脱敏规则扫描；本 proposal 提供 `cassette_lint` 工具脚本（`scripts/cassette_lint.py`），CI 跑一次确认所有 `tests/fixtures/cassettes/*.jsonl` 不含 Anthropic API key / Bearer token / JWT 形式串
  - cassette 内的 `usage.cache_creation_input_tokens` 等纯数字字段不脱敏
- **不扩大攻击面**：
  - `AnthropicAPIBackend` 只调 Anthropic 官方 endpoint（默认 `https://api.anthropic.com`），`base_url` override 走 `Settings.backend.base_url` 显式配置；不动态发现 endpoint
  - `BackendCapabilityViolation` / `BackendError` 子类的 `__str__` 不含 api_key / cassette path 完整路径（只输出 `<cassette>/...filename.jsonl` 形式相对路径片段）

### Cost / Quota Impact

- **M2 单次 `hostlens inspect --intent "..."` 估算（5-8 turns）**：
  - System prompt + tools schema（cached after first hit）：≈3K cache_creation_input_tokens（首次）+ 后续 cache_read ≈300 tokens × N turns
  - Messages 历史（不 cache）：≈2K-10K input tokens per turn（取决于 tool_result 大小）
  - Output：≈500-2K output tokens per turn
  - **总成本**：单次 inspect ≈10K-50K input + 5K-15K output tokens（按 Anthropic 当前定价 ≈$0.20-$0.80）
- **`AnthropicAPIBackend.health_check`**：≤10 input + ≤10 output tokens × 每次 `hostlens doctor` 调用，对配额影响可忽略
- **不改变** OPERABILITY.md §3.1 已声明的 quota 估算上界（`per_day_total_tokens` 默认 5M）
- **CI 成本**：本 proposal 所有集成测试走 `PlaybackBackend` cassette 回放，**零** Anthropic API 调用（CI 不消耗任何 token）

### Demo Path

M2 落地后（实施完成时）应能在 5 分钟内 reproduce：

```bash
# 干净 venv（M0 已支持）
pip install -e ".[dev]"

# 步骤 1：验证 capability 声明与工厂分派（不需要真实 API key）
python -c "
from hostlens.core.config import Settings
from hostlens.agent.backend import create_backend, BackendCapabilities

# fake backend 不需要 API key
settings = Settings(backend={'type': 'fake'}, agent={'primary_model': 'claude-opus-4-7'})
backend = create_backend(settings)
print(f'name={backend.name} caps={backend.capabilities}')
"
# 期望输出: name=fake caps=BackendCapabilities(prompt_caching=True, tool_use=True, ...)

# 步骤 2：验证 FakeBackend 走通 messages_create 与 capability gate
python -c "
import asyncio
from hostlens.agent.backend import MessageResponse, TextBlock, Usage
from hostlens.agent.backends.fake import FakeBackend

response = MessageResponse(
    id='msg_demo', model='claude-opus-4-7', role='assistant',
    content=[TextBlock(type='text', text='hello')],
    stop_reason='end_turn',
    usage=Usage(input_tokens=10, output_tokens=2, cache_creation_input_tokens=0, cache_read_input_tokens=0),
)
backend = FakeBackend(responses=[response])
result = asyncio.run(backend.messages_create(
    model='claude-opus-4-7', system='you are helpful', messages=[{'role':'user','content':'hi'}],
    tools=[], max_tokens=100, timeout=60.0,
))
print(f'stop_reason={result.stop_reason} text={result.content[0].text}')
"
# 期望输出: stop_reason=end_turn text=hello

# 步骤 3：验证 PlaybackBackend 走 cassette 回放
python -c "
import asyncio
from pathlib import Path
from hostlens.agent.backends.playback import PlaybackBackend

cassette = Path('tests/fixtures/cassettes/list_inspectors_demo.jsonl')
backend = PlaybackBackend(cassette_path=cassette)
# cassette 内首条 request 与下方完全匹配（含 model+messages+tools 哈希一致）
result = asyncio.run(backend.messages_create(
    model='claude-opus-4-7', system='SYS', messages=[{'role':'user','content':'list inspectors'}],
    tools=[], max_tokens=100, timeout=60.0,
))
print(f'replayed stop_reason={result.stop_reason}')
"
# 期望输出: replayed stop_reason=tool_use (cassette 内预录制的响应)

# 步骤 4：单元测试 + 集成测试
pytest tests/agent/test_backend_*.py tests/agent/backends/ -v
# 期望: 所有 capability gate / 错误包装 / cassette miss / fake exhausted / daemon-safe 测试通过
```

**真实 Anthropic API 调用**（需要 `ANTHROPIC_API_KEY`）的 smoke test 不放在 demo path（CI 不跑），但 `tests/agent/backends/test_anthropic_api_live.py` 提供一个 `@pytest.mark.live` 标记的测试，开发者本地用 `pytest -m live` 跑一次确认 SDK 集成正确。

完整 demo（含 Agent loop 调用真实 Inspector 跑完一次 inspect）依赖 M1 ExecutionTarget + Inspector 与 M2 `add-agent-loop-skeleton` 落地，因此本 proposal 的 demo 是 **backend-only 路径**：验证 backend 抽象、capability 声明、3 个实现的可加载与可调用。
