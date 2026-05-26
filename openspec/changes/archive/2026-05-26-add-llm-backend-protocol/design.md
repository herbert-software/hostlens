## 上下文

M0 已落地项目脚手架（CLI / config / logging / 异常基类），M1 三件套（ExecutionTarget / Inspector / Report）已归档。M2 `add-tool-registry-capability-layer` 已归档，定义了 Agent loop ↔ 能力层契约，明确把 `LLMBackend` 排除在 `ToolContext` 之外。本提案是 M2 `add-agent-loop-skeleton` 的最后一块前置依赖 —— Agent loop 不能直接 `import anthropic`，必须通过本提案定义的 `LLMBackend` Protocol 调模型。

约束：

- CLAUDE.md §4.11 把抽象形状钦点为 Anthropic-schema-first 薄层（不是 vendor-agnostic）；4 条硬规则不可让步：
  1. Backend 注入 `AgentLoop.__init__`，不进 `ToolContext`（ADR-008）
  2. `cache_control` 由 Agent loop 按 capability 决定是否注入；backend 严格透传不做静默丢弃，不一致时 `BackendCapabilityViolation`
  3. `ClaudeSubscriptionBackend` daemon 模式强制 raise（M10.5 落地，本提案预留接口与异常）
  4. `backend:` / `agent:` 配置分两个 namespace
- docs/ARCHITECTURE.md §9「模型层」已含 Protocol 完整签名、实现矩阵、ToS 风险表，本提案落地需严格对齐
- 不能引入"provider-agnostic LLM 抽象"幻觉 —— 那是 LangChain / LiteLLM 的事；本提案只覆盖「换认证 / 换 endpoint」用例

利益相关者：

- M2 `add-agent-loop-skeleton` 实施者（最终消费者；loop.py 直接 `await backend.messages_create(...)`）
- M10.5 `add-bedrock-backend` / `add-claude-subscription-backend` 实施者（按本提案定的 Protocol 与 `BackendDiagnostics` 接口添加实现）
- M5 Scheduler 实施者（daemon 启动时调 `is_daemon_mode` + `ensure_safe_for_daemon` 强制守门）
- CI 测试集（依赖 `PlaybackBackend` + cassette 走零成本集成测试）

## 目标 / 非目标

**目标：**

- 把"Agent ↔ LLM"抽象成 `LLMBackend` Protocol + `BackendCapabilities` dataclass + `MessageResponse` Pydantic 模型；Agent loop 不再依赖 `anthropic` 模块
- 把"backend 健康/配额/daemon-safe 探测"抽到独立 `BackendDiagnostics` Protocol（duck-type；可选实现），让 `hostlens doctor` 统一调度
- 交付 3 个 backend 实现满足 M2 范围：`AnthropicAPIBackend`（生产路径）/ `FakeBackend`（单测）/ `PlaybackBackend`（集成测试 cassette 回放）
- 把 capability gate 写成 backend 端的硬约束：`BackendCapabilityViolation` 在 `cache_control` 与 `capabilities.prompt_caching` 不一致时**必须** raise，不允许静默丢弃
- 配置 schema `Settings.backend` / `Settings.agent` 两 namespace 切干净；`backend.type` 用 `Literal` 限定取值，未来 backend 提前在 schema 占位但加载时 `NotImplementedError`
- 单点工厂 `create_backend(settings)` 是唯一构造入口；daemon 模式 hook 通过 `is_daemon_mode` 函数化（M5 落地时改实现，不动调用点）
- 全部敏感字段（API key / base_url / cassette path）走 OPERABILITY.md §7 脱敏规则，doctor JSON 输出永不泄露

**非目标：**

- ❌ `BedrockBackend`（M10.5；需要 `boto3` 依赖与 IAM 认证流程）
- ❌ `VertexBackend`（1.0 后；需要 `google-cloud-aiplatform`）
- ❌ `ClaudeSubscriptionBackend`（M10.5 experimental；本提案预留 `BackendDaemonUnsafe` 异常与 `ensure_safe_for_daemon` 接口）
- ❌ Agent loop 本体（`add-agent-loop-skeleton` proposal 范围）
- ❌ Streaming 调用模式（`streaming: bool` capability 字段预留但实现全置 False）
- ❌ Token bucket / API budget 排队（OPERABILITY.md §3.1；M5 Scheduler 或独立 proposal 落地）
- ❌ Cassette **录制** workflow（M2 集成测试用手写 cassette；录制工具未来再加）
- ❌ Fallback model 降级机制（`agent.fallback_model` 字段预留但 Agent loop 实施时再消费）
- ❌ Backend 版本兼容协商 / capability 字段向后兼容矩阵

## 决策

### D-1 Protocol 形状 —— Anthropic-schema-first 薄抽象

**选择：** `LLMBackend.messages_create(*, model, system, messages, tools, max_tokens, timeout) -> MessageResponse`，`system` / `messages` / `tools` 参数类型镜像 Anthropic Messages API（dict 结构原样透传）。

**替代方案：**

- (A) **Vendor-agnostic 抽象**（`messages: list[Message]`，自定义 `Message` Pydantic 类替代 Anthropic dict 结构）→ **否决**。理由：CLAUDE.md §4.11 第 4 条明确「不是 vendor-agnostic 通用包装」；自定义 Message 类需要双向转换层 = 维护负担 + 隐藏 Anthropic 新特性。
- (B) **更接近 OpenAI Chat Completions API 风格的 `chat_completion`** → **否决**。理由：Anthropic 是项目唯一 LLM 选型；模拟 OpenAI 风格反而增加阻抗失配（system role 处理 / tool_use 结构差异）。
- (C) **拆成多个细粒度方法**（`send_message` / `send_with_tools` / `send_with_cache`）→ **否决**。理由：Anthropic SDK 本身是单方法多参数；拆细方法增加 backend 实现复杂度且未提升测试性。

**理由：** Anthropic Messages API 的 dict 结构在 SDK 与官方文档间是稳定的语义契约，原样透传让 backend 实现最薄、Agent loop 阅读最直观（HR/面试官读 `loop.py` 时不需要先理解一层自定义类型映射）。`system` / `messages` / `tools` 用 `list[dict]` 而非自定义 Pydantic 类是 conscious trade-off：失去类型安全换取契约稳定性 + 实现薄度。`MessageResponse` 是**返回**端的薄包装（用 Pydantic 类型化关键字段），让 Agent loop 端的代码不需要处理 SDK 内部的 raw object。

### D-2 `cache_control` 由 Agent loop 控制，backend 严格透传

**选择：** Agent loop 在调用 `messages_create` 前根据 `backend.capabilities.prompt_caching` 自行决定是否在 `system` block 注入 `cache_control: {"type": "ephemeral"}`；backend **严格透传**入参；若 backend `capabilities.prompt_caching=False` 但入参含 `cache_control` block，**必须** raise `BackendCapabilityViolation`，**禁止**静默丢弃。

**替代方案：**

- (A) **Backend 自动 strip 不支持的 `cache_control`**（容错）→ **否决**。理由：让"是否走 cache"的决策路径不可观察 —— prompt cache miss rate 指标会因 backend 静默丢弃而失真，发现不了线上 prompt 没被缓存的 bug。CLAUDE.md §4.11 第 2 条明确点名拒绝这种容错方案。
- (B) **Backend 自动 inject `cache_control`**（封装）→ **否决**。理由：把 cache 策略从 Agent loop 隐藏到 backend，让"哪些 block 该缓存"的决策不再属于 Agent loop —— 违反「Agent loop 拥有 prompt caching 控制权」的核心展示点（CLAUDE.md §4.8）。

**理由：** 让 capability 不一致变成可见的 fail-fast。`BackendCapabilityViolation` 在测试覆盖（FakeBackend 配置 `prompt_caching=False` + Agent loop 传 cache_control）下能立刻命中，是项目层级的「不允许 silent failure」工程纪律体现。生产路径上 AnthropicAPIBackend 的 `capabilities.prompt_caching=True`，正常 path 不会触发此异常。

### D-3 `BackendDiagnostics` 是独立可选 Protocol（duck-type）

**选择：** `BackendDiagnostics` 是与 `LLMBackend` **独立**的 Protocol（不继承、不组合）；backend 实现可选实现；`hostlens doctor` 用 `isinstance(backend, BackendDiagnostics)` 检测后调用（duck-type）。

**替代方案：**

- (A) **把 diagnostics 方法直接加到 `LLMBackend`** → **否决**。理由：`PlaybackBackend` 不需要 health_check（cassette 没有"真实健康"概念）；强制所有 backend 实现 diagnostics 会污染 Protocol。
- (B) **`LLMBackend` 继承 `BackendDiagnostics`**（强制）→ **否决**。理由：同 (A)，FakeBackend / PlaybackBackend 不应被强制实现 quota_check。
- (C) **Diagnostics 用 mixin 而非独立 Protocol** → **否决**。理由：Protocol 是 structural typing，mixin 是 nominal typing；混用会让类型检查矛盾（mypy `--strict` 友好度）。

**理由：** Duck-type 让 backend 实现负担最小，doctor 调用点的 `isinstance` 检查显式可读。`BackendDiagnostics` 的三个方法（`health_check` / `quota_check` / `ensure_safe_for_daemon`）职责单一，未来扩展（如 `latency_check`）也走加方法不破坏现有实现。

### D-4 `is_daemon_mode` 是函数化 hook（M2 stub）

**选择：** `is_daemon_mode(settings: Settings) -> bool` 在本提案 M2 范围内**永远返回 False**；定义函数签名让 `create_backend` 调用点稳定，M5 Scheduler 落地时改实现即可不动 `create_backend`。

**替代方案：**

- (A) **`is_daemon_mode` 检测环境变量 `HOSTLENS_DAEMON=1`** → **否决**。理由：M2 范围内 daemon 模式不存在，提前定义环境变量等于污染 ABI；M5 Scheduler 实施时再决定是用环境变量、CLI flag、还是 process group 检测。
- (B) **`create_backend` 直接接 `daemon: bool` 参数**（移除 hook 函数）→ **否决**。理由：daemon 状态是 process-wide 全局属性，由 `create_backend` 调用点（如 CLI 入口 / Scheduler 守护进程）通过参数手动传递容易遗漏；用一个统一函数 `is_daemon_mode(settings)` 让"如何判断是否 daemon"成为单一决策点。

**理由：** 提前定义函数签名 + M2 stub 实现 = 让 M5 Scheduler 落地时只改一个函数体不改任何调用点。M2 测试时手动 monkey-patch `is_daemon_mode` 验证 `ensure_safe_for_daemon` 调用路径。

### D-5 `AnthropicAPIBackend` 内部 `max_retries=0`，重试归 Agent loop 独占

**选择：** 构造 Anthropic SDK client 时显式 `max_retries=0`，关闭 SDK 内部重试机制；所有 429 / 529 / 5xx 直接以 `BackendRateLimited` / `BackendUnavailable` raise，由 Agent loop 按 ARCHITECTURE.md §9 Failure Semantics 表统一处理重试策略。

**替代方案：**

- (A) **保留 SDK 默认 `max_retries=2`** → **否决**。理由：双层重试（SDK + Agent loop）放大配额消耗；SDK 重试不感知 Agent loop 的"已采集 finding 部分报告输出"降级策略，会让降级路径不可控。
- (B) **SDK 重试 + Agent loop 不重试** → **否决**。理由：把重试策略从核心展示点（loop.py 看得见的代码）藏到 SDK 实现细节 —— 违反「Agent loop 拥有重试控制权」的简历可读性目标。

**理由：** 让重试策略集中在 Agent loop 一处可见（`agent/loop.py` 的 Failure Semantics 处理代码），是项目核心展示点。`max_retries=0` 是 Anthropic SDK 显式关重试的官方参数。

### D-6 `PlaybackBackend` cassette miss 必须 raise（不静默打真实 API）

**选择：** `PlaybackBackend.messages_create` 查 cassette 找不到匹配 request 时 **必须** raise `CassetteMiss(request_key, cassette_path)`；**禁止**回落到真实 API 调用（即使 `ANTHROPIC_API_KEY` 在环境变量中）。

**替代方案：**

- (A) **Miss 时调真实 API 并自动追加到 cassette**（录制模式）→ **否决**。理由：CI 在 miss 时会静默打到生产 Anthropic（消耗真实 token + 测试结果不可重现）；录制 workflow 是独立功能（非本提案范围），不能与回放路径混合。
- (B) **Miss 时返回固定 stub response** → **否决**。理由：让"是否真的回放了 cassette"不可观察；测试断言可能误判通过。

**理由：** Fail-fast 是测试 backend 的核心价值。`CassetteMiss` 异常带 `request_key` 让开发者立刻知道哪个 request 需要补录。录制 workflow 未来作为独立工具脚本（`scripts/record_cassette.py`）落地，不污染 `PlaybackBackend` 实现。

### D-7 `Settings.backend` 与 `Settings.agent` 分 namespace

**选择：** 严格按 CLAUDE.md §4.11 第 4 条把配置分两个 sub-section：

```yaml
backend:                                  # "与谁通信 / 如何认证"
  type: anthropic_api                     # Literal["anthropic_api", "fake", "playback", "bedrock", "vertex", "claude_subscription"]
  api_key: ${ANTHROPIC_API_KEY}
  base_url: null
  cassette_path: null                     # type=playback 必填
  # type=bedrock / vertex / claude_subscription: 字段在 schema 占位但 M2 加载时 NotImplementedError

agent:                                    # "用哪个模型 / 行为参数"
  primary_model: claude-opus-4-7
  fallback_model: claude-haiku-4-5        # M2 字段预留，Agent loop 不消费
  max_turns: 20
  token_budget_input: 100000
  token_budget_output: 30000
```

**替代方案：**

- (A) **扁平 `Settings.llm.{api_key, model, max_turns, ...}`** → **否决**。理由：让"换 backend"与"换 model"两件事变化频率不同的配置混在一个 namespace；未来切 Bedrock 时需要改 `llm.api_key` → `llm.aws_region`，但 `llm.model` 不变 —— 这种语义差异要在 schema 层体现。
- (B) **`agent.backend.{...}` 嵌套**（backend 是 agent 的子配置）→ **否决**。理由：backend 是「认证 + 通信」属性，跨多个 agent 使用场景（Planner / Diagnostician 共享同一 backend）；让 backend 嵌套在 agent 下意味着多 agent 必须各自声明 backend，破坏共享配置。

**理由：** 两个 namespace 让 yaml 文件读起来语义清晰：换公司部署 = 改 `backend` 节；调实验参数 = 改 `agent` 节。M0 / M1 既有配置不强制升级（`backend` 字段缺省允许），只在调 `create_backend` 时才校验 —— 平滑升级路径。

### D-8 `MessageResponse` 是 Pydantic 类不是 dict（仅返回端类型化）

**选择：** `MessageResponse` 是 Pydantic v2 `BaseModel`，字段镜像 Anthropic SDK `Message` 对象关键字段；`content` 字段是 `list[ContentBlock]`，其中 `ContentBlock = TextBlock | ToolUseBlock`（discriminated union by `type` field）。

**替代方案：**

- (A) **`MessageResponse` 仍用 dict** → **否决**。理由：Agent loop 端需要 `response.stop_reason` / `response.content[0].text` 这种属性访问；dict 形式让 loop 代码到处 `response["stop_reason"]` 难读。
- (B) **直接复用 Anthropic SDK `Message` 类型** → **否决**。理由：`FakeBackend` / `PlaybackBackend` 不依赖 Anthropic SDK 也能构造响应；让 backend 间共享一个 SDK 类型会强制所有 backend 引 `anthropic` 包。

**理由：** 入参（`messages` / `tools`）用 dict 透传保最大兼容，返回（`MessageResponse`）用 Pydantic 类型化让 Agent loop 端阅读体验好。这是 conscious 不对称：dict-in / typed-out。

### D-9 Cassette 文件格式选 JSON Lines

**选择：** Cassette 文件用 JSON Lines（每行一个 `{"request": {...}, "response": {...}}`），不用 vcrpy / yaml。

**替代方案：**

- (A) **vcrpy** → **否决**。理由：vcrpy 在 HTTP 层拦截，与 backend 层抽象正交（PlaybackBackend 在 backend 层做 cassette，让"LLM 决策路径"独立可 replay 而不依赖 SDK 内部 HTTP 实现）。
- (B) **YAML** → **否决**。理由：cassette 可能含转义敏感字符（API response 中的 JSON 嵌套）；JSON Lines 解析最简单，每行独立可 grep，易做 cassette_lint 脱敏扫描。

**理由：** JSON Lines 实现最简、可 grep、cassette_lint 易写、CI diff 易读。

### D-10 异常体系一次性扩到 11 个

**选择：** 本提案在 `core/exceptions.py` 新增 5 个异常类（`BackendError` / `BackendUnavailable` / `BackendRateLimited` / `BackendCapabilityViolation` / `BackendDaemonUnsafe`），同时更新 `tests/core/test_exceptions.py` 的"恰好 N 个"断言为 11（之前是 6）。

**替代方案：**

- (A) **只加 1 个 `BackendError` 基类，子类型用 `kind: Literal[...]`** → **否决**。理由：429 / 5xx / capability violation 是结构上不同的故障域（重试策略、用户可见消息、retry-after 字段语义都不一样），用单一类 + kind 字段会让调用方写 `if e.kind == "rate_limited" and e.retry_after:` 这种依赖 kind 二次分派 —— 不如直接 `except BackendRateLimited as e:`。
- (B) **不加异常类，直接 re-raise Anthropic SDK 异常** → **否决**。理由：Anthropic SDK 异常体系是实现细节，未来切 Bedrock / Vertex 时 SDK 不同；backend 层异常包装让上层（Agent loop / CLI）有稳定接口。

**理由：** 5 个异常的差异化值得各自一个类。结构化字段（`retry_after_seconds` / `cause` / `reason`）让上层 except 块精准捕获 + 取数据，不需要二次 isinstance 检查 kind。

## 风险 / 权衡

- **风险**：未来 Anthropic SDK API 形状变化（如 `messages.create` 重命名 / 参数调整）→ **缓解**：`AnthropicAPIBackend` 是单点 adapter，SDK 变化时只改一个文件；live smoke test（`@pytest.mark.live`）是 SDK 兼容性最终验收门，本地开发者跑一次就能发现 SDK 升级破坏；pyproject.toml 用 `anthropic>=0.45,<2` 允许 0.x / 1.x 升级但屏蔽 2.0 主版本跨越（pre-1.0 SDK 仍可能在 0.x → 1.x 跳变，固定 `<1.0` 等于永远不升级）；dependabot 升级走 §15.2 流程 + live smoke 把关。
- **风险**：JSON Lines cassette 格式与 Anthropic SDK 实际 response 形状偏离（如 SDK 新增字段未在 cassette 反映）→ **缓解**：`PlaybackBackend` 加载 cassette 时用 `MessageResponse.model_validate` 解析，extra 字段忽略（不 fail），missing 必填字段 fail-fast；cassette_lint 工具单独跑一遍 schema 校验。
- **风险**：`BackendCapabilityViolation` 在生产路径（AnthropicAPIBackend `prompt_caching=True`）永远不被触发，相当于死代码 → **缓解**：单测 `tests/agent/backends/test_fake_capability_gate.py` 用 `FakeBackend(capabilities=BackendCapabilities(prompt_caching=False, ...))` 模拟未来 backend 场景，覆盖 violation 路径。
- **权衡**：`messages_create` 入参 `system: list[dict] | str` 类型联合反映 Anthropic API 真实支持两种形式（plain string 或 block list）→ 让 backend 实现需要 handle 两种 case；本提案选择透传不做规范化（**Anthropic-schema-first** 原则）；Agent loop 端如需统一处理可在 loop 层做一次 normalize。
- **权衡**：`is_daemon_mode` M2 永远返回 False 让 `ensure_safe_for_daemon` 在 M2 范围内"没有真实触发场景"→ 通过单测 monkey-patch 验证调用路径，避免 M5 Scheduler 落地时发现 `ensure_safe_for_daemon` 从未被真实调用过的 bug。
- **权衡**：本提案不实现 cassette 录制工具，开发者新增 cassette 需要手写 JSONL → M2 集成测试只需要 3-5 条 cassette（覆盖 list_inspectors / list_targets / run_inspector 三个 demo tool_use 路径），手写成本可控；录制工具未来作为独立 proposal 落地。
- **权衡**：`anthropic>=0.45` runtime 依赖让 `pip install hostlens` 强引 SDK 100+ KB → 不可避免，AnthropicAPIBackend 是 M2 默认 backend；M10.5 引入 Bedrock 后 `anthropic[bedrock]` extra 会成为可选 dependency group，但 M2 范围保持简单。

## 迁移计划

本提案是 M2 引入，无历史用户数据需要迁移。M0 / M1 配置文件兼容性：

- M0 / M1 既有 `~/.config/hostlens/config.yaml` 没有 `backend` / `agent` namespace
- M2 起 `Settings` 解析允许缺省（向后兼容）；只在调用 `create_backend(settings)` 时才校验 `backend.type` 存在
- 同 PR 在 `docs/MIGRATION.md`（如不存在则新建）写明 M1 → M2 升级最小 diff：

```yaml
# 在 ~/.config/hostlens/config.yaml 顶部追加
backend:
  type: anthropic_api
  api_key: ${ANTHROPIC_API_KEY}

agent:
  primary_model: claude-opus-4-7
  max_turns: 20
  token_budget_input: 100000
  token_budget_output: 30000
```

回滚策略：

- 本提案变更可整体回滚（撤销 `feat/add-llm-backend-protocol` branch 的 squash commit）
- 回滚后 M1 状态完整可用（不依赖 backend 抽象）
- 已合并到 main 的配置 schema 改动通过保留 `backend` namespace 可选实现向后兼容；回滚后用户配置文件中的 `backend:` 节会被 M1 `Settings` 忽略（Pydantic v2 `extra="ignore"` 行为）

## 开放问题

- **Q1**：`AnthropicAPIBackend.quota_check` 在 M2 返回 `None`（Anthropic Console quota API 未公开标准接口）；未来是否切到读取 response header 中的剩余 token？→ **暂留作 1.0 后再评估**，本提案不实现。
- **Q2**：`agent.fallback_model` 字段预留是否在本提案的 `agent` namespace？还是单独留到 `add-agent-loop-skeleton`？→ **决策：留在本提案 schema**，Agent loop 实施时再消费；理由：配置 schema 一次定型避免后续配置文件破坏性变更。
- **Q3**：`PlaybackBackend` 的 cassette key normalization 算法（如何把 `messages_create` 入参哈希成 cassette key）？→ **本提案决定：key = SHA256(json.dumps({"model": ..., "messages": ..., "tools_count": len(tools)}, sort_keys=True))**；**key 排除的字段（trade-off）**：`system`（让 system iteration 不破 cassette）/ `max_tokens`（与响应等价性无关）/ `tools` 内容（仅 `tools_count` 入 key）/ `timeout`（client 侧参数）；schema drift 检测交给 `tools_schema_hash` lint-only metadata（详见 spec §需求:PlaybackBackend）；如未来发现 system / max_tokens 真实变化需要影响匹配，再扩 key。
- **Q4**：M2 是否提供 `hostlens backend doctor` 子命令独立测试 backend 连通性？→ **决策：不单独加子命令**，复用 `hostlens doctor`（已在 M0 落地），让 doctor 检测到 `Settings.backend.type` 存在时调用 `health_check`。
