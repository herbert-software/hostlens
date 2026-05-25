## 为什么

M2 Agent loop 需要一个稳定的"Agent ↔ 能力"入口：Planner Agent 不能直接 import Inspector / ExecutionTarget registry，否则把"业务插件契约"与"Agent capability 契约"混在同一个抽象里。CLAUDE.md §9 钦点"M0 完成后、M1 之前先起 `add-tool-registry-capability-layer`"，是因为 M1 的三个抽象（ExecutionTarget / Inspector / Report）需要按"未来被 Tool Registry 包装暴露给 Agent"的姿态设计，先把 Tool Registry 的 `ToolContext` / `ToolSpec` 契约定死，能避免 M2 集成时回头改 M1 接口。

同时，Tool Registry 与手写 Agent loop 并列为项目的"简历可读性"核心展示点 —— 双层 capability 模型（host-agnostic spec + surface adapter）是这个项目区别于 LangChain / FastAPI tool 路由的设计点，越早写成 spec 越能锚定差异化叙事。

## 变更内容

**新增（核心抽象）：**

- `ToolSpec` Pydantic 模型：含 input/output Pydantic schema、三 surface description（agent / mcp / cli）、policy 元数据（`surfaces` / `side_effects` / `requires_approval` / `permissions` / `sensitive_output` / `target_constraints` / `timeout` / `tags`）
- `ToolRegistry`：按 name 索引 ToolSpec，提供 `register` / `get` / `names` / `list_for(surface)` API
- `ToolContext`：依赖注入容器，M2 字段集 = `target_registry` + `inspector_registry` + `config` + `logger` + `approval_service` + `cancel`
- `@tool(...)` 装饰器：**纯 spec factory**，包装 handler 返回 `ToolSpec` 实例，不 mutate module-level/global state
- `register_default_tools(registry)`：显式装配函数，是 M2 Agent loop 启动时唯一注册路径
- `ToolError(HostlensError)` + `ToolPolicyViolation(ToolError)` 异常：携带结构化字段（`tool_name` / `surface` / `violated_field` / `reason`）

**新增（Layer 2 agent surface adapter）：**

- `hostlens.agent.tools_adapter.ToolsAdapter`：把 `surfaces ∋ "agent"` 的 ToolSpec 投成 Anthropic `tool_use` schema；dispatch 前强制校验 policy gate（surface 不匹配 → `ToolPolicyViolation`）；JSON Schema 由 adapter 在投影时从 Pydantic 模型生成（**不**进 ToolSpec 持久化）

**新增（M2 首批 3 个 ToolSpec）：**

- `run_inspector`：`side_effects="read"` / `sensitive_output=True` / `surfaces={"agent"}`
- `list_inspectors`：`side_effects="none"` / `sensitive_output=False` / `surfaces={"agent"}`
- `list_targets`：`side_effects="none"` / `sensitive_output=True` / `surfaces={"agent"}`；**`TargetSummary` 输出 schema 按 M7 MCP 投影安全标准设计**（不含 credentials / 路径 / 主机名 / IP / 端口 / 用户名 / env vars / raw config）

**修订（文档措辞，消除内部矛盾）：**

- CLAUDE.md §4.10 "6 条硬规则" 第 3 条：从 "必须走 `@tool` 注册" 改为 "必须声明为 `ToolSpec`，`@tool` 只能作为纯 spec factory，装配走 `register_default_tools(registry)`"
- docs/ARCHITECTURE.md §3 "6 条硬规则" 第 3 条：与 CLAUDE.md 保持完全一致的措辞

**非目标（Non-Goals）：**

- ❌ **不**实现 MCP surface adapter（推到 M7 `add-mcp-tool-adapter`）
- ❌ **不**实现 CLI surface adapter（暂未决定时机；M2 阶段 CLI 通过 `cli/` 直接调用 ToolRegistry.dispatch 即可，不需要专门 adapter）
- ❌ **不**实现写操作 / destructive ToolSpec（推到 M9 Remediation 与 `add-remediation-approval-flow`）
- ❌ **不**实现 `ApprovalService` 真实流程（M2 提供 `NoopApprovalService` stub，保 ABI 稳定）
- ❌ **不**实现 Inspector / ExecutionTarget 本体（M1 范围；本提案的 3 个首批 ToolSpec 在 M1 落地前是"声明 + 单测"，集成测试 stub `inspector_registry` / `target_registry`）
- ❌ **不**实现 `read_finding_detail` ToolSpec（M3 报告持久化时再讨论；M2 由 `run_inspector` 直接返回完整 finding 列表）
- ❌ **不**预先添加 `ToolContext` 字段 `report_store` / `run_history`（M3 再加，避免服务定位器化）
- ❌ **不**实现 LLMBackend（M2 范围内由独立 proposal `add-llm-backend-protocol` 处理；本 proposal 严格遵守 ADR-008：Backend 不进 ToolContext）
- ❌ **不**实现 token 预算 / max_turns 等 Agent loop 控制逻辑（M2 `add-agent-loop-skeleton` proposal 范围）
- ❌ **不**做 ToolSpec 的版本兼容协商（spec 版本号 `version: str` 字段保留但 M2 不实现 backward-compat 处理逻辑）

## 功能 (Capabilities)

### 新增功能

- `tool-registry-capability-layer`: Layer 1 的 `ToolSpec` / `ToolRegistry` / `ToolContext` 数据模型与 API 契约，`@tool` 纯 factory 模式，`register_default_tools` 显式装配约定，`ToolPolicyViolation` 异常语义，3 个首批 ToolSpec 的 input/output schema 与 policy 元数据。
- `agent-tool-adapter`: Layer 2 的 agent surface adapter。把 `surfaces ∋ "agent"` 的 ToolSpec 投成 Anthropic `tool_use` JSON Schema，dispatch 前 policy gate 校验，handler 异常包装成 tool_error 返回 Agent loop。

### 修改功能

无（M0 主规范 `cli-foundation` / `core-services` / `project-skeleton` 不变；CLAUDE.md §4.10 与 ARCHITECTURE.md §3 是项目级 doc 措辞修订，不算 spec capability 变更）。

## 影响

### 对外契约影响

- **新增 Python public API（M2 落地后用户可 import）**：
  - `hostlens.tools.base.ToolSpec` / `ToolContext` / `ToolHandler`
  - `hostlens.tools.registry.ToolRegistry` / `register_default_tools`
  - `hostlens.tools.decorators.tool`（`@tool` 装饰器）
  - `hostlens.agent.tools_adapter.ToolsAdapter` / `scrub_exception_message`
  - `hostlens.tools.schemas.list_targets.scrub_inventory_string`
  - `hostlens.core.exceptions.ToolError` / `ToolPolicyViolation`（扩展 M0 落地的 `HostlensError` 体系；`ToolPolicyViolation` 的 4 字段均为受约束取值域：`tool_name` 受 ToolSpec 正则约束，`surface` / `violated_field` / `reason` 均为 `Literal[...]`；**禁止**接受自由文本 reason 以防 prompt/log injection 与敏感数据泄露）
- **M0 异常完整性测试更新（同 PR 必修）**：M0 `tests/core/test_exceptions.py::test_module_exports_exactly_four_exception_classes` 断言"恰好 4 个"会与 M2 新增子类冲突。本提案 task §1.4 把该测试更新为断言"恰好 6 个"，并相应更新 `src/hostlens/core/exceptions.py` 的 `__all__`。M0 spec §需求:异常基类层次明确 §场景:M0 子类列表完整且最小 的"恰好 4 个"语义在 M2 后演进为"恰好 6 个"，由 M2 spec 显式声明此 invariant 变更。
- **新增 Pydantic schemas（首批 ToolSpec input/output）**：
  - `RunInspectorInput` / `RunInspectorOutput`
  - `ListInspectorsInput` / `ListInspectorsOutput`
  - `ListTargetsInput` / `TargetSummary` / `ListTargetsOutput`
- **doc 修订**：CLAUDE.md §4.10 / ARCHITECTURE.md §3 措辞变更，**不影响代码契约**
- **不影响**：M0 已落地的 CLI 命令（`hostlens doctor`）/ config schema / logging / 异常基类继承链（仅在子类层面扩展）

### Failure Modes

| 故障场景 | 表现 | 降级行为 |
|---|---|---|
| ToolSpec 注册时 `name` 冲突 | `ToolRegistry.register()` raise `ToolError("duplicate tool name")` | 启动失败，错误消息含两个冲突 ToolSpec 的 `__module__` 来源；fail-fast 不进 Agent loop |
| Agent loop 试图 dispatch `surfaces ∌ "agent"` 的 ToolSpec | adapter raise `ToolPolicyViolation(tool_name=..., surface="agent", violated_field="surfaces", reason="not_exposed_to_surface")` | Agent loop 捕获后向 LLM 返回 `tool_error` 让其重新选 tool；本次不阻塞整个 inspect 流程 |
| `ToolContext` 必填字段缺失（如 `target_registry=None`） | `ToolsAdapter.__init__` 时 raise `RuntimeError`（不进 `ToolPolicyViolation` 因为是配置 bug 不是 policy 拒绝） | 启动配置阶段 fail-fast，不进 Agent loop |
| Handler 内部异常（如 Inspector 加载失败） | adapter 捕获后包装为 `tool_error` 返回 LLM，结构化字段 `error_kind` / `tool_name` / `cause` | Agent loop 重试 1 次或转人工，依 `add-agent-loop-skeleton` 决定 |
| `list_targets` 返回的 `TargetSummary` 意外含敏感字段（实施 bug） | mypy --strict + 单测白盒 + spec scenario 红 | CI 阻断 PR；不进生产 |

### Operational Limits

| 维度 | 上限 / 行为 |
|---|---|
| `ToolRegistry` 启动注册总耗时 | <100 ms（纯内存，无 IO） |
| 单次 ToolSpec dispatch 默认 timeout | 60s（由 `ToolSpec.timeout` 覆盖；首批 `list_inspectors` / `list_targets` 默认 5s，`run_inspector` 默认 30s） |
| `ToolContext` 实例化（每次 Agent loop turn） | <10ms（无远程调用，仅 DI 容器构造） |
| Anthropic `tool_use` schema 投影后的 JSON 大小 | 单 ToolSpec ≤4 KB；3 个首批 ToolSpec 总和 ≤12 KB |
| 并发预算 | 不适用（M2 单 Agent loop 同时只发 1 个 `messages.create`；并行 tool_use 由 Anthropic API 端 fanout，本提案只保证 handler 异步安全可重入） |
| 内存预算 | `ToolRegistry` + 3 个 ToolSpec 静态结构 ≤500 KB；ToolContext 实例 ≤50 KB |

完整运维约束（daemon 并发 / API quota / 报告存储等）见 [docs/OPERABILITY.md](../../../docs/OPERABILITY.md)，本提案不引入新的运维约束。

### Security & Secrets

- **不引入**新密钥；不扩大攻击面
- **`list_targets` 输出 schema 严格脱敏**：`TargetSummary` 字段集 = `name` / `kind` / `display_name?` / `description?` / `capabilities` / `tags` / `enabled`；**禁止**任何形式出现 `password` / `token` / `private_key` / `ssh_key_path` / `connection_string` / `dsn` / `url` / `host` / `hostname` / `ip_address` / `port` / `username` / `env` / `secret_ref` / `raw_config` 字段或其内容子串
- **`sensitive_output` 标记取值（M2 阶段）**：
  - `run_inspector.sensitive_output=True`（Inspector 输出可能含 process list / open ports / network connections）
  - `list_inspectors.sensitive_output=False`（列表本质是项目元数据，Inspector name + description + 标签可公开）
  - `list_targets.sensitive_output=True`（即使过滤后 target name + kind + tags 仍透露环境结构，M7 MCP 投影时需要 policy gate）
- **`ToolPolicyViolation` 异常的 string repr 不可能含敏感字段值**：4 字段均为受约束取值域 —— `tool_name` 受 ToolSpec 正则 `^[a-z][a-z0-9_]*$` 约束，`surface` / `violated_field` / `reason` 均为 `Literal[...]`；非法 reason 在 `__init__` 阶段 raise `ValueError` 拒绝（**禁止**自由文本 reason 形成 prompt/log injection 入口）
- **`@tool` 装饰器与 `register_default_tools` 装配过程不产生任何 IO**（防止日志侧通道泄露注册元数据）

### Cost / Quota Impact

- 单次 Agent loop turn 注入的 `tools` array：首批 3 个 ToolSpec 投影后约 12 KB JSON ≈ 3K input tokens
- 走 `cache_control: ephemeral`（M2 `add-agent-loop-skeleton` 实现）后实际计费 input tokens：cache_creation 一次 ~3K + 后续 cache_read 每次 ~3K × 0.1 ≈ 300 tokens（按 Anthropic 当前定价）
- 对比裸 prompt 嵌入工具描述：节约约 90% input token 成本
- M2 单次 `hostlens inspect --intent "..."` 估计 5-8 turns × 300 tokens ≈ 2K input tokens（仅 tools 部分；不含 system prompt 与 messages 历史）
- **总成本影响**：极低；不改变 [docs/OPERABILITY.md](../../../docs/OPERABILITY.md) §3 已声明的 quota 估算上界

### Demo Path

M2 落地后（实施完成时）应能在 5 分钟内 reproduce：

```bash
# 干净 venv（M0 已支持）
pip install -e ".[dev]"

# 步骤 1：验证 Tool Registry 加载
python -c "
from hostlens.tools import ToolRegistry, register_default_tools
registry = ToolRegistry()
register_default_tools(registry)
print('Registered tools:', sorted(registry.names()))
"
# 期望输出: Registered tools: ['list_inspectors', 'list_targets', 'run_inspector']

# 步骤 2：验证 agent adapter 投影出合法 Anthropic tool_use schema
python -c "
import json
from hostlens.tools import ToolRegistry, register_default_tools
from hostlens.tools.base import ToolContext
from hostlens.agent.tools_adapter import ToolsAdapter

registry = ToolRegistry()
register_default_tools(registry)
# ToolsAdapter 唯一合约: (registry, context_factory)
# Demo 用一个抛 NotImplementedError 的 stub factory（仅验证 list_for_agent 投影路径，
# 不实际 dispatch）
adapter = ToolsAdapter(registry, lambda: (_ for _ in ()).throw(NotImplementedError('demo')))
schemas = adapter.list_for_agent()
print(json.dumps(schemas, indent=2, ensure_ascii=False))
"
# 期望输出: 3 个 Anthropic tool_use JSON Schema，每个含 name / description / input_schema
# 三个 key 按 insertion order 输出（name → description → input_schema）

# 步骤 3：单元测试 + 集成测试
pytest tests/tools/ tests/agent/ tests/integration/ -v
# 期望: 至少 5 个 policy gate 单测通过（surface mismatch / side_effects write/destructive /
# requires_approval / args validation / timeout 路径）+ 集成测试覆盖 demo path 端到端
```

**完整 demo（含 Agent loop 调用真实 Inspector）依赖 M1 ExecutionTarget + Inspector 与 M2 `add-llm-backend-protocol` + `add-agent-loop-skeleton` 落地**，因此本 proposal 的 demo 是 **registry-only 路径**：验证 Tool Registry / Adapter / 首批 ToolSpec schema 的可加载与可投影。
